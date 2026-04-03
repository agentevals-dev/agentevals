"""Tests for the OTLP HTTP receiver endpoints and session auto-management."""

import asyncio
import signal
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentevals.api.otlp_grpc import (
    GRPC_SHUTDOWN_GRACE_SECONDS,
    OtlpLogsService,
    OtlpTraceService,
    create_otlp_grpc_server,
)
from agentevals.api.otlp_processing import (
    _convert_otlp_log_record,
    _extract_agentevals_metadata,
    _normalize_span,
    _parse_otlp_body,
    decode_protobuf_logs,
    decode_protobuf_traces,
    fix_protobuf_id_fields,
    process_logs,
    process_traces,
)
from agentevals.cli import _install_shared_exit_handler
from agentevals.streaming.session import TraceSession
from agentevals.streaming.ws_server import StreamingTraceManager


def _run(coro):
    return asyncio.run(coro)


def _make_otlp_attr(key: str, value, value_type: str = "stringValue") -> dict:
    return {"key": key, "value": {value_type: value}}


def _make_span(
    trace_id: str = "abc123",
    span_id: str = "span1",
    parent_span_id: str | None = "parent1",
    name: str = "test_span",
    attributes: list[dict] | None = None,
) -> dict:
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": attributes or [],
        "status": {"code": 0},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def _make_export_request(
    spans: list[dict],
    resource_attrs: list[dict] | None = None,
    scope_name: str = "",
    scope_version: str = "",
) -> dict:
    scope = {}
    if scope_name:
        scope["name"] = scope_name
    if scope_version:
        scope["version"] = scope_version

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs or []},
                "scopeSpans": [
                    {
                        "scope": scope,
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def _make_mgr():
    """Create a StreamingTraceManager with broadcast mocked."""
    mgr = StreamingTraceManager()
    mgr.broadcast_to_ui = AsyncMock()
    return mgr


def _make_resource_attrs(
    session_name: str | None = None,
    eval_set_id: str | None = None,
) -> list[dict]:
    attrs = []
    if session_name:
        attrs.append(_make_otlp_attr("agentevals.session_name", session_name))
    if eval_set_id:
        attrs.append(_make_otlp_attr("agentevals.eval_set_id", eval_set_id))
    return attrs


def _cancel_timers(mgr):
    for t in mgr._idle_timers.values():
        t.cancel()
    for t in mgr._completion_timers.values():
        t.cancel()


# ---------------------------------------------------------------------------
# Span normalization
# ---------------------------------------------------------------------------


class TestNormalizeSpan:
    def test_injects_scope_name(self):
        span = _make_span(attributes=[])
        result = _normalize_span(span, "gcp.vertex.agent", "1.0.0")
        attr_keys = {a["key"] for a in result["attributes"]}
        assert "otel.scope.name" in attr_keys
        assert "otel.scope.version" in attr_keys

    def test_does_not_duplicate_existing_scope(self):
        span = _make_span(
            attributes=[
                _make_otlp_attr("otel.scope.name", "existing-scope"),
            ]
        )
        result = _normalize_span(span, "gcp.vertex.agent", "1.0.0")
        scope_attrs = [a for a in result["attributes"] if a["key"] == "otel.scope.name"]
        assert len(scope_attrs) == 1
        assert scope_attrs[0]["value"]["stringValue"] == "existing-scope"

    def test_empty_scope_no_injection(self):
        span = _make_span(attributes=[])
        result = _normalize_span(span, "", "")
        attr_keys = {a["key"] for a in result["attributes"]}
        assert "otel.scope.name" not in attr_keys
        assert "otel.scope.version" not in attr_keys

    def test_does_not_mutate_original_attrs_list(self):
        original_attrs = [_make_otlp_attr("key1", "val1")]
        span = _make_span(attributes=original_attrs)
        _normalize_span(span, "scope", "1.0")
        assert len(original_attrs) == 1

    def test_promotes_genai_event_attributes(self):
        """Strands SDK puts gen_ai.input/output.messages in span events."""
        span = _make_span(attributes=[])
        span["events"] = [
            {
                "name": "gen_ai.client.inference.operation.details",
                "attributes": [
                    _make_otlp_attr("gen_ai.input.messages", '[{"role":"user"}]'),
                ],
            },
            {
                "name": "gen_ai.client.inference.operation.details",
                "attributes": [
                    _make_otlp_attr("gen_ai.output.messages", '[{"role":"assistant"}]'),
                ],
            },
        ]
        result = _normalize_span(span, "", "")
        attr_keys = {a["key"] for a in result["attributes"]}
        assert "gen_ai.input.messages" in attr_keys
        assert "gen_ai.output.messages" in attr_keys

    def test_event_promotion_does_not_overwrite_existing(self):
        span = _make_span(
            attributes=[
                _make_otlp_attr("gen_ai.input.messages", '[{"existing": true}]'),
            ]
        )
        span["events"] = [
            {
                "name": "gen_ai.client.inference.operation.details",
                "attributes": [
                    _make_otlp_attr("gen_ai.input.messages", '[{"from_event": true}]'),
                ],
            },
        ]
        result = _normalize_span(span, "", "")
        input_attrs = [a for a in result["attributes"] if a["key"] == "gen_ai.input.messages"]
        assert len(input_attrs) == 1
        assert "existing" in input_attrs[0]["value"]["stringValue"]

    def test_ignores_non_genai_event_attributes(self):
        span = _make_span(attributes=[])
        span["events"] = [
            {
                "name": "some.event",
                "attributes": [
                    _make_otlp_attr("some.random.key", "value"),
                ],
            },
        ]
        result = _normalize_span(span, "", "")
        attr_keys = {a["key"] for a in result["attributes"]}
        assert "some.random.key" not in attr_keys


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


class TestExtractMetadata:
    def test_extracts_eval_set_id(self):
        attrs = [
            _make_otlp_attr("agentevals.eval_set_id", "my-eval"),
            _make_otlp_attr("service.name", "test-agent"),
        ]
        meta = _extract_agentevals_metadata(attrs)
        assert meta["eval_set_id"] == "my-eval"
        assert meta["service_name"] == "test-agent"

    def test_extracts_session_name(self):
        attrs = [_make_otlp_attr("agentevals.session_name", "run-42")]
        meta = _extract_agentevals_metadata(attrs)
        assert meta["session_name"] == "run-42"

    def test_missing_keys_are_none(self):
        meta = _extract_agentevals_metadata([])
        assert meta["eval_set_id"] is None
        assert meta["session_name"] is None
        assert meta["service_name"] is None


# ---------------------------------------------------------------------------
# OTLP log record conversion
# ---------------------------------------------------------------------------


class TestConvertOtlpLogRecord:
    def test_genai_user_message(self):
        record = {
            "timeUnixNano": "1000000000",
            "body": {"stringValue": '{"content": "hello"}'},
            "attributes": [
                _make_otlp_attr("event.name", "gen_ai.user.message"),
            ],
            "traceId": "abc123",
        }
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "gen_ai.user.message"
        assert result["body"] == {"content": "hello"}

    def test_non_genai_log_returns_none(self):
        record = {
            "timeUnixNano": "1000000000",
            "body": {"stringValue": "some log"},
            "attributes": [
                _make_otlp_attr("event.name", "http.request"),
            ],
        }
        assert _convert_otlp_log_record(record) is None

    def test_missing_event_name_returns_none(self):
        record = {
            "timeUnixNano": "1000000000",
            "body": {"stringValue": "data"},
            "attributes": [],
        }
        assert _convert_otlp_log_record(record) is None

    def test_kvlist_body(self):
        record = {
            "timeUnixNano": "1000000000",
            "body": {
                "kvlistValue": {
                    "values": [
                        _make_otlp_attr("content", "hello from kvlist"),
                    ]
                }
            },
            "attributes": [
                _make_otlp_attr("event.name", "gen_ai.user.message"),
            ],
        }
        result = _convert_otlp_log_record(record)
        assert result["body"] == {"content": "hello from kvlist"}


class TestParseOtlpBody:
    def test_string_value_json(self):
        assert _parse_otlp_body({"stringValue": '{"key": "val"}'}) == {"key": "val"}

    def test_string_value_plain(self):
        assert _parse_otlp_body({"stringValue": "plain text"}) == "plain text"

    def test_empty_body(self):
        assert _parse_otlp_body({}) == {}

    def test_nested_kvlist_body(self):
        """OpenAI instrumentor sends gen_ai.choice with nested kvlistValue."""
        body = {
            "kvlistValue": {
                "values": [
                    {"key": "index", "value": {"intValue": "0"}},
                    {"key": "finish_reason", "value": {"stringValue": "stop"}},
                    {
                        "key": "message",
                        "value": {
                            "kvlistValue": {
                                "values": [
                                    {"key": "role", "value": {"stringValue": "assistant"}},
                                    {"key": "content", "value": {"stringValue": "Hello!"}},
                                ]
                            }
                        },
                    },
                ]
            }
        }
        result = _parse_otlp_body(body)
        assert result == {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Hello!"},
        }

    def test_kvlist_with_array_value(self):
        """Tool calls come as arrayValue inside kvlistValue."""
        body = {
            "kvlistValue": {
                "values": [
                    {"key": "role", "value": {"stringValue": "assistant"}},
                    {
                        "key": "tool_calls",
                        "value": {
                            "arrayValue": {
                                "values": [
                                    {
                                        "kvlistValue": {
                                            "values": [
                                                {"key": "id", "value": {"stringValue": "call_1"}},
                                                {"key": "type", "value": {"stringValue": "function"}},
                                            ]
                                        }
                                    },
                                ]
                            }
                        },
                    },
                ]
            }
        }
        result = _parse_otlp_body(body)
        assert result["role"] == "assistant"
        assert result["tool_calls"] == [{"id": "call_1", "type": "function"}]


class TestConvertOtlpLogRecordEventName:
    """Tests for the eventName top-level field (newer OTel SDKs)."""

    def test_eventname_field_used(self):
        """OpenAI instrumentor v2 puts event name in top-level eventName."""
        record = {
            "eventName": "gen_ai.user.message",
            "observedTimeUnixNano": "1773581221981770020",
            "body": {
                "kvlistValue": {
                    "values": [
                        {"key": "content", "value": {"stringValue": "Hello!"}},
                    ]
                }
            },
            "attributes": [
                _make_otlp_attr("gen_ai.system", "openai"),
            ],
            "traceId": "abc123",
            "spanId": "def456",
        }
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "gen_ai.user.message"
        assert result["body"] == {"content": "Hello!"}
        assert result["span_id"] == "def456"

    def test_event_name_attribute_still_works(self):
        """Strands-style: event.name in attributes with stringValue body."""
        record = {
            "timeUnixNano": "1000000000",
            "body": {"stringValue": '{"role": "user", "content": "Hi"}'},
            "attributes": [
                _make_otlp_attr("event.name", "gen_ai.user.message"),
            ],
            "traceId": "abc123",
        }
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "gen_ai.user.message"
        assert result["body"] == {"role": "user", "content": "Hi"}

    def test_eventname_takes_precedence_over_attribute(self):
        record = {
            "eventName": "gen_ai.choice",
            "timeUnixNano": "1000000000",
            "body": {"stringValue": "{}"},
            "attributes": [
                _make_otlp_attr("event.name", "gen_ai.user.message"),
            ],
        }
        result = _convert_otlp_log_record(record)
        assert result["event_name"] == "gen_ai.choice"

    def test_non_genai_eventname_returns_none(self):
        record = {
            "eventName": "http.request",
            "body": {"stringValue": "data"},
            "attributes": [],
        }
        assert _convert_otlp_log_record(record) is None

    def test_observed_time_fallback(self):
        record = {
            "eventName": "gen_ai.user.message",
            "observedTimeUnixNano": "9999",
            "body": {"stringValue": '{"content": "hi"}'},
            "attributes": [],
        }
        result = _convert_otlp_log_record(record)
        assert result["timestamp"] == "9999"

    def test_time_unix_nano_preferred_over_observed(self):
        record = {
            "eventName": "gen_ai.user.message",
            "timeUnixNano": "1111",
            "observedTimeUnixNano": "9999",
            "body": {"stringValue": '{"content": "hi"}'},
            "attributes": [],
        }
        result = _convert_otlp_log_record(record)
        assert result["timestamp"] == "1111"

    def test_openai_choice_with_nested_kvlist(self):
        """Full OpenAI instrumentor gen_ai.choice record with nested message."""
        record = {
            "eventName": "gen_ai.choice",
            "observedTimeUnixNano": "1773581222000000000",
            "body": {
                "kvlistValue": {
                    "values": [
                        {"key": "index", "value": {"intValue": "0"}},
                        {"key": "finish_reason", "value": {"stringValue": "stop"}},
                        {
                            "key": "message",
                            "value": {
                                "kvlistValue": {
                                    "values": [
                                        {"key": "role", "value": {"stringValue": "assistant"}},
                                        {"key": "content", "value": {"stringValue": "I can help!"}},
                                    ]
                                }
                            },
                        },
                    ]
                }
            },
            "attributes": [
                _make_otlp_attr("gen_ai.system", "openai"),
            ],
            "traceId": "abc",
            "spanId": "def",
        }
        result = _convert_otlp_log_record(record)
        assert result["event_name"] == "gen_ai.choice"
        assert result["body"]["message"]["role"] == "assistant"
        assert result["body"]["message"]["content"] == "I can help!"
        assert result["body"]["index"] == 0
        assert result["body"]["finish_reason"] == "stop"


class TestLateLogReextraction:
    """Logs arriving after session completion trigger re-extraction."""

    def test_late_logs_accepted_for_completed_session(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-abc", meta)
            session.is_complete = True
            mgr.schedule_log_reextraction = MagicMock()

            body = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "eventName": "gen_ai.user.message",
                                        "observedTimeUnixNano": "1000000000",
                                        "body": {
                                            "kvlistValue": {
                                                "values": [
                                                    {"key": "content", "value": {"stringValue": "hello"}},
                                                ]
                                            }
                                        },
                                        "attributes": [],
                                        "traceId": "trace-abc",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)

            assert len(session.logs) == 1
            mgr.schedule_log_reextraction.assert_called_once_with("s1")

        _run(go())

    def test_late_logs_not_matched_to_completed_session_by_name(self):
        """Logs with a new trace_id should not attach to a completed session
        even if the session_name matches (the next run may reuse the name)."""

        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "named-session", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-abc", meta)
            session.is_complete = True
            mgr.buffer_orphan_log = MagicMock()

            body = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                _make_otlp_attr("agentevals.session_name", "named-session"),
                            ]
                        },
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "eventName": "gen_ai.user.message",
                                        "observedTimeUnixNano": "1000000000",
                                        "body": {"stringValue": '{"content": "hi"}'},
                                        "attributes": [],
                                        "traceId": "new-trace-id",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)

            assert len(session.logs) == 0
            assert "new-trace-id" not in session.trace_ids
            mgr.buffer_orphan_log.assert_called_once()

        _run(go())


# ---------------------------------------------------------------------------
# StreamingTraceManager — OTLP session management
# ---------------------------------------------------------------------------


class TestGetOrCreateOtlpSession:
    def test_creates_new_session(self):
        mgr = _make_mgr()
        meta = {"eval_set_id": "eval1", "session_name": "s1", "resource_attrs": {}}
        session = _run(mgr.get_or_create_otlp_session("trace-abc", meta))
        assert session.session_id == "s1"
        assert session.trace_id == "trace-abc"
        assert session.eval_set_id == "eval1"
        assert session.source == "otlp"
        assert "s1" in mgr.sessions
        assert "s1" in mgr.incremental_extractors

    def test_returns_existing_session(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            s1 = await mgr.get_or_create_otlp_session("trace-abc", meta)
            s2 = await mgr.get_or_create_otlp_session("trace-abc", meta)
            assert s1 is s2

        _run(go())

    def test_new_trace_id_does_not_return_complete_session(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            s1 = await mgr.get_or_create_otlp_session("trace-abc", meta)
            s1.is_complete = True
            s2 = await mgr.get_or_create_otlp_session("trace-new", meta)
            assert s2 is not s1
            assert s2.session_id == "s1-2"

        _run(go())

    def test_same_trace_id_reopens_complete_session(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            s1 = await mgr.get_or_create_otlp_session("trace-abc", meta)
            s1.is_complete = True
            s2 = await mgr.get_or_create_otlp_session("trace-abc", meta)
            assert s2 is s1
            assert not s2.is_complete

        _run(go())

    def test_unique_session_ids_across_runs(self):
        """Repeated runs with the same session_name get unique IDs."""

        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "my-agent", "resource_attrs": {}}
            s1 = await mgr.get_or_create_otlp_session("trace-1", meta)
            s1.is_complete = True
            s2 = await mgr.get_or_create_otlp_session("trace-2", meta)
            s2.is_complete = True
            s3 = await mgr.get_or_create_otlp_session("trace-3", meta)

            assert s1.session_id == "my-agent"
            assert s2.session_id == "my-agent-2"
            assert s3.session_id == "my-agent-3"
            assert len(mgr.sessions) == 3

        _run(go())

    def test_auto_generates_session_name(self):
        mgr = _make_mgr()
        meta = {"eval_set_id": None, "session_name": None, "resource_attrs": {}}
        session = _run(mgr.get_or_create_otlp_session("abcdef123456789", meta))
        assert session.session_id.startswith("otlp-abcdef123456")

    def test_broadcasts_session_started(self):
        mgr = _make_mgr()
        meta = {"eval_set_id": "e1", "session_name": "s1", "resource_attrs": {}}
        _run(mgr.get_or_create_otlp_session("trace-1", meta))

        mgr.broadcast_to_ui.assert_called_once()
        event = mgr.broadcast_to_ui.call_args[0][0]
        assert event["type"] == "session_started"
        assert event["session"]["sessionId"] == "s1"
        assert event["session"]["evalSetId"] == "e1"

    def test_excludes_agentevals_attrs_from_metadata(self):
        mgr = _make_mgr()
        meta = {
            "eval_set_id": "e1",
            "session_name": "s1",
            "resource_attrs": {
                "agentevals.eval_set_id": "e1",
                "service.name": "my-agent",
                "deployment.env": "dev",
            },
        }
        session = _run(mgr.get_or_create_otlp_session("trace-1", meta))
        assert "agentevals.eval_set_id" not in session.metadata
        assert session.metadata["service.name"] == "my-agent"


class TestCompleteOtlpSession:
    def test_marks_session_complete(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-1", meta)
            session.spans.append(_make_span())
            mgr.broadcast_to_ui = AsyncMock()
            await mgr._complete_otlp_session("s1")
            assert session.is_complete is True
            assert "s1" not in mgr.incremental_extractors

        _run(go())

    def test_broadcasts_session_complete(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            mgr.broadcast_to_ui = AsyncMock()
            await mgr._complete_otlp_session("s1")
            calls = mgr.broadcast_to_ui.call_args_list
            assert any(c[0][0]["type"] == "session_complete" for c in calls)

        _run(go())

    def test_idempotent(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            mgr.broadcast_to_ui = AsyncMock()
            await mgr._complete_otlp_session("s1")
            await mgr._complete_otlp_session("s1")
            complete_events = [c for c in mgr.broadcast_to_ui.call_args_list if c[0][0]["type"] == "session_complete"]
            assert len(complete_events) == 1

        _run(go())

    def test_missing_session_no_error(self):
        mgr = _make_mgr()
        _run(mgr._complete_otlp_session("nonexistent"))


class TestScheduleSessionCompletion:
    def test_creates_timer(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            mgr.schedule_session_completion("s1")
            assert "s1" in mgr._completion_timers
            assert isinstance(mgr._completion_timers["s1"], asyncio.Task)
            _cancel_timers(mgr)

        _run(go())

    def test_replaces_existing_timer(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            mgr.schedule_session_completion("s1")
            task1 = mgr._completion_timers["s1"]
            mgr.schedule_session_completion("s1")
            task2 = mgr._completion_timers["s1"]
            assert task1 is not task2
            await asyncio.sleep(0)
            assert task1.cancelled()
            _cancel_timers(mgr)

        _run(go())


class TestResetIdleTimer:
    def test_creates_idle_timer(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            mgr.reset_idle_timer("s1")
            assert "s1" in mgr._idle_timers
            assert isinstance(mgr._idle_timers["s1"], asyncio.Task)
            _cancel_timers(mgr)

        _run(go())


# ---------------------------------------------------------------------------
# Full pipeline: process_traces
# ---------------------------------------------------------------------------


class TestProcessTraces:
    def test_single_span_creates_session(self):
        async def go():
            mgr = _make_mgr()
            body = _make_export_request(
                spans=[_make_span(trace_id="t1", parent_span_id="p1")],
                resource_attrs=[
                    _make_otlp_attr("agentevals.session_name", "test-session"),
                ],
            )
            await process_traces(body, mgr)
            assert "test-session" in mgr.sessions
            session = mgr.sessions["test-session"]
            assert len(session.spans) == 1
            assert session.trace_id == "t1"
            _cancel_timers(mgr)

        _run(go())

    def test_multiple_spans_same_trace(self):
        async def go():
            mgr = _make_mgr()
            body = _make_export_request(
                spans=[
                    _make_span(trace_id="t1", span_id="s1", parent_span_id="p1"),
                    _make_span(trace_id="t1", span_id="s2", parent_span_id="p1"),
                ],
            )
            await process_traces(body, mgr)
            sessions = [s for s in mgr.sessions.values() if s.trace_id == "t1"]
            assert len(sessions) == 1
            assert len(sessions[0].spans) == 2
            _cancel_timers(mgr)

        _run(go())

    def test_different_traces_create_different_sessions(self):
        async def go():
            mgr = _make_mgr()
            body = {
                "resourceSpans": [
                    {
                        "resource": {"attributes": []},
                        "scopeSpans": [
                            {
                                "scope": {},
                                "spans": [
                                    _make_span(trace_id="t1", span_id="s1"),
                                ],
                            }
                        ],
                    },
                    {
                        "resource": {"attributes": []},
                        "scopeSpans": [
                            {
                                "scope": {},
                                "spans": [
                                    _make_span(trace_id="t2", span_id="s2"),
                                ],
                            }
                        ],
                    },
                ]
            }
            await process_traces(body, mgr)
            trace_ids = {s.trace_id for s in mgr.sessions.values()}
            assert "t1" in trace_ids
            assert "t2" in trace_ids
            _cancel_timers(mgr)

        _run(go())

    def test_scope_injected_into_spans(self):
        async def go():
            mgr = _make_mgr()
            body = _make_export_request(
                spans=[_make_span(trace_id="t1")],
                scope_name="gcp.vertex.agent",
                scope_version="1.2.3",
            )
            await process_traces(body, mgr)
            session = list(mgr.sessions.values())[0]
            span = session.spans[0]
            attr_map = {a["key"]: a["value"] for a in span["attributes"]}
            assert attr_map["otel.scope.name"]["stringValue"] == "gcp.vertex.agent"
            assert attr_map["otel.scope.version"]["stringValue"] == "1.2.3"
            _cancel_timers(mgr)

        _run(go())

    def test_root_span_schedules_completion(self):
        """Root spans trigger a 3-second grace period for session completion."""

        async def go():
            mgr = _make_mgr()
            mgr.schedule_session_completion = MagicMock()
            body = _make_export_request(
                spans=[_make_span(trace_id="t1", parent_span_id=None)],
            )
            await process_traces(body, mgr)
            session = list(mgr.sessions.values())[0]
            assert session.has_root_span is True
            mgr.schedule_session_completion.assert_called_once_with(session.session_id)
            _cancel_timers(mgr)

        _run(go())

    def test_idle_timer_reset_on_each_span(self):
        async def go():
            mgr = _make_mgr()
            mgr.reset_idle_timer = MagicMock()
            body = _make_export_request(
                spans=[
                    _make_span(trace_id="t1", span_id="s1"),
                    _make_span(trace_id="t1", span_id="s2"),
                ],
            )
            await process_traces(body, mgr)
            assert mgr.reset_idle_timer.call_count == 2
            _cancel_timers(mgr)

        _run(go())

    def test_multi_trace_same_session(self):
        """Spans from different traces with the same session_name group into one session."""

        async def go():
            mgr = _make_mgr()
            meta = _make_resource_attrs(session_name="my-session")

            body1 = _make_export_request(
                spans=[_make_span(trace_id="trace-a", span_id="s1")],
                resource_attrs=meta,
            )
            body2 = _make_export_request(
                spans=[_make_span(trace_id="trace-b", span_id="s2")],
                resource_attrs=meta,
            )
            await process_traces(body1, mgr)
            await process_traces(body2, mgr)

            assert len(mgr.sessions) == 1
            session = mgr.sessions["my-session"]
            assert len(session.spans) == 2
            assert session.trace_ids == {"trace-a", "trace-b"}
            _cancel_timers(mgr)

        _run(go())

    def test_logs_route_to_multi_trace_session(self):
        """Logs with any of the session's trace_ids are routed correctly."""

        async def go():
            mgr = _make_mgr()
            meta = _make_resource_attrs(session_name="my-session")

            body1 = _make_export_request(
                spans=[_make_span(trace_id="trace-a")],
                resource_attrs=meta,
            )
            body2 = _make_export_request(
                spans=[_make_span(trace_id="trace-b")],
                resource_attrs=meta,
            )
            await process_traces(body1, mgr)
            await process_traces(body2, mgr)

            log_body = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": "1000000000",
                                        "body": {"stringValue": '{"content": "hello"}'},
                                        "attributes": [
                                            _make_otlp_attr("event.name", "gen_ai.user.message"),
                                        ],
                                        "traceId": "trace-a",
                                    },
                                    {
                                        "timeUnixNano": "2000000000",
                                        "body": {"stringValue": '{"content": "world"}'},
                                        "attributes": [
                                            _make_otlp_attr("event.name", "gen_ai.user.message"),
                                        ],
                                        "traceId": "trace-b",
                                    },
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(log_body, mgr)

            session = mgr.sessions["my-session"]
            assert len(session.logs) == 2
            _cancel_timers(mgr)

        _run(go())

    def test_empty_request(self):
        async def go():
            mgr = _make_mgr()
            await process_traces({"resourceSpans": []}, mgr)
            assert len(mgr.sessions) == 0

        _run(go())

    def test_broadcasts_span_received(self):
        async def go():
            mgr = _make_mgr()
            body = _make_export_request(
                spans=[_make_span(trace_id="t1")],
            )
            await process_traces(body, mgr)
            span_received_calls = [c for c in mgr.broadcast_to_ui.call_args_list if c[0][0]["type"] == "span_received"]
            assert len(span_received_calls) == 1
            _cancel_timers(mgr)

        _run(go())


# ---------------------------------------------------------------------------
# Full pipeline: process_logs
# ---------------------------------------------------------------------------


class TestOrphanLogBuffer:
    """Tests for orphan log buffering when logs arrive before sessions."""

    def test_logs_buffered_when_no_session_exists(self):
        """Logs arriving before any session should be buffered, not dropped."""

        async def go():
            mgr = _make_mgr()
            body = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                _make_otlp_attr("agentevals.session_name", "my-agent"),
                            ]
                        },
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "eventName": "gen_ai.user.message",
                                        "observedTimeUnixNano": "1000000000",
                                        "body": {
                                            "kvlistValue": {
                                                "values": [
                                                    {"key": "content", "value": {"stringValue": "Hello!"}},
                                                ]
                                            }
                                        },
                                        "attributes": [],
                                        "traceId": "trace-1",
                                        "spanId": "span-1",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)
            assert len(mgr._orphan_logs) == 1
            assert mgr._orphan_logs[0]["trace_id"] == "trace-1"
            assert mgr._orphan_logs[0]["session_name"] == "my-agent"

        _run(go())

    def test_orphan_logs_replayed_on_session_creation(self):
        """Buffered logs are injected when a matching session is created."""

        async def go():
            mgr = _make_mgr()

            log_body = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                _make_otlp_attr("agentevals.session_name", "my-agent"),
                            ]
                        },
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "eventName": "gen_ai.user.message",
                                        "observedTimeUnixNano": "1000000000",
                                        "body": {
                                            "kvlistValue": {
                                                "values": [
                                                    {"key": "content", "value": {"stringValue": "Hello!"}},
                                                ]
                                            }
                                        },
                                        "attributes": [],
                                        "traceId": "trace-1",
                                        "spanId": "span-1",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(log_body, mgr)
            assert len(mgr._orphan_logs) == 1

            meta = {"eval_set_id": None, "session_name": "my-agent", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-1", meta)

            assert len(session.logs) == 1
            assert session.logs[0]["event_name"] == "gen_ai.user.message"
            assert len(mgr._orphan_logs) == 0

        _run(go())

    def test_orphan_logs_matched_by_session_name(self):
        """Orphan logs with different trace_id but same session_name are replayed."""

        async def go():
            mgr = _make_mgr()

            log_body = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                _make_otlp_attr("agentevals.session_name", "my-agent"),
                            ]
                        },
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "eventName": "gen_ai.user.message",
                                        "observedTimeUnixNano": "1000000000",
                                        "body": {
                                            "kvlistValue": {
                                                "values": [
                                                    {"key": "content", "value": {"stringValue": "Hello!"}},
                                                ]
                                            }
                                        },
                                        "attributes": [],
                                        "traceId": "different-trace",
                                        "spanId": "span-1",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(log_body, mgr)

            meta = {"eval_set_id": None, "session_name": "my-agent", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-1", meta)

            assert len(session.logs) == 1
            assert "different-trace" in session.trace_ids

        _run(go())

    def test_expired_orphan_logs_not_replayed(self):
        """Orphan logs older than max_age are discarded."""

        async def go():
            mgr = _make_mgr()

            mgr._orphan_logs.append(
                {
                    "trace_id": "trace-old",
                    "session_name": "my-agent",
                    "log_event": {
                        "event_name": "gen_ai.user.message",
                        "timestamp": "1000",
                        "body": {"content": "old"},
                        "attributes": {},
                    },
                    "buffered_at": datetime.now(UTC) - timedelta(seconds=120),
                }
            )

            meta = {"eval_set_id": None, "session_name": "my-agent", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-1", meta)

            assert len(session.logs) == 0

        _run(go())

    def test_multiple_orphan_logs_for_same_session(self):
        """Multiple orphan logs are all replayed into the session."""

        async def go():
            mgr = _make_mgr()

            for i in range(3):
                log_body = {
                    "resourceLogs": [
                        {
                            "resource": {
                                "attributes": [
                                    _make_otlp_attr("agentevals.session_name", "my-agent"),
                                ]
                            },
                            "scopeLogs": [
                                {
                                    "logRecords": [
                                        {
                                            "eventName": "gen_ai.user.message",
                                            "observedTimeUnixNano": str(1000 + i),
                                            "body": {
                                                "kvlistValue": {
                                                    "values": [
                                                        {"key": "content", "value": {"stringValue": f"msg {i}"}},
                                                    ]
                                                }
                                            },
                                            "attributes": [],
                                            "traceId": "trace-1",
                                            "spanId": f"span-{i}",
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
                await process_logs(log_body, mgr)

            assert len(mgr._orphan_logs) == 3

            meta = {"eval_set_id": None, "session_name": "my-agent", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session("trace-1", meta)

            assert len(session.logs) == 3
            assert len(mgr._orphan_logs) == 0

        _run(go())


class TestProcessLogs:
    def test_routes_log_to_session_by_trace_id(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-abc", meta)
            body = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": "1000000000",
                                        "body": {"stringValue": '{"content": "hello"}'},
                                        "attributes": [
                                            _make_otlp_attr("event.name", "gen_ai.user.message"),
                                        ],
                                        "traceId": "trace-abc",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)
            session = mgr.sessions["s1"]
            assert len(session.logs) == 1
            assert session.logs[0]["event_name"] == "gen_ai.user.message"

        _run(go())

    def test_buffers_log_with_unknown_trace_id(self):
        async def go():
            mgr = _make_mgr()
            body = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": "1000000000",
                                        "body": {"stringValue": '{"content": "hi"}'},
                                        "attributes": [
                                            _make_otlp_attr("event.name", "gen_ai.user.message"),
                                        ],
                                        "traceId": "unknown-trace",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)
            assert len(mgr.sessions) == 0
            assert len(mgr._orphan_logs) == 1

        _run(go())

    def test_ignores_non_genai_logs(self):
        async def go():
            mgr = _make_mgr()
            meta = {"eval_set_id": None, "session_name": "s1", "resource_attrs": {}}
            await mgr.get_or_create_otlp_session("trace-1", meta)
            body = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": "1000000000",
                                        "body": {"stringValue": "just a log"},
                                        "attributes": [
                                            _make_otlp_attr("event.name", "http.request"),
                                        ],
                                        "traceId": "trace-1",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            await process_logs(body, mgr)
            session = mgr.sessions["s1"]
            assert len(session.logs) == 0

        _run(go())


# ---------------------------------------------------------------------------
# Cleanup with timers
# ---------------------------------------------------------------------------


class TestCleanupWithTimers:
    def test_cleanup_cancels_timers(self):
        async def go():
            mgr = StreamingTraceManager()
            session = TraceSession(
                session_id="s1",
                trace_id="t1",
                eval_set_id=None,
                source="otlp",
            )
            session.is_complete = True
            session.started_at = datetime.now(UTC) - timedelta(hours=10)
            mgr.sessions["s1"] = session

            mgr._completion_timers["s1"] = asyncio.create_task(asyncio.sleep(999))
            mgr._idle_timers["s1"] = asyncio.create_task(asyncio.sleep(999))

            removed = mgr._cleanup_old_sessions()
            assert removed == 1
            assert "s1" not in mgr._completion_timers
            assert "s1" not in mgr._idle_timers

        _run(go())


# ---------------------------------------------------------------------------
# Protobuf decoding
# ---------------------------------------------------------------------------

import base64

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest as LogsServiceRequestPB,
)
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest as TraceServiceRequestPB,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.logs.v1.logs_pb2 import (
    LogRecord as LogRecordPB,
)
from opentelemetry.proto.logs.v1.logs_pb2 import (
    ResourceLogs,
    ScopeLogs,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource as ResourcePB
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
)
from opentelemetry.proto.trace.v1.trace_pb2 import (
    Span as SpanPB,
)


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


TRACE_ID_HEX = "0102030405060708090a0b0c0d0e0f10"
SPAN_ID_HEX = "1112131415161718"
PARENT_SPAN_ID_HEX = "2122232425262728"


def _make_pb_span(
    trace_id_hex: str = TRACE_ID_HEX,
    span_id_hex: str = SPAN_ID_HEX,
    parent_span_id_hex: str | None = PARENT_SPAN_ID_HEX,
    name: str = "test-span",
    attributes: list | None = None,
) -> SpanPB:
    span = SpanPB(
        trace_id=_hex_to_bytes(trace_id_hex),
        span_id=_hex_to_bytes(span_id_hex),
        name=name,
        kind=SpanPB.SPAN_KIND_INTERNAL,
        start_time_unix_nano=1000000000,
        end_time_unix_nano=2000000000,
    )
    if parent_span_id_hex:
        span.parent_span_id = _hex_to_bytes(parent_span_id_hex)
    if attributes:
        span.attributes.extend(attributes)
    return span


def _make_pb_export_request(
    spans: list[SpanPB],
    resource_attrs: list[KeyValue] | None = None,
    scope_name: str = "",
    scope_version: str = "",
) -> TraceServiceRequestPB:
    resource = ResourcePB()
    if resource_attrs:
        resource.attributes.extend(resource_attrs)

    scope = InstrumentationScope(name=scope_name, version=scope_version)
    scope_spans = ScopeSpans(scope=scope, spans=spans)
    resource_spans = ResourceSpans(resource=resource, scope_spans=[scope_spans])

    return TraceServiceRequestPB(resource_spans=[resource_spans])


class TestFixProtobufIdFields:
    def test_converts_base64_trace_id_to_hex(self):
        raw_bytes = _hex_to_bytes(TRACE_ID_HEX)
        b64 = base64.b64encode(raw_bytes).decode()
        data = {"traceId": b64, "spanId": base64.b64encode(_hex_to_bytes(SPAN_ID_HEX)).decode()}
        fix_protobuf_id_fields(data)
        assert data["traceId"] == TRACE_ID_HEX
        assert data["spanId"] == SPAN_ID_HEX

    def test_converts_parent_span_id(self):
        b64 = base64.b64encode(_hex_to_bytes(PARENT_SPAN_ID_HEX)).decode()
        data = {"parentSpanId": b64}
        fix_protobuf_id_fields(data)
        assert data["parentSpanId"] == PARENT_SPAN_ID_HEX

    def test_recurses_into_nested_structures(self):
        raw_bytes = _hex_to_bytes(TRACE_ID_HEX)
        b64_trace = base64.b64encode(raw_bytes).decode()
        b64_span = base64.b64encode(_hex_to_bytes(SPAN_ID_HEX)).decode()
        data = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": b64_trace, "spanId": b64_span}]}]}]}
        fix_protobuf_id_fields(data)
        span = data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["traceId"] == TRACE_ID_HEX
        assert span["spanId"] == SPAN_ID_HEX

    def test_leaves_non_id_fields_alone(self):
        data = {"name": "test", "kind": 1, "traceId": base64.b64encode(b"\x01\x02").decode()}
        fix_protobuf_id_fields(data)
        assert data["name"] == "test"
        assert data["kind"] == 1

    def test_handles_already_hex_strings(self):
        data = {"traceId": TRACE_ID_HEX}
        fix_protobuf_id_fields(data)
        assert len(data["traceId"]) > 0


class TestDecodeProtobufTraces:
    def test_single_span_roundtrip(self):
        span = _make_pb_span()
        request = _make_pb_export_request([span])
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)

        assert "resourceSpans" in body
        spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        assert spans[0]["traceId"] == TRACE_ID_HEX
        assert spans[0]["spanId"] == SPAN_ID_HEX
        assert spans[0]["parentSpanId"] == PARENT_SPAN_ID_HEX
        assert spans[0]["name"] == "test-span"

    def test_root_span_no_parent(self):
        span = _make_pb_span(parent_span_id_hex=None)
        request = _make_pb_export_request([span])
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)
        decoded_span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert "parentSpanId" not in decoded_span

    def test_preserves_attributes(self):
        attrs = [
            KeyValue(key="gen_ai.request.model", value=AnyValue(string_value="gpt-4")),
            KeyValue(key="token.count", value=AnyValue(int_value=42)),
            KeyValue(key="temperature", value=AnyValue(double_value=0.7)),
            KeyValue(key="stream", value=AnyValue(bool_value=True)),
        ]
        span = _make_pb_span(attributes=attrs)
        request = _make_pb_export_request([span])
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)
        decoded_attrs = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        attr_map = {a["key"]: a["value"] for a in decoded_attrs}
        assert attr_map["gen_ai.request.model"]["stringValue"] == "gpt-4"
        assert "stream" in attr_map

    def test_preserves_resource_attributes(self):
        resource_attrs = [
            KeyValue(key="service.name", value=AnyValue(string_value="test-agent")),
            KeyValue(key="agentevals.eval_set_id", value=AnyValue(string_value="eval-1")),
        ]
        span = _make_pb_span()
        request = _make_pb_export_request([span], resource_attrs=resource_attrs)
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)
        res_attrs = body["resourceSpans"][0]["resource"]["attributes"]
        attr_map = {a["key"]: a["value"] for a in res_attrs}
        assert attr_map["service.name"]["stringValue"] == "test-agent"
        assert attr_map["agentevals.eval_set_id"]["stringValue"] == "eval-1"

    def test_preserves_scope(self):
        span = _make_pb_span()
        request = _make_pb_export_request([span], scope_name="gcp.vertex.agent", scope_version="1.2.3")
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)
        scope = body["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "gcp.vertex.agent"
        assert scope["version"] == "1.2.3"

    def test_multiple_spans(self):
        span1 = _make_pb_span(span_id_hex="1112131415161718", name="span-1")
        span2 = _make_pb_span(span_id_hex="2122232425262728", name="span-2")
        request = _make_pb_export_request([span1, span2])
        raw = request.SerializeToString()

        body = decode_protobuf_traces(raw)
        spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2
        names = {s["name"] for s in spans}
        assert names == {"span-1", "span-2"}

    def test_empty_request(self):
        request = TraceServiceRequestPB()
        raw = request.SerializeToString()
        body = decode_protobuf_traces(raw)
        assert body.get("resourceSpans") is None or body.get("resourceSpans") == []


class TestDecodeProtobufLogs:
    def test_genai_log_roundtrip(self):
        log_record = LogRecordPB(
            time_unix_nano=1000000000,
            trace_id=_hex_to_bytes(TRACE_ID_HEX),
            span_id=_hex_to_bytes(SPAN_ID_HEX),
            body=AnyValue(string_value='{"content": "hello"}'),
        )
        log_record.attributes.append(KeyValue(key="event.name", value=AnyValue(string_value="gen_ai.user.message")))

        scope_logs = ScopeLogs(log_records=[log_record])
        resource_logs = ResourceLogs(scope_logs=[scope_logs])
        request = LogsServiceRequestPB(resource_logs=[resource_logs])
        raw = request.SerializeToString()

        body = decode_protobuf_logs(raw)

        assert "resourceLogs" in body
        lr = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        assert lr["traceId"] == TRACE_ID_HEX
        assert lr["spanId"] == SPAN_ID_HEX
        attrs = {a["key"]: a["value"] for a in lr.get("attributes", [])}
        assert attrs["event.name"]["stringValue"] == "gen_ai.user.message"

    def test_empty_logs_request(self):
        request = LogsServiceRequestPB()
        raw = request.SerializeToString()
        body = decode_protobuf_logs(raw)
        assert body.get("resourceLogs") is None or body.get("resourceLogs") == []


class TestCreateOtlpGrpcServer:
    def test_raises_when_bind_fails(self, monkeypatch):
        fake_server = MagicMock()
        fake_server.add_insecure_port.return_value = 0

        class _FakeAio:
            @staticmethod
            def server(**kwargs):
                return fake_server

        fake_grpc = MagicMock()
        fake_grpc.aio = _FakeAio()
        monkeypatch.setitem(sys.modules, "grpc", fake_grpc)

        with pytest.raises(RuntimeError, match="Failed to bind OTLP gRPC receiver"):
            create_otlp_grpc_server(
                host="127.0.0.1",
                port=4317,
                manager=MagicMock(),
            )


class _FakeGrpcServer:
    def __init__(self):
        self.grace_values: list[float | None] = []

    async def stop(self, grace: float | None) -> None:
        self.grace_values.append(grace)


class TestInstallSharedExitHandler:
    def test_first_sigint_gracefully_stops_grpc(self):
        async def go():
            server_a = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
            server_b = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
            grpc_server = _FakeGrpcServer()

            _install_shared_exit_handler(server_a, server_b, grpc_server=grpc_server)

            server_a.handle_exit(signal.SIGINT, None)
            await asyncio.sleep(0)

            assert server_a.should_exit is True
            assert server_b.should_exit is True
            assert server_a.force_exit is False
            assert server_b.force_exit is False
            assert grpc_server.grace_values == [GRPC_SHUTDOWN_GRACE_SECONDS]

        _run(go())

    def test_second_sigint_force_stops_grpc(self):
        async def go():
            server_a = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
            server_b = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
            grpc_server = _FakeGrpcServer()

            _install_shared_exit_handler(server_a, server_b, grpc_server=grpc_server)

            server_a.handle_exit(signal.SIGINT, None)
            await asyncio.sleep(0)
            server_a.handle_exit(signal.SIGINT, None)
            await asyncio.sleep(0)

            assert server_a.force_exit is True
            assert server_b.force_exit is True
            assert grpc_server.grace_values == [GRPC_SHUTDOWN_GRACE_SECONDS, 0]

        _run(go())


class TestGrpcServices:
    def test_trace_service_export_creates_session(self):
        async def go():
            mgr = _make_mgr()
            service = OtlpTraceService(mgr)

            resource_attrs = [
                KeyValue(key="agentevals.session_name", value=AnyValue(string_value="grpc-session")),
                KeyValue(key="agentevals.eval_set_id", value=AnyValue(string_value="grpc-eval")),
            ]
            span = _make_pb_span(parent_span_id_hex=PARENT_SPAN_ID_HEX)
            request = _make_pb_export_request([span], resource_attrs=resource_attrs)

            response = await service.Export(request, None)

            assert isinstance(response, trace_service_pb2.ExportTraceServiceResponse)
            assert "grpc-session" in mgr.sessions
            session = mgr.sessions["grpc-session"]
            assert session.eval_set_id == "grpc-eval"
            assert session.trace_id == TRACE_ID_HEX
            assert len(session.spans) == 1
            _cancel_timers(mgr)

        _run(go())

    def test_logs_service_export_attaches_logs_to_existing_session(self):
        async def go():
            mgr = _make_mgr()
            service = OtlpLogsService(mgr)
            meta = {"eval_set_id": None, "session_name": "grpc-logs", "resource_attrs": {}}
            session = await mgr.get_or_create_otlp_session(TRACE_ID_HEX, meta)

            log_record = LogRecordPB(
                time_unix_nano=1000000000,
                trace_id=_hex_to_bytes(TRACE_ID_HEX),
                span_id=_hex_to_bytes(SPAN_ID_HEX),
                body=AnyValue(string_value='{"content": "hello from grpc"}'),
            )
            log_record.attributes.append(KeyValue(key="event.name", value=AnyValue(string_value="gen_ai.user.message")))
            scope_logs = ScopeLogs(log_records=[log_record])
            resource_logs = ResourceLogs(scope_logs=[scope_logs])
            request = LogsServiceRequestPB(resource_logs=[resource_logs])

            response = await service.Export(request, None)

            assert isinstance(response, logs_service_pb2.ExportLogsServiceResponse)
            assert len(session.logs) == 1
            assert session.logs[0]["event_name"] == "gen_ai.user.message"
            _cancel_timers(mgr)

        _run(go())


class TestProtobufJsonParity:
    """Verify that protobuf-decoded traces produce the same session/span behavior as JSON."""

    def test_protobuf_traces_create_session(self):
        async def go():
            mgr = _make_mgr()

            resource_attrs = [
                KeyValue(key="agentevals.session_name", value=AnyValue(string_value="pb-session")),
                KeyValue(key="agentevals.eval_set_id", value=AnyValue(string_value="pb-eval")),
            ]
            span = _make_pb_span(parent_span_id_hex=PARENT_SPAN_ID_HEX)
            request = _make_pb_export_request([span], resource_attrs=resource_attrs)
            raw = request.SerializeToString()

            body = decode_protobuf_traces(raw)
            await process_traces(body, mgr)

            assert "pb-session" in mgr.sessions
            session = mgr.sessions["pb-session"]
            assert session.eval_set_id == "pb-eval"
            assert session.trace_id == TRACE_ID_HEX
            assert len(session.spans) == 1
            _cancel_timers(mgr)

        _run(go())

    def test_protobuf_root_span_schedules_completion(self):
        async def go():
            mgr = _make_mgr()
            mgr.schedule_session_completion = MagicMock()

            span = _make_pb_span(parent_span_id_hex=None)
            request = _make_pb_export_request([span])
            raw = request.SerializeToString()

            body = decode_protobuf_traces(raw)
            await process_traces(body, mgr)

            session = list(mgr.sessions.values())[0]
            assert session.has_root_span is True
            mgr.schedule_session_completion.assert_called_once()
            _cancel_timers(mgr)

        _run(go())

    def test_protobuf_scope_injection(self):
        async def go():
            mgr = _make_mgr()

            span = _make_pb_span()
            request = _make_pb_export_request([span], scope_name="strands.agent", scope_version="2.0.0")
            raw = request.SerializeToString()

            body = decode_protobuf_traces(raw)
            await process_traces(body, mgr)

            session = list(mgr.sessions.values())[0]
            stored_span = session.spans[0]
            attr_map = {a["key"]: a["value"] for a in stored_span["attributes"]}
            assert attr_map["otel.scope.name"]["stringValue"] == "strands.agent"
            assert attr_map["otel.scope.version"]["stringValue"] == "2.0.0"
            _cancel_timers(mgr)

        _run(go())
