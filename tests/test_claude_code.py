"""Tests for Claude Code OTEL log ingestion, hook receiver, and invocation conversion."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentevals.api.otlp_routes import _convert_otlp_log_record, _is_claude_code_log
from agentevals.claude_code_converter import (
    _ClaudeCodeTurn,
    _get_timestamp,
    _group_logs_into_turns,
    _parse_tool_parameters,
    _turn_to_invocation,
    convert_claude_code_session,
)
from agentevals.streaming.incremental_processor import ClaudeCodeIncrementalExtractor


def _run(coro):
    return asyncio.run(coro)


def _make_otlp_attr(key: str, value, value_type: str = "stringValue") -> dict:
    return {"key": key, "value": {value_type: value}}


def _make_cc_log_record(event_name: str, attrs: dict, body=None, trace_id: str = "trace1") -> dict:
    """Build an OTLP log record with Claude Code event attributes."""
    otlp_attrs = [_make_otlp_attr("event.name", event_name)]
    for k, v in attrs.items():
        if isinstance(v, int):
            otlp_attrs.append(_make_otlp_attr(k, str(v), "intValue"))
        elif isinstance(v, float):
            otlp_attrs.append(_make_otlp_attr(k, v, "doubleValue"))
        elif isinstance(v, bool):
            otlp_attrs.append(_make_otlp_attr(k, v, "boolValue"))
        else:
            otlp_attrs.append(_make_otlp_attr(k, str(v)))

    record = {
        "traceId": trace_id,
        "timeUnixNano": "1700000000000000000",
        "attributes": otlp_attrs,
    }
    if body is not None:
        record["body"] = {"stringValue": body} if isinstance(body, str) else body
    return record


def _make_cc_log_event(event_name: str, attrs: dict, timestamp=1700000000000000000) -> dict:
    """Build a converted internal log event (post _convert_otlp_log_record)."""
    return {
        "event_name": event_name,
        "timestamp": timestamp,
        "body": {},
        "attributes": attrs,
    }


class TestOtlpLogFilter:
    """Test that the OTLP log filter accepts claude_code.* events."""

    def test_accepts_claude_code_user_prompt(self):
        record = _make_cc_log_record("claude_code.user_prompt", {"prompt.id": "p1"})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.user_prompt"

    def test_accepts_claude_code_tool_result(self):
        record = _make_cc_log_record("claude_code.tool_result", {"tool_name": "Bash"})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.tool_result"

    def test_accepts_claude_code_api_request(self):
        record = _make_cc_log_record("claude_code.api_request", {"model": "opus"})
        result = _convert_otlp_log_record(record)
        assert result is not None

    def test_still_accepts_genai_events(self):
        record = _make_cc_log_record("gen_ai.user.message", {})
        result = _convert_otlp_log_record(record)
        assert result is not None

    def test_rejects_unknown_event(self):
        record = _make_cc_log_record("some.other.event", {})
        result = _convert_otlp_log_record(record)
        assert result is None

    def test_is_claude_code_log(self):
        cc_log = {"event_name": "claude_code.user_prompt"}
        assert _is_claude_code_log(cc_log) is True

        genai_log = {"event_name": "gen_ai.user.message"}
        assert _is_claude_code_log(genai_log) is False

    def test_preserves_attributes(self):
        record = _make_cc_log_record(
            "claude_code.user_prompt",
            {"prompt.id": "p1", "session.id": "sess1", "prompt": "hello"},
        )
        result = _convert_otlp_log_record(record)
        assert result["attributes"]["prompt.id"] == "p1"
        assert result["attributes"]["session.id"] == "sess1"
        assert result["attributes"]["prompt"] == "hello"

    def test_accepts_short_form_user_prompt(self):
        record = _make_cc_log_record("user_prompt", {"prompt.id": "p1"})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.user_prompt"

    def test_accepts_short_form_tool_result(self):
        record = _make_cc_log_record("tool_result", {"tool_name": "Bash"})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.tool_result"

    def test_accepts_short_form_api_request(self):
        record = _make_cc_log_record("api_request", {"model": "opus"})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.api_request"

    def test_accepts_short_form_api_error(self):
        record = _make_cc_log_record("api_error", {})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.api_error"

    def test_accepts_short_form_tool_decision(self):
        record = _make_cc_log_record("tool_decision", {})
        result = _convert_otlp_log_record(record)
        assert result is not None
        assert result["event_name"] == "claude_code.tool_decision"


class TestClaudeCodeConverter:
    """Test the Claude Code log-to-Invocation converter."""

    def test_empty_logs(self):
        result = convert_claude_code_session([], [])
        assert len(result.invocations) == 0
        assert any("No invocations" in w for w in result.warnings)

    def test_single_turn_with_prompt(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Fix the bug",
                "event.sequence": 1,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 1
        inv = result.invocations[0]
        assert inv.invocation_id == "cc-p1"
        assert inv.user_content.parts[0].text == "Fix the bug"

    def test_single_turn_without_prompt_text(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt_length": 42,
                "event.sequence": 1,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 1
        assert "42 chars" in result.invocations[0].user_content.parts[0].text

    def test_turn_with_tool_calls(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Fix it",
                "event.sequence": 1,
            }),
            _make_cc_log_event("claude_code.tool_result", {
                "prompt.id": "p1",
                "tool_name": "Read",
                "success": "true",
                "duration_ms": 50,
                "tool_parameters": '{"file_path": "/src/main.py"}',
                "event.sequence": 2,
            }),
            _make_cc_log_event("claude_code.tool_result", {
                "prompt.id": "p1",
                "tool_name": "Edit",
                "success": "true",
                "duration_ms": 100,
                "tool_parameters": '{"file_path": "/src/main.py"}',
                "event.sequence": 3,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 1
        inv = result.invocations[0]
        assert len(inv.intermediate_data.tool_uses) == 2
        assert inv.intermediate_data.tool_uses[0].name == "Read"
        assert inv.intermediate_data.tool_uses[0].args == {"file_path": "/src/main.py"}
        assert inv.intermediate_data.tool_uses[1].name == "Edit"

    def test_turn_with_api_request(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Hello",
                "event.sequence": 1,
            }),
            _make_cc_log_event("claude_code.api_request", {
                "prompt.id": "p1",
                "model": "claude-opus-4-6",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": 0.05,
                "event.sequence": 2,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 1

    def test_multi_turn_conversation(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "First question",
                "event.sequence": 1,
            }),
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p2",
                "prompt": "Second question",
                "event.sequence": 10,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 2
        assert result.invocations[0].user_content.parts[0].text == "First question"
        assert result.invocations[1].user_content.parts[0].text == "Second question"

    def test_ordering_by_sequence(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p2",
                "prompt": "Second",
                "event.sequence": 10,
            }),
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "First",
                "event.sequence": 1,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert result.invocations[0].invocation_id == "cc-p1"
        assert result.invocations[1].invocation_id == "cc-p2"

    def test_logs_without_prompt_id_are_skipped(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt": "hello",
                "event.sequence": 1,
            }),
        ]
        result = convert_claude_code_session(logs)
        assert len(result.invocations) == 0


class TestHookEventMerging:
    """Test merging hook events with OTEL logs."""

    def test_stop_hook_provides_response(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Fix the bug",
                "event.sequence": 1,
            }),
        ]
        hooks = [
            {
                "hook_event_name": "Stop",
                "session_id": "sess1",
                "last_assistant_message": "I fixed the bug in auth.py by correcting the token validation.",
            },
        ]
        result = convert_claude_code_session(logs, hooks)
        assert len(result.invocations) == 1
        inv = result.invocations[0]
        assert "fixed the bug" in inv.final_response.parts[0].text

    def test_post_tool_use_hook_provides_tool_io(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Read main.py",
                "event.sequence": 1,
            }),
            _make_cc_log_event("claude_code.tool_result", {
                "prompt.id": "p1",
                "tool_name": "Read",
                "success": "true",
                "event.sequence": 2,
            }),
        ]
        hooks = [
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess1",
                "tool_name": "Read",
                "tool_input": {"file_path": "/src/main.py"},
                "tool_response": {"content": "def main(): pass"},
                "tool_use_id": "toolu_123",
            },
        ]
        result = convert_claude_code_session(logs, hooks)
        inv = result.invocations[0]
        assert len(inv.intermediate_data.tool_uses) == 1
        assert inv.intermediate_data.tool_uses[0].args == {"file_path": "/src/main.py"}
        assert len(inv.intermediate_data.tool_responses) == 1
        assert inv.intermediate_data.tool_responses[0].response == {"content": "def main(): pass"}

    def test_hooks_only_no_logs(self):
        hooks = [
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "file1.py"},
                "tool_use_id": "toolu_456",
            },
            {
                "hook_event_name": "Stop",
                "session_id": "sess1",
                "last_assistant_message": "Here are the files.",
            },
        ]
        result = convert_claude_code_session([], hooks)
        assert len(result.invocations) == 0

    def test_empty_response_when_no_stop_hook(self):
        logs = [
            _make_cc_log_event("claude_code.user_prompt", {
                "prompt.id": "p1",
                "prompt": "Hello",
                "event.sequence": 1,
            }),
        ]
        result = convert_claude_code_session(logs, [])
        inv = result.invocations[0]
        assert inv.final_response.parts[0].text == ""


class TestClaudeCodeIncrementalExtractor:
    """Test real-time extraction from Claude Code log events."""

    def test_user_prompt_extraction(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("claude_code.user_prompt", {
            "prompt.id": "p1",
            "prompt": "Fix the bug",
        })
        updates = extractor.process_log(log)
        assert len(updates) == 1
        assert updates[0]["type"] == "user_input"
        assert updates[0]["text"] == "Fix the bug"
        assert updates[0]["invocationId"] == "cc-p1"

    def test_user_prompt_dedup(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("claude_code.user_prompt", {
            "prompt.id": "p1",
            "prompt": "Fix the bug",
        })
        updates1 = extractor.process_log(log)
        updates2 = extractor.process_log(log)
        assert len(updates1) == 1
        assert len(updates2) == 0

    def test_user_prompt_fallback_to_length(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("claude_code.user_prompt", {
            "prompt.id": "p1",
            "prompt_length": 100,
        })
        updates = extractor.process_log(log)
        assert len(updates) == 1
        assert "100 chars" in updates[0]["text"]

    def test_tool_result_extraction(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("claude_code.tool_result", {
            "prompt.id": "p1",
            "tool_name": "Bash",
            "tool_parameters": '{"bash_command": "pytest"}',
            "event.sequence": 2,
        })
        updates = extractor.process_log(log)
        assert len(updates) == 1
        assert updates[0]["type"] == "tool_call"
        assert updates[0]["toolCall"]["name"] == "Bash"
        assert updates[0]["toolCall"]["args"]["bash_command"] == "pytest"

    def test_api_request_extraction(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("claude_code.api_request", {
            "prompt.id": "p1",
            "model": "claude-opus-4-6",
            "input_tokens": 500,
            "output_tokens": 200,
        })
        updates = extractor.process_log(log)
        assert len(updates) == 1
        assert updates[0]["type"] == "token_update"
        assert updates[0]["inputTokens"] == 500
        assert updates[0]["outputTokens"] == 200
        assert updates[0]["model"] == "claude-opus-4-6"

    def test_ignores_non_claude_code_events(self):
        extractor = ClaudeCodeIncrementalExtractor()
        log = _make_cc_log_event("gen_ai.user.message", {})
        updates = extractor.process_log(log)
        assert len(updates) == 0

    def test_process_span_returns_empty(self):
        extractor = ClaudeCodeIncrementalExtractor()
        assert extractor.process_span({"name": "test"}) == []

    def test_hook_post_tool_use(self):
        extractor = ClaudeCodeIncrementalExtractor()
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/src/main.py"},
            "tool_response": {"content": "def main(): pass"},
            "tool_use_id": "toolu_789",
            "timestamp": 1700000000000000000,
        }
        updates = extractor.process_hook_event(event)
        assert len(updates) == 2
        assert updates[0]["type"] == "tool_call"
        assert updates[0]["toolCall"]["name"] == "Read"
        assert updates[1]["type"] == "tool_result"
        assert updates[1]["toolName"] == "Read"

    def test_hook_stop(self):
        extractor = ClaudeCodeIncrementalExtractor()
        event = {
            "hook_event_name": "Stop",
            "last_assistant_message": "Done!",
            "timestamp": 1700000000000000000,
        }
        updates = extractor.process_hook_event(event)
        assert len(updates) == 1
        assert updates[0]["type"] == "agent_response"
        assert updates[0]["text"] == "Done!"


class TestParseToolParameters:
    def test_json_string(self):
        result = _parse_tool_parameters('{"command": "ls"}')
        assert result == {"command": "ls"}

    def test_dict(self):
        result = _parse_tool_parameters({"command": "ls"})
        assert result == {"command": "ls"}

    def test_invalid_json(self):
        result = _parse_tool_parameters("not json")
        assert result == {}

    def test_empty(self):
        result = _parse_tool_parameters("{}")
        assert result == {}


class TestGetTimestamp:
    def test_nanosecond(self):
        ts = _get_timestamp({"timestamp": 1700000000000000000})
        assert ts == pytest.approx(1700000000.0)

    def test_string_nanosecond(self):
        ts = _get_timestamp({"timestamp": "1700000000000000000"})
        assert ts == pytest.approx(1700000000.0)

    def test_none(self):
        assert _get_timestamp({}) == 0.0
