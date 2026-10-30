"""ADK's informational efficiency metrics on invocations rebuilt from traces.

ADK fills token usage, model call events and turn timings while it runs an agent. agentevals
rebuilds invocations from telemetry, so the bridge has to supply the same fields, and the
metrics take no threshold because they never pass or fail.
"""

from __future__ import annotations

import os

import pytest
from google.adk.evaluation.eval_case import get_all_tool_calls

from agentevals.adk_bridge import model_call_events, to_adk_invocation
from agentevals.builtin_metrics import _to_invocation_events, evaluate_builtin_metric, metric_catalog
from agentevals.config import (
    INFORMATIONAL_METRICS,
    BuiltinMetricDef,
    apply_builtin_overrides,
    make_builtin_evaluator_entries,
)
from agentevals.custom_evaluators import evaluate_custom_evaluator
from agentevals.genai.extract import extract_conversation
from agentevals.genai.model import LlmCall, ToolCall, Turn, Usage
from agentevals.loader import load_traces
from agentevals.otel.model import SpanRef

HELM_TRACE = os.path.join(os.path.dirname(__file__), "..", "samples", "helm.json")

T0 = 1_700_000_000_000_000_000


def _ns(ms: float | None) -> int | None:
    return None if ms is None else T0 + int(ms * 1_000_000)


def _call(
    leaf: tuple[float, float],
    chain: tuple[float, float] | None = None,
    usage: Usage | None = None,
    model: str | None = "gpt-test",
    index: int = 0,
) -> LlmCall:
    start, end = chain or leaf
    return LlmCall(
        ref=SpanRef("t", f"llm{index}"),
        operation="chat",
        response_model=model,
        usage=usage,
        start_ns=_ns(start),
        end_ns=_ns(end),
        leaf_start_ns=_ns(leaf[0]),
        leaf_end_ns=_ns(leaf[1]),
    )


def _tool(start: float | None, end: float | None, name: str = "lookup") -> ToolCall:
    return ToolCall(name=name, call_id=name, arguments={}, result={"ok": True}, start_ns=_ns(start), end_ns=_ns(end))


def _turn(llm_calls=(), tool_calls=(), start: float = 0, end: float = 1000) -> Turn:
    return Turn(
        ref=SpanRef("t", "turn"),
        user_input=[{"role": "user", "parts": [{"type": "text", "content": "hi"}]}],
        llm_calls=list(llm_calls),
        tool_calls=list(tool_calls),
        start_ns=_ns(start) if start is not None else 0,
        end_ns=_ns(end) if end is not None else 0,
    )


def _timing(turn: Turn) -> tuple[float | None, float | None, float | None]:
    inv = to_adk_invocation(turn)
    return inv.duration, inv.model_duration, inv.tool_duration


async def _evaluate(metric: str, *turns: Turn):
    return await evaluate_builtin_metric(
        metric_name=metric,
        actual_invocations=[to_adk_invocation(t) for t in turns],
        expected_invocations=None,
        judge_model=None,
        threshold=None,
        model_calls=[model_call_events(t) for t in turns],
    )


class TestConfig:
    @pytest.mark.parametrize("name", sorted(INFORMATIONAL_METRICS))
    def test_a_threshold_is_refused(self, name):
        with pytest.raises(ValueError, match="never passes or fails, so it takes no threshold"):
            BuiltinMetricDef(name=name, threshold=0.5)
        assert BuiltinMetricDef(name=name).threshold is None

    def test_a_run_level_threshold_skips_informational_metrics(self):
        entries = make_builtin_evaluator_entries(["tool_trajectory_avg_score", "token_usage_v1"], threshold=0.7)
        assert [(e.name, e.threshold) for e in entries] == [
            ("tool_trajectory_avg_score", 0.7),
            ("token_usage_v1", None),
        ]
        overridden = apply_builtin_overrides(
            [BuiltinMetricDef(name="tool_trajectory_avg_score"), BuiltinMetricDef(name="token_usage_v1")],
            threshold=0.7,
        )
        assert [e.threshold for e in overridden] == [0.7, None]

    def test_the_catalog_lists_them_as_efficiency_metrics(self):
        categories = {m.name: m.category for m in metric_catalog()}
        assert {name: categories.get(name) for name in INFORMATIONAL_METRICS} == dict.fromkeys(
            INFORMATIONAL_METRICS, "efficiency"
        )


class TestDurations:
    def test_a_wrapper_around_the_tool_does_not_hide_tool_time(self):
        turn = _turn([_call(leaf=(0, 400), chain=(0, 600))], [_tool(450, 550)])
        assert _timing(turn) == pytest.approx((1.0, 0.4, 0.1))

    def test_concurrent_model_calls_count_once(self):
        turn = _turn([_call(leaf=(100, 500)), _call(leaf=(300, 700), index=1)])
        assert _timing(turn) == pytest.approx((1.0, 0.6, 0.0))

    def test_a_model_call_inside_a_tool_is_model_time(self):
        turn = _turn([_call(leaf=(300, 500))], [_tool(100, 900)])
        assert _timing(turn) == pytest.approx((1.0, 0.2, 0.6))

    def test_spans_are_clipped_to_the_turn(self):
        turn = _turn([_call(leaf=(-100, 200))], [_tool(900, 1300)])
        assert _timing(turn) == pytest.approx((1.0, 0.2, 0.1))

    def test_an_untimed_tool_leaves_tool_time_unknown(self):
        turn = _turn([_call(leaf=(0, 400))], [_tool(450, None)])
        assert _timing(turn) == pytest.approx((1.0, 0.4, None))

    def test_an_untimed_turn_reports_no_timing(self):
        turn = _turn([_call(leaf=(0, 400))], start=None, end=None)
        assert _timing(turn) == (None, None, None)


class TestModelCalls:
    @pytest.mark.parametrize(
        ("usage", "total"),
        [
            (Usage(input_tokens=100, output_tokens=50, reasoning_output_tokens=20), 150),
            (Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=40), 150),
            (Usage(input_tokens=100, output_tokens=10, reasoning_output_tokens=20), 130),
        ],
        ids=["reasoning_within_output", "cached_within_input", "reasoning_reported_apart"],
    )
    async def test_token_usage_counts_each_token_once(self, usage, total):
        result = await _evaluate("token_usage_v1", _turn([_call(leaf=(0, 400), usage=usage)]))
        assert (result.eval_status, result.score, result.error) == ("INFORMATIONAL", total, None)

    async def test_a_call_without_usage_or_model_still_counts_as_an_inference_call(self):
        turn = _turn([_call(leaf=(0, 100), usage=Usage(input_tokens=5)), _call(leaf=(200, 300), model=None, index=1)])
        result = await _evaluate("inference_call_count_v1", turn)
        assert (result.eval_status, result.score) == ("INFORMATIONAL", 2.0)

    async def test_untimed_turns_report_no_duration(self):
        result = await _evaluate("invocation_duration_v1", _turn([_call(leaf=(0, 100))], start=None, end=None))
        assert (result.eval_status, result.score, result.error) == ("INFORMATIONAL", None, None)

    def test_model_call_events_leave_the_trajectory_alone(self):
        turn = _turn([_call(leaf=(0, 100), usage=Usage(input_tokens=5))], [_tool(150, 200)])
        inv = to_adk_invocation(turn)
        with_calls = _to_invocation_events(inv, model_call_events(turn)).intermediate_data
        without = _to_invocation_events(inv).intermediate_data
        assert get_all_tool_calls(with_calls) == get_all_tool_calls(without)
        assert len(with_calls.invocation_events) == len(without.invocation_events) + 1


@pytest.mark.skipif(not os.path.exists(HELM_TRACE), reason="samples/helm.json not available")
class TestRecordedTrace:
    """``samples/helm.json`` is an ADK agent whose ``call_llm`` span also covers the tool it ran."""

    def test_tool_time_comes_from_the_tool_span_not_the_wrapper(self):
        (turn,) = extract_conversation(load_traces(HELM_TRACE)).turns
        duration, model, tool = _timing(turn)
        assert (round(duration, 3), round(tool, 3)) == (4.168, 0.057)
        assert model + tool < duration

    @pytest.mark.parametrize(
        ("metric", "score"),
        [
            ("tool_call_count_v1", 1.0),
            ("inference_call_count_v1", 2.0),
            ("token_usage_v1", 3906.0),
            ("invocation_duration_v1", pytest.approx(4.168, abs=1e-3)),
        ],
    )
    async def test_metrics_match_the_recorded_telemetry(self, metric, score):
        conversation = extract_conversation(load_traces(HELM_TRACE))
        result = await evaluate_custom_evaluator(BuiltinMetricDef(name=metric), conversation, None)
        assert (result.eval_status, result.score, result.error) == ("INFORMATIONAL", score, None)
