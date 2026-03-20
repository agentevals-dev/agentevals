"""Tests for log-to-span enrichment logic."""

import json

from agentevals.utils.log_enrichment import enrich_spans_with_logs


def _make_span(span_id: str = "span1", attrs: list | None = None) -> dict:
    return {
        "spanId": span_id,
        "name": "chat",
        "attributes": attrs or [],
    }


def _make_log(event_name: str, body: dict, span_id: str | None = None) -> dict:
    log = {
        "event_name": event_name,
        "timestamp": 1000000000,
        "body": body,
        "attributes": {"event.name": event_name},
    }
    if span_id:
        log["span_id"] = span_id
    return log


def _get_injected_attr(span: dict, key: str, parse_json: bool = True):
    for attr in span.get("attributes", []):
        if attr.get("key") == key:
            raw = attr["value"]["stringValue"]
            return json.loads(raw) if parse_json else raw
    return None


class TestBroadcastEnrichment:
    """Legacy path: logs without span_id → all messages into every span."""

    def test_user_message_injected(self):
        spans = [_make_span()]
        logs = [_make_log("gen_ai.user.message", {"content": "hello"})]
        result = enrich_spans_with_logs(spans, logs)

        msgs = _get_injected_attr(result[0], "gen_ai.input.messages")
        assert msgs == [{"role": "user", "content": "hello"}]

    def test_assistant_message_injected(self):
        spans = [_make_span()]
        logs = [_make_log("gen_ai.assistant.message", {"content": "hi there"})]
        result = enrich_spans_with_logs(spans, logs)

        msgs = _get_injected_attr(result[0], "gen_ai.output.messages")
        assert msgs == [{"role": "assistant", "content": "hi there"}]

    def test_all_messages_broadcast_to_all_spans(self):
        spans = [_make_span("s1"), _make_span("s2")]
        logs = [
            _make_log("gen_ai.user.message", {"content": "hello"}),
            _make_log("gen_ai.assistant.message", {"content": "hi"}),
        ]
        result = enrich_spans_with_logs(spans, logs)

        for enriched in result:
            assert _get_injected_attr(enriched, "gen_ai.input.messages") is not None
            assert _get_injected_attr(enriched, "gen_ai.output.messages") is not None

    def test_no_logs_returns_original(self):
        spans = [_make_span()]
        result = enrich_spans_with_logs(spans, [])
        assert result is spans

    def test_deduplicates_user_messages(self):
        spans = [_make_span()]
        logs = [
            _make_log("gen_ai.user.message", {"content": "hello"}),
            _make_log("gen_ai.user.message", {"content": "hello"}),
        ]
        result = enrich_spans_with_logs(spans, logs)
        msgs = _get_injected_attr(result[0], "gen_ai.input.messages")
        assert len(msgs) == 1

    def test_session_id_injected(self):
        spans = [_make_span()]
        logs = [_make_log("gen_ai.user.message", {"content": "hi"})]
        result = enrich_spans_with_logs(spans, logs, session_id="my-session")

        agent_name = _get_injected_attr(result[0], "gen_ai.agent.name", parse_json=False)
        assert agent_name == "my-session"

    def test_choice_extracts_nested_content(self):
        spans = [_make_span()]
        logs = [_make_log("gen_ai.choice", {"message": {"content": "reply"}})]
        result = enrich_spans_with_logs(spans, logs)

        msgs = _get_injected_attr(result[0], "gen_ai.output.messages")
        assert msgs[0]["content"] == "reply"

    def test_choice_extracts_tool_calls_from_nested_message(self):
        spans = [_make_span()]
        logs = [
            _make_log(
                "gen_ai.choice",
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "roll_die", "arguments": "{}"}},
                        ],
                    },
                },
            )
        ]
        result = enrich_spans_with_logs(spans, logs)

        msgs = _get_injected_attr(result[0], "gen_ai.output.messages")
        assert len(msgs) == 1
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "roll_die"


class TestPerSpanEnrichment:
    """OTLP path: logs with span_id → matched to specific spans."""

    def test_logs_matched_to_correct_span(self):
        spans = [_make_span("s1"), _make_span("s2")]
        logs = [
            _make_log("gen_ai.user.message", {"content": "question 1"}, span_id="s1"),
            _make_log("gen_ai.assistant.message", {"content": "answer 1"}, span_id="s1"),
            _make_log("gen_ai.user.message", {"content": "question 2"}, span_id="s2"),
            _make_log("gen_ai.assistant.message", {"content": "answer 2"}, span_id="s2"),
        ]
        result = enrich_spans_with_logs(spans, logs)

        msgs_s1 = _get_injected_attr(result[0], "gen_ai.input.messages")
        assert msgs_s1 == [{"role": "user", "content": "question 1"}]

        msgs_s2 = _get_injected_attr(result[1], "gen_ai.input.messages")
        assert msgs_s2 == [{"role": "user", "content": "question 2"}]

    def test_span_without_logs_not_enriched(self):
        spans = [_make_span("s1"), _make_span("s2")]
        logs = [
            _make_log("gen_ai.user.message", {"content": "hello"}, span_id="s1"),
        ]
        result = enrich_spans_with_logs(spans, logs)

        assert _get_injected_attr(result[0], "gen_ai.input.messages") is not None
        assert _get_injected_attr(result[1], "gen_ai.input.messages") is None

    def test_session_id_on_all_spans(self):
        spans = [_make_span("s1"), _make_span("s2")]
        logs = [
            _make_log("gen_ai.user.message", {"content": "hello"}, span_id="s1"),
        ]
        result = enrich_spans_with_logs(spans, logs, session_id="test")

        assert _get_injected_attr(result[0], "gen_ai.agent.name", parse_json=False) == "test"
        assert _get_injected_attr(result[1], "gen_ai.agent.name", parse_json=False) == "test"

    def test_tool_calls_in_assistant_message(self):
        spans = [_make_span("s1")]
        logs = [
            _make_log(
                "gen_ai.assistant.message",
                {
                    "content": "",
                    "tool_calls": [{"id": "tc1", "function": {"name": "roll", "arguments": "{}"}}],
                },
                span_id="s1",
            ),
        ]
        result = enrich_spans_with_logs(spans, logs)

        msgs = _get_injected_attr(result[0], "gen_ai.output.messages")
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "roll"

    def test_multiple_logs_per_span(self):
        """A multi-turn LLM call has multiple user+assistant log pairs."""
        spans = [_make_span("s1")]
        logs = [
            _make_log("gen_ai.user.message", {"content": "hi"}, span_id="s1"),
            _make_log(
                "gen_ai.assistant.message",
                {"content": "", "tool_calls": [{"id": "t1", "function": {"name": "roll", "arguments": "{}"}}]},
                span_id="s1",
            ),
            _make_log("gen_ai.user.message", {"content": "thanks"}, span_id="s1"),
            _make_log("gen_ai.assistant.message", {"content": "you're welcome"}, span_id="s1"),
        ]
        result = enrich_spans_with_logs(spans, logs)

        input_msgs = _get_injected_attr(result[0], "gen_ai.input.messages")
        output_msgs = _get_injected_attr(result[0], "gen_ai.output.messages")
        assert len(input_msgs) == 2
        assert len(output_msgs) == 2
