"""Session and turn views: what every client renders without deriving it."""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.protobuf import json_format
from opentelemetry.proto.trace.v1 import trace_pb2

from agentevals.api.v1alpha1 import views_pb2
from agentevals.api.views import (
    GAP_NO_TURNS,
    GAP_TRUNCATED,
    MAX_TEXT_CHARS,
    MAX_VALUE_BYTES,
    MAX_VALUE_DEPTH,
    SessionFacts,
    session_view,
    turn_view,
)
from agentevals.genai.extract import extract_conversation
from agentevals.genai.model import Gap, ToolCall, Turn, Usage
from agentevals.loader import load_telemetry, load_telemetry_text
from agentevals.otel.model import SpanRef

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "otlp"
FIXTURE = CORPUS / "hand" / "adk_go_main.json"
TRUTHS = sorted(CORPUS.rglob("*.truth.json"))
DOCUMENT = json.loads(FIXTURE.read_text())
TRACE_ID = "1dbd779ff94bf24c7b84678a5dc0346c"
RECEIVED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _facts(session_id: str = "s1", *, complete: bool = True, span_count: int = 5) -> SessionFacts:
    return SessionFacts(
        session_id=session_id,
        received_at=RECEIVED,
        is_complete=complete,
        trace_ids=(TRACE_ID,),
        span_count=span_count,
    )


def _session(document: dict = DOCUMENT, facts: SessionFacts | None = None) -> views_pb2.Session:
    traces = load_telemetry_text(json.dumps(document), format="otlp-json").traces
    conversation = extract_conversation(traces, "s1")
    spans = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
    return session_view(facts or _facts(), conversation, spans)


def _with_resource(**attributes: str) -> dict:
    document = copy.deepcopy(DOCUMENT)
    for resource_spans in document["resourceSpans"]:
        resource_spans["resource"]["attributes"] += [
            {"key": k, "value": {"stringValue": v}} for k, v in attributes.items()
        ]
    return document


def _turn(**fields) -> Turn:
    return Turn(ref=SpanRef(TRACE_ID, "eb86921785ed9606"), **fields)


def _user(text: str) -> list[dict]:
    return [{"role": "user", "parts": [{"type": "text", "content": text}]}]


class TestShortId:
    @pytest.mark.parametrize(
        ("session_id", "short_id"),
        [
            ("0199a3c2-6f1e-7c4b-9d2a-5e8f1a2b3c4d", "2b3c4d"),
            ("0199a3c26f1e7c4b9d2a5e8f1a2b3c4d", "2b3c4d"),
            (TRACE_ID, "c0346c"),
            ("o-code-review", "o-code-review"),
            ("o-code", "o-code"),
            ("abc123", "abc123"),
            ("deadbeefcafe", "deadbeefcafe"),
        ],
    )
    def test_only_generated_ids_are_shortened(self, session_id, short_id):
        assert session_view(_facts(session_id)).summary.short_id == short_id


class TestSummary:
    def test_totals_come_from_the_turns(self):
        summary = _session().summary
        totals = summary.totals
        assert (totals.turns, totals.failed_turns, totals.tool_calls) == (1, 0, 1)
        assert totals.model_calls == totals.model_calls_with_usage == 2
        assert totals.tool_calls_with_arguments == 1
        assert list(summary.models) == ["gemini-2.5-flash"]
        assert summary.first_user_text == "What is the weather in Paris?"
        assert summary.content_capture == views_pb2.CONTENT_CAPTURE_ON
        assert summary.state == views_pb2.SESSION_STATE_COMPLETE
        assert summary.conversation_id == "sess-go-1"

    def test_unrecorded_token_counts_stay_unset(self):
        session = _session()
        for usage in (session.summary.totals.usage, session.turns[0].usage):
            assert (usage.input_tokens, usage.output_tokens) == (90, 18)
            for name in ("cache_read_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"):
                assert not usage.HasField(name), name
        assert "cacheReadInputTokens" not in json_format.MessageToDict(session.summary.totals.usage)

    @pytest.mark.parametrize(
        ("resource", "agent"),
        [({}, "weather_agent"), ({"gen_ai.agent.name": "planner"}, "planner")],
    )
    def test_agent_prefers_the_resource_then_the_turn(self, resource, agent):
        assert _session(_with_resource(**resource)).summary.agent == agent

    def test_agent_falls_back_to_the_service(self):
        document = copy.deepcopy(DOCUMENT)
        for resource_spans in document["resourceSpans"]:
            for scope_spans in resource_spans["scopeSpans"]:
                for span in scope_spans["spans"]:
                    span["attributes"] = [a for a in span["attributes"] if a["key"] != "gen_ai.agent.name"]
        assert _session(document).summary.agent == "fixture-agent"

    def test_agent_version_falls_back_to_the_service_version(self):
        assert _session().summary.agent_version == "1.0.0"
        assert _session(_with_resource(**{"gen_ai.agent.version": "7"})).summary.agent_version == "7"

    def test_resource_is_carried_as_otlp(self):
        attributes = {a.key: a.value.string_value for a in _session().summary.resource.attributes}
        assert attributes == {"service.name": "fixture-agent", "service.version": "1.0.0"}

    def test_a_session_without_turns_is_described_by_its_facts(self):
        summary = session_view(_facts(complete=False)).summary
        assert summary.state == views_pb2.SESSION_STATE_LIVE
        assert summary.started_at.ToDatetime(tzinfo=UTC) == RECEIVED
        assert (summary.span_count, list(summary.trace_ids)) == (5, [TRACE_ID])
        assert not summary.HasField("first_user_text")
        assert list(summary.gaps) == []

    def test_a_complete_session_without_turns_says_so(self):
        [gap] = session_view(_facts()).summary.gaps
        assert gap.code == GAP_NO_TURNS

    def test_summary_only_leaves_the_turns_out(self):
        traces = load_telemetry_text(FIXTURE.read_text(), format="otlp-json").traces
        session = session_view(_facts(), extract_conversation(traces), {}, include_turns=False)
        assert (len(session.turns), session.summary.totals.turns) == (0, 1)


class TestTurn:
    def test_texts_and_status(self):
        view = turn_view(_turn(user_input=_user("hi"), status="error", error_type="TimeoutError"), {})
        assert (view.user_text, view.status, view.error_type) == (
            "hi",
            trace_pb2.Status.STATUS_CODE_ERROR,
            "TimeoutError",
        )
        assert not view.HasField("answer_text")

    def test_long_text_is_cut_with_a_gap(self):
        view = turn_view(_turn(user_input=_user("x" * (MAX_TEXT_CHARS + 10))), {})
        assert len(view.user_text) == MAX_TEXT_CHARS
        [gap] = view.gaps
        assert gap.code == GAP_TRUNCATED and "user text" in gap.reason

    def test_large_tool_result_becomes_cut_json_text(self):
        result = {"rows": ["y" * 1000 for _ in range(100)]}
        view = turn_view(_turn(tool_calls=[ToolCall(name="query", arguments={"q": 1}, result=result)]), {})
        [call] = view.tool_calls
        assert call.result.WhichOneof("kind") == "string_value"
        assert len(call.result.string_value.encode()) <= MAX_VALUE_BYTES
        assert json.dumps(result, separators=(",", ":")).startswith(call.result.string_value)
        assert json_format.MessageToDict(call.arguments) == {"q": 1}
        [gap] = view.gaps
        assert gap.code == GAP_TRUNCATED and "tool query result" in gap.reason

    def test_values_json_cannot_carry_are_replaced_with_a_gap(self):
        deep: dict = {}
        node = deep
        for _ in range(MAX_VALUE_DEPTH + 5):
            node["n"] = {}
            node = node["n"]
        arguments = {"nan": math.nan, "raw": b"\x01\x02", "big": 2**64, "deep": deep}
        view = turn_view(_turn(tool_calls=[ToolCall(name="t", arguments=arguments)]), {})
        args = json_format.MessageToDict(view.tool_calls[0].arguments)
        assert (args["nan"], args["raw"], args["big"]) == (None, "AQI=", str(2**64))
        assert json_format.MessageToJson(view)
        [gap] = view.gaps
        for reason in ("non finite", "base64", "2^53", "nesting"):
            assert reason in gap.reason

    def test_unknown_results_stay_unset(self):
        [call] = turn_view(_turn(tool_calls=[ToolCall(name="t")]), {}).tool_calls
        assert not call.HasField("arguments") and not call.HasField("result") and not call.HasField("call_id")

    def test_repeated_warnings_become_one_gap(self):
        gap = Gap(code="tool_result_by_order", reason="paired by order")
        view = turn_view(_turn(warnings=[gap, gap]), {})
        assert [(g.code, g.reason) for g in view.gaps] == [("tool_result_by_order", "paired by order")]


class TestUsage:
    def test_sum_keeps_unrecorded_counts_unset(self):
        total = Usage(input_tokens=5) + Usage(output_tokens=2) + Usage()
        assert total == Usage(input_tokens=5, output_tokens=2)

    def test_recorded_zero_is_kept(self):
        assert (Usage(input_tokens=0) + Usage()).input_tokens == 0


@pytest.mark.parametrize("truth_path", TRUTHS, ids=lambda p: str(p.relative_to(CORPUS)).removesuffix(".truth.json"))
def test_every_corpus_fixture_has_a_view_that_matches_its_truth(truth_path):
    truth = json.loads(truth_path.read_text())
    fixture = truth_path.with_name(truth_path.name.replace(".truth.json", ".json"))
    traces = load_telemetry(str(fixture)).traces
    conversation = extract_conversation(traces, "s1")
    spans = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
    session = session_view(_facts(), conversation, spans)

    assert json_format.Parse(json_format.MessageToJson(session), views_pb2.Session()) == session
    totals = session.summary.totals
    if truth.get("extra_turns_allowed"):
        assert totals.turns >= len(truth["turns"])
    else:
        assert totals.turns == len(truth["turns"]) == len(session.turns)
    for name, key in (("input_tokens", "input"), ("output_tokens", "output")):
        recorded = any(getattr(t.usage, name) is not None for t in conversation.turns)
        assert totals.usage.HasField(name) is recorded
        assert getattr(totals.usage, name) == truth["tokens"][key]
    assert totals.tool_calls == sum(len(t.tool_calls) for t in session.turns)
