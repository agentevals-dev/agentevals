"""Tests for the OTLP receivers, the live store and session management."""

import asyncio
import hashlib
import signal
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest as LogsServiceRequestPB
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest as TraceServiceRequestPB
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as LogRecordPB
from opentelemetry.proto.logs.v1.logs_pb2 import ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource as ResourcePB
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
from opentelemetry.proto.trace.v1.trace_pb2 import Span as SpanPB

from agentevals.api.otlp_grpc import (
    GRPC_SHUTDOWN_GRACE_SECONDS,
    OtlpLogsService,
    OtlpTraceService,
    create_otlp_grpc_server,
)
from agentevals.api.otlp_processing import (
    decode_logs_json,
    decode_logs_protobuf,
    decode_traces_json,
    decode_traces_protobuf,
    ingest_logs,
    ingest_traces,
)
from agentevals.cli import _install_shared_exit_handler
from agentevals.otel.store import Limits, TelemetryStore, session_metadata
from agentevals.streaming.manager import LiveManager


def tid(label: str) -> str:
    return hashlib.sha256(f"trace:{label}".encode()).hexdigest()[:32]


def sid(label: str) -> str:
    return hashlib.sha256(f"span:{label}".encode()).hexdigest()[:16]


def _attr(key: str, value, value_type: str = "stringValue") -> dict:
    return {"key": key, "value": {value_type: value}}


def _span(trace="t1", span="s1", parent: str | None = "p1", name="test_span", attributes=None, flags=None) -> dict:
    data = {
        "traceId": tid(trace),
        "spanId": sid(span),
        "name": name,
        "kind": 1,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": attributes or [],
        "status": {"code": 0},
    }
    if parent:
        data["parentSpanId"] = sid(parent)
    if flags is not None:
        data["flags"] = flags
    return data


def _chat(trace="t1", span="c1", parent: str | None = None, text="hello", reply="hi there", **kw) -> dict:
    return _span(
        trace,
        span,
        parent,
        name="chat gpt",
        attributes=[
            _attr("gen_ai.operation.name", "chat"),
            _attr("gen_ai.request.model", "gpt-test"),
            _attr("gen_ai.input.messages", f'[{{"role": "user", "parts": [{{"type": "text", "content": "{text}"}}]}}]'),
            _attr(
                "gen_ai.output.messages",
                f'[{{"role": "assistant", "parts": [{{"type": "text", "content": "{reply}"}}]}}]',
            ),
            _attr("gen_ai.usage.input_tokens", "10", "intValue"),
            _attr("gen_ai.usage.output_tokens", "3", "intValue"),
        ],
        **kw,
    )


def _request(spans, resource_attrs=None, scope_name="", scope_version="") -> dict:
    scope = {k: v for k, v in (("name", scope_name), ("version", scope_version)) if v}
    return {
        "resourceSpans": [
            {"resource": {"attributes": resource_attrs or []}, "scopeSpans": [{"scope": scope, "spans": spans}]}
        ]
    }


def _named(name: str, eval_set_id: str | None = None) -> list[dict]:
    attrs = [_attr("agentevals.session_name", name)]
    if eval_set_id:
        attrs.append(_attr("agentevals.eval_set_id", eval_set_id))
    return attrs


def _log(trace="t1", span: str | None = None, event="gen_ai.user.message", body=None, attributes=None, **extra):
    record = {
        "observedTimeUnixNano": "1000000000",
        "body": body if body is not None else {"stringValue": '{"content": "hello"}'},
        "attributes": attributes or [],
        "traceId": tid(trace),
        **extra,
    }
    if event:
        record["eventName"] = event
    if span:
        record["spanId"] = sid(span)
    return record


def _log_request(records, resource_attrs=None, scope_name="") -> dict:
    scope = {"name": scope_name} if scope_name else {}
    return {
        "resourceLogs": [
            {"resource": {"attributes": resource_attrs or []}, "scopeLogs": [{"scope": scope, "logRecords": records}]}
        ]
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(**limits) -> tuple[TelemetryStore, FakeClock]:
    clock = FakeClock()
    return TelemetryStore(Limits(**limits), clock=clock), clock


def _ingest(store: TelemetryStore, body: dict):
    decoded = decode_traces_json(body)
    groups: dict = {}
    for span in decoded.spans:
        groups.setdefault((id(span.resource), span.trace_id), []).append(span)
    results = [store.ingest_spans(g) for g in groups.values()]
    return decoded, results


def _ingest_logs(store: TelemetryStore, body: dict):
    decoded = decode_logs_json(body)
    groups: dict = {}
    for log in decoded.logs:
        groups.setdefault((id(log.resource), log.trace_id), []).append(log)
    return [store.ingest_logs(g) for g in groups.values()]


def _events(store: TelemetryStore, kind: str) -> list[tuple]:
    return [e for e in store.events if e[0] == kind]


# ---------------------------------------------------------------------------
# Decoding into envelopes
# ---------------------------------------------------------------------------


class TestDecodeEnvelopes:
    def test_scope_stays_on_the_span(self):
        decoded = decode_traces_json(_request([_span()], scope_name="gcp.vertex.agent", scope_version="1.2.3"))
        span = decoded.spans[0]
        assert (span.scope.name, span.scope.version) == ("gcp.vertex.agent", "1.2.3")
        assert "otel.scope.name" not in span.attributes

    def test_span_events_are_kept_with_their_attributes(self):
        data = _span()
        data["events"] = [
            {"name": "gen_ai.client.inference.operation.details", "timeUnixNano": "1", "attributes": [_attr("k", "v")]}
        ]
        span = decode_traces_json(_request([data])).spans[0]
        assert span.events[0].name == "gen_ai.client.inference.operation.details"
        assert span.events[0].attributes == {"k": "v"}

    def test_resource_attributes_never_merge_into_span_attributes(self):
        span = decode_traces_json(_request([_span()], resource_attrs=[_attr("service.name", "svc")])).spans[0]
        assert span.resource.attributes["service.name"] == "svc"
        assert "service.name" not in span.attributes

    def test_span_without_trace_id_is_rejected_by_the_receiver(self):
        data = _span()
        data["traceId"] = ""
        decoded = decode_traces_json(_request([data]))
        assert decoded.spans == []
        assert decoded.rejected_spans == 1

    def test_non_hex_ids_are_rejected_by_the_receiver(self):
        data = _span()
        data["spanId"] = "span1"
        decoded = decode_traces_json(_request([data]))
        assert decoded.rejected_spans == 1

    def test_log_event_name_field(self):
        log = decode_logs_json(_log_request([_log()])).logs[0]
        assert log.event_name == "gen_ai.user.message"

    @pytest.mark.parametrize("key", ["otel.event.name", "event.name"])
    def test_log_event_name_attribute_forms(self, key):
        log = decode_logs_json(_log_request([_log(event=None, attributes=[_attr(key, "gen_ai.choice")])])).logs[0]
        assert log.event_name == "gen_ai.choice"

    def test_event_name_field_takes_precedence_over_attribute(self):
        record = _log(event="gen_ai.user.message", attributes=[_attr("event.name", "gen_ai.choice")])
        assert decode_logs_json(_log_request([record])).logs[0].event_name == "gen_ai.user.message"

    def test_kvlist_body_decodes_to_mapping(self):
        body = {"kvlistValue": {"values": [{"key": "content", "value": {"stringValue": "hello"}}]}}
        assert decode_logs_json(_log_request([_log(body=body)])).logs[0].body == {"content": "hello"}

    def test_nested_kvlist_with_array_body(self):
        body = {
            "kvlistValue": {
                "values": [
                    {
                        "key": "message",
                        "value": {
                            "kvlistValue": {
                                "values": [
                                    {"key": "role", "value": {"stringValue": "assistant"}},
                                    {
                                        "key": "tool_calls",
                                        "value": {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "2"}]}},
                                    },
                                ]
                            }
                        },
                    }
                ]
            }
        }
        log = decode_logs_json(_log_request([_log(event="gen_ai.choice", body=body)])).logs[0]
        assert log.body == {"message": {"role": "assistant", "tool_calls": ["a", 2]}}

    def test_string_body_is_kept_verbatim(self):
        log = decode_logs_json(_log_request([_log(body={"stringValue": '{"content": "x"}'})])).logs[0]
        assert log.body == '{"content": "x"}'

    def test_both_timestamps_are_kept(self):
        record = _log(timeUnixNano="2000000000")
        log = decode_logs_json(_log_request([record])).logs[0]
        assert (log.time_unix_nano, log.observed_time_unix_nano) == (2_000_000_000, 1_000_000_000)


# ---------------------------------------------------------------------------
# Protobuf decoding
# ---------------------------------------------------------------------------

TRACE_ID_HEX = "0102030405060708090a0b0c0d0e0f10"
SPAN_ID_HEX = "1112131415161718"
PARENT_SPAN_ID_HEX = "2122232425262728"


def _pb_span(span_id_hex=SPAN_ID_HEX, parent_span_id_hex=PARENT_SPAN_ID_HEX, name="test-span", attributes=None):
    span = SpanPB(
        trace_id=bytes.fromhex(TRACE_ID_HEX),
        span_id=bytes.fromhex(span_id_hex),
        name=name,
        kind=SpanPB.SPAN_KIND_INTERNAL,
        start_time_unix_nano=1000000000,
        end_time_unix_nano=2000000000,
    )
    if parent_span_id_hex:
        span.parent_span_id = bytes.fromhex(parent_span_id_hex)
    if attributes:
        span.attributes.extend(attributes)
    return span


def _pb_request(spans, resource_attrs=None, scope_name="", scope_version="") -> TraceServiceRequestPB:
    resource = ResourcePB()
    if resource_attrs:
        resource.attributes.extend(resource_attrs)
    scope_spans = ScopeSpans(scope=InstrumentationScope(name=scope_name, version=scope_version), spans=spans)
    return TraceServiceRequestPB(resource_spans=[ResourceSpans(resource=resource, scope_spans=[scope_spans])])


def _pb_log_request(trace_hex=TRACE_ID_HEX, body='{"content": "hello"}') -> LogsServiceRequestPB:
    record = LogRecordPB(
        time_unix_nano=1000000000,
        trace_id=bytes.fromhex(trace_hex),
        span_id=bytes.fromhex(SPAN_ID_HEX),
        body=AnyValue(string_value=body),
    )
    record.attributes.append(KeyValue(key="event.name", value=AnyValue(string_value="gen_ai.user.message")))
    return LogsServiceRequestPB(resource_logs=[ResourceLogs(scope_logs=[ScopeLogs(log_records=[record])])])


class TestDecodeProtobuf:
    def test_ids_decode_to_hex(self):
        span = decode_traces_protobuf(_pb_request([_pb_span()]).SerializeToString()).spans[0]
        assert (span.trace_id, span.span_id, span.parent_span_id) == (TRACE_ID_HEX, SPAN_ID_HEX, PARENT_SPAN_ID_HEX)
        assert span.name == "test-span"

    def test_root_span_has_no_parent(self):
        span = decode_traces_protobuf(_pb_request([_pb_span(parent_span_id_hex=None)]).SerializeToString()).spans[0]
        assert span.parent_span_id is None

    def test_attribute_types_are_preserved(self):
        attrs = [
            KeyValue(key="gen_ai.request.model", value=AnyValue(string_value="gpt-4")),
            KeyValue(key="token.count", value=AnyValue(int_value=42)),
            KeyValue(key="temperature", value=AnyValue(double_value=0.7)),
            KeyValue(key="stream", value=AnyValue(bool_value=True)),
        ]
        span = decode_traces_protobuf(_pb_request([_pb_span(attributes=attrs)]).SerializeToString()).spans[0]
        assert dict(span.attributes) == {
            "gen_ai.request.model": "gpt-4",
            "token.count": 42,
            "temperature": 0.7,
            "stream": True,
        }

    def test_resource_and_scope_are_preserved(self):
        resource = [
            KeyValue(key="service.name", value=AnyValue(string_value="test-agent")),
            KeyValue(key="agentevals.eval_set_id", value=AnyValue(string_value="eval-1")),
        ]
        raw = _pb_request([_pb_span()], resource, "gcp.vertex.agent", "1.2.3").SerializeToString()
        span = decode_traces_protobuf(raw).spans[0]
        assert span.resource.attributes == {"service.name": "test-agent", "agentevals.eval_set_id": "eval-1"}
        assert (span.scope.name, span.scope.version) == ("gcp.vertex.agent", "1.2.3")

    def test_multiple_spans(self):
        raw = _pb_request([_pb_span(name="span-1"), _pb_span("2122232425262729", name="span-2")]).SerializeToString()
        assert {s.name for s in decode_traces_protobuf(raw).spans} == {"span-1", "span-2"}

    def test_empty_requests(self):
        assert decode_traces_protobuf(TraceServiceRequestPB().SerializeToString()).spans == []
        assert decode_logs_protobuf(LogsServiceRequestPB().SerializeToString()).logs == []

    def test_log_roundtrip(self):
        log = decode_logs_protobuf(_pb_log_request().SerializeToString()).logs[0]
        assert (log.trace_id, log.span_id, log.event_name) == (TRACE_ID_HEX, SPAN_ID_HEX, "gen_ai.user.message")

    def test_protobuf_and_json_decode_to_the_same_span(self):
        from google.protobuf.json_format import MessageToDict

        request = _pb_request([_pb_span()], [KeyValue(key="service.name", value=AnyValue(string_value="svc"))], "s")
        as_json = MessageToDict(request)
        for rs in as_json["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for sp in ss["spans"]:
                    for key in ("traceId", "spanId", "parentSpanId"):
                        sp[key] = __import__("base64").b64decode(sp[key]).hex()
        assert decode_traces_protobuf(request.SerializeToString()).spans == decode_traces_json(as_json).spans


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------


class TestSessionMetadata:
    def test_excludes_agentevals_attributes(self):
        meta = session_metadata({"agentevals.eval_set_id": "e1", "service.name": "my-agent", "deployment.env": "dev"})
        assert meta == {"service.name": "my-agent", "deployment.env": "dev"}

    def test_unprefixes_sdk_metadata(self):
        assert session_metadata({"agentevals.metadata.model": "gpt"}) == {"model": "gpt"}

    def test_eval_set_id_from_resource(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1", eval_set_id="eval1")))
        assert store.sessions["s1"].eval_set_id == "eval1"

    @pytest.mark.parametrize("value", [{"arrayValue": {"values": [{"stringValue": "a"}]}}, {"stringValue": "x" * 300}])
    def test_unusable_session_name_is_ignored(self, value):
        store, _ = _store()
        _ingest(store, _request([_chat()], [{"key": "agentevals.session_name", "value": value}]))
        assert list(store.sessions) == [f"otlp-{tid('t1')[:12]}"]

    def test_non_string_eval_set_id_is_ignored(self):
        store, _ = _store()
        attrs = [*_named("s1"), {"key": "agentevals.eval_set_id", "value": {"kvlistValue": {"values": []}}}]
        _ingest(store, _request([_span()], attrs))
        assert store.sessions["s1"].eval_set_id is None


# ---------------------------------------------------------------------------
# Routing (R1 to R3)
# ---------------------------------------------------------------------------


class TestRouting:
    def test_session_name_creates_session(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1", "eval1")))
        session = store.sessions["s1"]
        assert session.trace_ids == [tid("t1")]
        assert session.span_count == 1
        assert _events(store, "started") == [("started", "s1")]

    def test_same_trace_stays_in_its_session(self):
        store, _ = _store()
        _ingest(store, _request([_span(span="a")], _named("s1")))
        _ingest(store, _request([_span(span="b")], _named("s1")))
        assert list(store.sessions) == ["s1"]
        assert store.sessions["s1"].span_count == 2

    def test_new_trace_after_completed_name_starts_a_new_generation(self):
        store, clock = _store(rerun_window_seconds=30.0)
        _ingest(store, _request([_span(parent=None)], _named("s1")))
        clock.advance(10)
        assert store.tick() == ["s1"]
        clock.advance(30)
        _ingest(store, _request([_span(trace="t2", parent=None)], _named("s1")))
        assert list(store.sessions) == ["s1", "s1-2"]

    def test_unique_session_ids_across_runs(self):
        store, clock = _store(rerun_window_seconds=30.0)
        for n in range(3):
            _ingest(store, _request([_span(trace=f"t{n}", parent=None)], _named("my-agent")))
            clock.advance(10)
            store.tick()
            clock.advance(30)
        assert list(store.sessions) == ["my-agent", "my-agent-2", "my-agent-3"]

    def test_same_trace_reopens_a_complete_session(self):
        store, clock = _store()
        _ingest(store, _request([_span(span="a", parent=None)], _named("s1")))
        clock.advance(10)
        store.tick()
        _ingest(store, _request([_span(span="b", parent="a")], _named("s1")))
        assert list(store.sessions) == ["s1"]
        assert store.sessions["s1"].is_complete is False
        assert ("reopened", "s1") in store.events

    def test_retried_duplicate_span_does_not_reopen(self):
        store, clock = _store()
        _ingest(store, _request([_span(parent=None)], _named("s1")))
        clock.advance(10)
        store.tick()
        store.events.clear()
        _, results = _ingest(store, _request([_span(parent=None)], _named("s1")))
        assert results[0].accepted == 1
        assert store.sessions["s1"].is_complete is True
        assert store.events == []

    def test_conversation_id_rejoins_a_complete_session(self):
        store, clock = _store()
        conv = [_attr("gen_ai.conversation.id", "conv-1")]
        _ingest(store, _request([_span(parent=None, attributes=conv)]))
        clock.advance(10)
        assert store.tick() == ["conv-1"]
        _ingest(store, _request([_span(trace="t2", parent=None, attributes=conv)]))
        assert list(store.sessions) == ["conv-1"]
        assert store.sessions["conv-1"].trace_ids == [tid("t1"), tid("t2")]
        assert store.sessions["conv-1"].is_complete is False

    def test_new_run_id_starts_a_new_generation(self):
        store, _ = _store()
        _ingest(store, _request([_span()], [*_named("s1"), _attr("agentevals.session.run_id", "run-a")]))
        _ingest(store, _request([_span(trace="t2")], [*_named("s1"), _attr("agentevals.session.run_id", "run-b")]))
        assert list(store.sessions) == ["s1", "s1-2"]

    def test_unkeyed_genai_trace_becomes_provisional(self):
        store, _ = _store()
        _ingest(store, _request([_chat(trace="abcdef")]))
        session_id = f"otlp-{tid('abcdef')[:12]}"
        assert list(store.sessions) == [session_id]
        assert store.sessions[session_id].provisional

    def test_unkeyed_non_genai_trace_is_staged_until_genai_arrives(self):
        store, _ = _store()
        _ingest(store, _request([_span(span="http", name="GET /")]))
        assert store.sessions == {}
        assert tid("t1") in store.staged
        _ingest(store, _request([_chat(span="c1", parent="http")]))
        session = next(iter(store.sessions.values()))
        assert session.span_count == 2
        assert tid("t1") not in store.staged

    def test_provisional_session_is_absorbed_when_its_key_arrives(self):
        store, _ = _store()
        _ingest(store, _request([_chat(span="c1")]))
        provisional = next(iter(store.sessions))
        _ingest(store, _request([_span(span="agent", parent=None)], _named("named")))
        assert list(store.sessions) == ["named"]
        assert ("removed", provisional, "named") in store.events
        assert store.sessions["named"].span_count == 2

    def test_traces_with_the_same_name_group_into_one_session(self):
        store, _ = _store()
        _ingest(store, _request([_span(trace="a")], _named("my-session")))
        _ingest(store, _request([_span(trace="b")], _named("my-session")))
        assert list(store.sessions) == ["my-session"]
        assert store.sessions["my-session"].trace_ids == [tid("a"), tid("b")]

    def test_different_unkeyed_traces_get_different_sessions(self):
        store, _ = _store()
        body = {
            "resourceSpans": [
                *_request([_chat(trace="t1")])["resourceSpans"],
                *_request([_chat(trace="t2")])["resourceSpans"],
            ]
        }
        _ingest(store, body)
        assert len(store.sessions) == 2

    def test_keys_resolve_per_trace_inside_one_resource(self):
        store, _ = _store()
        conv_a = [_attr("gen_ai.conversation.id", "conv-a")]
        conv_b = [_attr("gen_ai.conversation.id", "conv-b")]
        _ingest(store, _request([_span(trace="a", attributes=conv_a), _span(trace="b", attributes=conv_b)]))
        assert sorted(store.sessions) == ["conv-a", "conv-b"]

    def test_empty_request(self):
        store, _ = _store()
        _ingest(store, {"resourceSpans": []})
        assert store.sessions == {}


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_root_span_completes_after_the_grace_period(self):
        store, clock = _store(completion_grace_seconds=3.0)
        _ingest(store, _request([_span(parent=None)], _named("s1")))
        clock.advance(2.9)
        assert store.tick() == []
        clock.advance(0.2)
        assert store.tick() == ["s1"]
        assert store.sessions["s1"].is_complete

    def test_trace_without_root_completes_after_idle_timeout(self):
        store, clock = _store(idle_timeout_seconds=30.0)
        _ingest(store, _request([_span(parent="elsewhere")], _named("s1")))
        clock.advance(10)
        assert store.tick() == []
        clock.advance(21)
        assert store.tick() == ["s1"]

    def test_remote_parent_counts_as_local_root(self):
        store, clock = _store(completion_grace_seconds=3.0, idle_timeout_seconds=30.0)
        _ingest(store, _request([_span(parent="remote", flags=0x300)], _named("s1")))
        clock.advance(4)
        assert store.tick() == ["s1"]

    def test_each_new_span_pushes_the_deadline(self):
        store, clock = _store(completion_grace_seconds=3.0)
        _ingest(store, _request([_span(span="a", parent=None)], _named("s1")))
        clock.advance(2)
        _ingest(store, _request([_span(span="b", parent="a")], _named("s1")))
        clock.advance(2)
        assert store.tick() == []
        clock.advance(1.5)
        assert store.tick() == ["s1"]

    def test_session_waits_for_every_span_bearing_trace(self):
        store, clock = _store(completion_grace_seconds=3.0, idle_timeout_seconds=30.0)
        _ingest(store, _request([_span(trace="a", parent=None)], _named("s1")))
        _ingest(store, _request([_span(trace="b", parent="elsewhere")], _named("s1")))
        clock.advance(4)
        assert store.tick() == []
        clock.advance(30)
        assert store.tick() == ["s1"]

    def test_logs_never_reopen_a_complete_session(self):
        store, clock = _store()
        _ingest(store, _request([_span(parent=None)], _named("s1")))
        clock.advance(10)
        store.tick()
        store.events.clear()
        results = _ingest_logs(store, _log_request([_log()]))
        assert results[0].accepted == 1
        assert store.sessions["s1"].is_complete is True
        assert store.sessions["s1"].log_count == 1
        assert ("dirty", "s1") in store.events

    def test_expired_sessions_are_removed(self):
        store, clock = _store(session_ttl_seconds=60.0)
        _ingest(store, _request([_span(parent=None)], _named("s1")))
        clock.advance(10)
        store.tick()
        clock.advance(61)
        store.expire()
        assert store.sessions == {}
        assert ("removed", "s1", None) in store.events


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_log_routes_to_its_trace_session(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1")))
        _ingest_logs(store, _log_request([_log()]))
        assert store.sessions["s1"].log_count == 1
        assert store.traces[tid("t1")].logs[0].event_name == "gen_ai.user.message"

    def test_logs_of_every_member_trace_reach_the_session(self):
        store, _ = _store()
        _ingest(store, _request([_span(trace="a")], _named("my-session")))
        _ingest(store, _request([_span(trace="b")], _named("my-session")))
        _ingest_logs(store, _log_request([_log(trace="a"), _log(trace="b")]))
        assert store.sessions["my-session"].log_count == 2

    def test_unkeyed_log_for_unknown_trace_waits_and_is_replayed(self):
        store, _ = _store()
        _ingest_logs(store, _log_request([_log(trace="t1", span="c1"), _log(trace="t1", span="c1", timeUnixNano="5")]))
        assert store.sessions == {}
        assert store.pending.count == 2
        _ingest(store, _request([_chat(span="c1")]))
        session = next(iter(store.sessions.values()))
        assert session.log_count == 2
        assert store.pending.count == 0

    def test_expired_pending_logs_are_not_replayed(self):
        store, clock = _store(pending_log_ttl_seconds=300.0)
        _ingest_logs(store, _log_request([_log()]))
        clock.advance(301)
        store.expire()
        _ingest(store, _request([_chat()]))
        assert next(iter(store.sessions.values())).log_count == 0

    def test_full_pending_buffer_rejects_the_newcomer(self):
        store, _ = _store(pending_logs=1)
        results = _ingest_logs(store, _log_request([_log(trace="a"), _log(trace="b")]))
        assert sum(r.accepted for r in results) == 1
        assert sum(r.rejected for r in results) == 1

    def test_keyed_log_for_unknown_trace_joins_its_session(self):
        store, _ = _store()
        _ingest(store, _request([_span(trace="t1")], _named("my-agent")))
        _ingest_logs(store, _log_request([_log(trace="other")], _named("my-agent")))
        assert store.sessions["my-agent"].trace_ids == [tid("t1"), tid("other")]
        assert store.sessions["my-agent"].log_count == 1

    def test_keyed_log_for_new_trace_does_not_attach_to_a_finished_named_session(self):
        store, clock = _store(rerun_window_seconds=30.0)
        _ingest(store, _request([_span(parent=None)], _named("named-session")))
        clock.advance(10)
        store.tick()
        clock.advance(30)
        _ingest_logs(store, _log_request([_log(trace="new")], _named("named-session")))
        assert store.sessions["named-session"].log_count == 0
        assert tid("new") not in store.sessions["named-session"].trace_ids
        assert store.sessions["named-session-2"].trace_ids == [tid("new")]

    def test_non_genai_logs_are_accepted_but_not_stored(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1")))
        results = _ingest_logs(store, _log_request([_log(event="http.server.request")]))
        assert (results[0].accepted, results[0].rejected) == (1, 0)
        assert store.sessions["s1"].log_count == 0

    def test_evaluation_results_from_agentevals_are_dropped(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1")))
        record = _log(event="gen_ai.evaluation.result")
        results = _ingest_logs(store, _log_request([record], scope_name="agentevals"))
        assert results[0].rejected == 1
        assert store.sessions["s1"].log_count == 0

    def test_evaluation_results_from_this_instance_are_dropped(self):
        clock = FakeClock()
        store = TelemetryStore(clock=clock, instance_id="me")
        _ingest(store, _request([_span()], _named("s1")))
        results = _ingest_logs(store, _log_request([_log()], [_attr("service.instance.id", "me")]))
        assert results[0].rejected == 1

    def test_third_party_evaluation_results_are_kept(self):
        store, _ = _store()
        _ingest(store, _request([_span()], _named("s1")))
        _ingest_logs(store, _log_request([_log(event="gen_ai.evaluation.result")], scope_name="other-evaluator"))
        assert store.sessions["s1"].log_count == 1


# ---------------------------------------------------------------------------
# Limits and partial success
# ---------------------------------------------------------------------------


def _manager(**limits) -> LiveManager:
    return LiveManager(Limits(**limits))


class TestExportResultCounts:
    async def test_full_success_reports_nothing_rejected(self):
        result = await ingest_traces(decode_traces_json(_request([_span()], _named("ok"))), _manager())
        assert (result.accepted, result.rejected, result.error_message) == (1, 0, "")

    async def test_trace_span_limit_is_counted_as_rejected(self):
        mgr = _manager(spans_per_trace=1)
        await ingest_traces(decode_traces_json(_request([_span(span="a")], _named("lim"))), mgr)
        result = await ingest_traces(decode_traces_json(_request([_span(span="b")], _named("lim"))), mgr)
        assert (result.accepted, result.rejected) == (0, 1)
        assert "trace span limit" in result.error_message

    async def test_session_span_limit_is_counted_as_rejected(self):
        mgr = _manager(spans_per_session=1)
        await ingest_traces(decode_traces_json(_request([_span(trace="a")], _named("lim"))), mgr)
        result = await ingest_traces(decode_traces_json(_request([_span(trace="b")], _named("lim"))), mgr)
        assert result.rejected == 1
        assert "session span limit" in result.error_message

    async def test_span_without_trace_id_is_counted_as_rejected(self):
        data = _span()
        data["traceId"] = ""
        result = await ingest_traces(decode_traces_json(_request([data])), _manager())
        assert (result.accepted, result.rejected) == (0, 1)
        assert "invalid trace or span id" in result.error_message

    async def test_log_limit_is_counted_as_rejected(self):
        mgr = _manager(logs_per_trace=1)
        await ingest_traces(decode_traces_json(_request([_span()], _named("lim"))), mgr)
        await ingest_logs(decode_logs_json(_log_request([_log()])), mgr)
        result = await ingest_logs(decode_logs_json(_log_request([_log(timeUnixNano="5")])), mgr)
        assert result.rejected == 1
        assert "trace log limit" in result.error_message

    async def test_filtered_logs_are_not_counted_as_rejected(self):
        mgr = _manager()
        await ingest_traces(decode_traces_json(_request([_span()], _named("f"))), mgr)
        result = await ingest_logs(decode_logs_json(_log_request([_log(event="http.server.request")])), mgr)
        assert (result.rejected, result.error_message) == (0, "")

    async def test_permanent_caps_are_partial_success_not_overload(self):
        mgr = _manager(spans_per_trace=1)
        await ingest_traces(decode_traces_json(_request([_span(span="a")], _named("lim"))), mgr)
        result = await ingest_traces(decode_traces_json(_request([_span(span="b")], _named("lim"))), mgr)
        assert (result.rejected, result.overloaded) == (1, False)

    async def test_capacity_refusal_of_every_item_is_overload(self):
        mgr = _manager(max_sessions=1)
        await ingest_traces(decode_traces_json(_request([_span()], _named("a"))), mgr)
        result = await ingest_traces(decode_traces_json(_request([_span(trace="t2")], _named("b"))), mgr)
        assert result.overloaded is True


# ---------------------------------------------------------------------------
# Live manager: SSE and recompute
# ---------------------------------------------------------------------------


def _drain(client) -> list[dict]:
    out = []
    while not client.queue.empty():
        out.append(client.queue.get_nowait())
    return out


async def _settle(mgr: LiveManager) -> None:
    for _ in range(50):
        mgr.tick()
        if not mgr._running and not mgr._pending:
            return
        await asyncio.sleep(0.02)


class TestLiveManager:
    async def test_ingest_broadcasts_session_started_and_span_stubs(self):
        mgr = LiveManager()
        client = mgr.register_sse_client()
        await mgr.ingest_spans(decode_traces_json(_request([_chat()], _named("s1", "e1"))))
        events = _drain(client)
        started = next(e for e in events if e["type"] == "session_started")
        assert (started["session"]["sessionId"], started["session"]["evalSetId"]) == ("s1", "e1")
        received = next(e for e in events if e["type"] == "span_received")
        assert received["span"] == {"traceId": tid("t1"), "spanId": sid("c1"), "parentSpanId": None, "name": "chat gpt"}
        assert "attributes" not in received["span"]

    async def test_recompute_streams_elements_then_session_complete(self):
        mgr = LiveManager(completion_grace_seconds=0.0)
        client = mgr.register_sse_client()
        await mgr.ingest_spans(decode_traces_json(_request([_chat(text="Roll", reply="Rolled 4")], _named("s1"))))
        await asyncio.sleep(0.01)
        await _settle(mgr)
        events = _drain(client)
        kinds = [e["type"] for e in events]
        assert kinds.index("user_input") < kinds.index("session_complete")
        complete = next(e for e in events if e["type"] == "session_complete")
        assert complete["invocations"][0]["userText"] == "Roll"
        assert complete["invocations"][0]["agentText"] == "Rolled 4"
        assert complete["invocations"][0]["invocationId"] == sid("c1")
        assert sum(e["inputTokens"] for e in events if e["type"] == "token_update") == 10
        assert mgr.sessions["s1"].invocations == complete["invocations"]

    async def test_reopened_session_sends_only_session_complete(self):
        mgr = LiveManager(completion_grace_seconds=0.0)
        client = mgr.register_sse_client()
        await mgr.ingest_spans(decode_traces_json(_request([_chat(span="c1")], _named("s1"))))
        await asyncio.sleep(0.01)
        await _settle(mgr)
        _drain(client)
        await mgr.ingest_spans(decode_traces_json(_request([_chat(trace="t1", span="c2", text="Again")], _named("s1"))))
        await asyncio.sleep(0.01)
        await _settle(mgr)
        kinds = {e["type"] for e in _drain(client)}
        assert kinds <= {"span_received", "session_complete"}
        assert "session_complete" in kinds

    async def test_slow_sse_client_is_dropped(self):
        mgr = LiveManager()
        client = mgr.register_sse_client()
        for n in range(1001):
            mgr.broadcast({"type": "noise", "n": n})
        assert client.dropped is True
        assert client not in mgr.clients
        assert client.queue.get_nowait() is None

    async def test_debug_load_session_is_complete_and_listed(self):
        mgr = LiveManager()
        client = mgr.register_sse_client()
        decoded = decode_traces_json(_request([_chat()]))
        session = mgr.load_session("bundle", decoded.spans, [], eval_set_id="e")
        await _settle(mgr)
        assert session.is_complete and session.session_id == "bundle"
        kinds = [e["type"] for e in _drain(client)]
        assert kinds == ["session_started", "session_complete"]

    async def test_shutdown_stops_the_ticker(self):
        mgr = LiveManager()
        mgr.start()
        ticker = mgr._ticker
        await mgr.shutdown()
        assert ticker.done()


# ---------------------------------------------------------------------------
# gRPC
# ---------------------------------------------------------------------------


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
            create_otlp_grpc_server(host="127.0.0.1", port=4317, manager=MagicMock())


class _FakeGrpcServer:
    def __init__(self):
        self.grace_values: list[float | None] = []

    async def stop(self, grace: float | None) -> None:
        self.grace_values.append(grace)


class TestInstallSharedExitHandler:
    async def test_first_sigint_gracefully_stops_grpc(self):
        server_a = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
        server_b = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
        grpc_server = _FakeGrpcServer()
        _install_shared_exit_handler(server_a, server_b, grpc_server=grpc_server)

        server_a.handle_exit(signal.SIGINT, None)
        await asyncio.sleep(0)

        assert server_a.should_exit is True and server_b.should_exit is True
        assert server_a.force_exit is False and server_b.force_exit is False
        assert grpc_server.grace_values == [GRPC_SHUTDOWN_GRACE_SECONDS]

    async def test_second_sigint_force_stops_grpc(self):
        server_a = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
        server_b = SimpleNamespace(should_exit=False, force_exit=False, handle_exit=None)
        grpc_server = _FakeGrpcServer()
        _install_shared_exit_handler(server_a, server_b, grpc_server=grpc_server)

        server_a.handle_exit(signal.SIGINT, None)
        await asyncio.sleep(0)
        server_a.handle_exit(signal.SIGINT, None)
        await asyncio.sleep(0)

        assert server_a.force_exit is True and server_b.force_exit is True
        assert grpc_server.grace_values == [GRPC_SHUTDOWN_GRACE_SECONDS, 0]


def _pb_named(name: str, eval_set_id: str | None = None) -> list[KeyValue]:
    attrs = [KeyValue(key="agentevals.session_name", value=AnyValue(string_value=name))]
    if eval_set_id:
        attrs.append(KeyValue(key="agentevals.eval_set_id", value=AnyValue(string_value=eval_set_id)))
    return attrs


class TestGrpcServices:
    async def test_trace_export_creates_session(self):
        mgr = LiveManager()
        response = await OtlpTraceService(mgr).Export(_pb_request([_pb_span()], _pb_named("grpc", "grpc-eval")), None)
        assert isinstance(response, trace_service_pb2.ExportTraceServiceResponse)
        session = mgr.sessions["grpc"]
        assert session.eval_set_id == "grpc-eval"
        assert session.trace_ids == [TRACE_ID_HEX]
        assert session.span_count == 1

    async def test_logs_export_attaches_to_existing_session(self):
        mgr = LiveManager()
        await OtlpTraceService(mgr).Export(_pb_request([_pb_span()], _pb_named("grpc-logs")), None)
        response = await OtlpLogsService(mgr).Export(_pb_log_request(), None)
        assert isinstance(response, logs_service_pb2.ExportLogsServiceResponse)
        assert mgr.sessions["grpc-logs"].log_count == 1

    async def test_trace_export_reports_rejected_spans(self):
        mgr = LiveManager(Limits(spans_per_trace=1))
        service = OtlpTraceService(mgr)
        await service.Export(_pb_request([_pb_span()], _pb_named("grpc-limit")), None)
        response = await service.Export(_pb_request([_pb_span("3132333435363738")], _pb_named("grpc-limit")), None)
        assert response.partial_success.rejected_spans == 1
        assert response.partial_success.error_message

    async def test_trace_export_full_success_leaves_partial_success_unset(self):
        response = await OtlpTraceService(LiveManager()).Export(_pb_request([_pb_span()], _pb_named("ok")), None)
        assert not response.HasField("partial_success")
