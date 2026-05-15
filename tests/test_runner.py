import asyncio
import json
import os

import pytest

from agentevals.config import BuiltinMetricDef, EvalRunConfig
from agentevals.converter import convert_traces
from agentevals.loader.base import Span, Trace
from agentevals.runner import _evaluate_trace, load_eval_set, run_evaluation
from agentevals.trace_metrics import extract_trace_metadata


def _make_tool_trace(tools: list[str]) -> Trace:
    """Build a minimal ADK trace calling the given tools in order."""
    invoke = Span(
        trace_id="t1",
        span_id="invoke1",
        parent_span_id=None,
        operation_name="invoke_agent test_agent",
        start_time=1000,
        duration=10000,
        tags={"otel.scope.name": "gcp.vertex.agent"},
    )
    call_llm_1 = Span(
        trace_id="t1",
        span_id="llm1",
        parent_span_id="invoke1",
        operation_name="call_llm",
        start_time=2000,
        duration=1000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_request": json.dumps(
                {"contents": [{"role": "user", "parts": [{"text": "do something"}]}]}
            ),
        },
    )
    tool_spans = [
        Span(
            trace_id="t1",
            span_id=f"tool{i}",
            parent_span_id="invoke1",
            operation_name=f"execute_tool {name}",
            start_time=3000 + i * 100,
            duration=100,
            tags={"otel.scope.name": "gcp.vertex.agent"},
        )
        for i, name in enumerate(tools)
    ]
    call_llm_2 = Span(
        trace_id="t1",
        span_id="llm2",
        parent_span_id="invoke1",
        operation_name="call_llm",
        start_time=5000,
        duration=1000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_response": json.dumps({"content": {"role": "model", "parts": [{"text": "done"}]}}),
        },
    )
    invoke.children = [call_llm_1, *tool_spans, call_llm_2]
    return Trace(
        trace_id="t1",
        root_spans=[invoke],
        all_spans=[invoke, call_llm_1, *tool_spans, call_llm_2],
    )


def _make_eval_set_json(tools: list[str]) -> dict:
    return {
        "eval_set_id": "test",
        "eval_cases": [
            {
                "eval_id": "inv_1",
                "conversation": [
                    {
                        "invocation_id": "inv_1",
                        "user_content": {
                            "role": "user",
                            "parts": [{"text": "do something"}],
                        },
                        "final_response": {
                            "role": "model",
                            "parts": [{"text": "done"}],
                        },
                        "intermediate_data": {
                            "tool_uses": [{"name": t, "args": {}, "id": f"e{i}"} for i, t in enumerate(tools)],
                            "tool_responses": [],
                        },
                    }
                ],
            }
        ],
    }


SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")
HELM_TRACE = os.path.join(SAMPLES_DIR, "helm.json")
HELM_3_TRACE = os.path.join(SAMPLES_DIR, "helm_3.json")
EVAL_SET = os.path.join(SAMPLES_DIR, "eval_set_helm.json")


@pytest.mark.skipif(
    not os.path.exists(HELM_TRACE) or not os.path.exists(EVAL_SET),
    reason="Sample files not available",
)
class TestRunner:
    def test_trajectory_eval_pass(self):
        """Helm trace should score 1.0 against its golden eval set."""
        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            eval_set_file=EVAL_SET,
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))

        assert len(result.errors) == 0
        assert len(result.trace_results) == 1

        tr = result.trace_results[0]
        assert tr.num_invocations == 1
        assert len(tr.metric_results) == 1

        mr = tr.metric_results[0]
        assert mr.metric_name == "tool_trajectory_avg_score"
        assert mr.score == 1.0
        assert mr.eval_status == "PASSED"
        assert mr.error is None
        assert mr.duration_ms is not None
        assert mr.duration_ms >= 0

    def test_missing_eval_set_error(self):
        """Trajectory metric without eval set should report a clear error."""
        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))

        mr = result.trace_results[0].metric_results[0]
        assert mr.error is not None
        assert "requires expected invocations" in mr.error
        assert mr.duration_ms is not None
        assert mr.duration_ms >= 0

    def test_bad_trace_file(self):
        config = EvalRunConfig(
            trace_files=["/nonexistent/file.json"],
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))
        assert len(result.errors) >= 1

    def test_load_eval_set(self):
        eval_set = load_eval_set(EVAL_SET)
        assert eval_set.eval_set_id == "helm_eval_set"
        assert len(eval_set.eval_cases) == 1
        case = eval_set.eval_cases[0]
        assert case.eval_id == "helm_list_releases"
        assert case.conversation is not None
        assert len(case.conversation) == 1

    @pytest.mark.skipif(
        not os.path.exists(HELM_3_TRACE),
        reason="helm_3.json not available",
    )
    def test_trajectory_failure_details(self):
        """Failed trajectory evaluation should include expected vs actual details."""
        config = EvalRunConfig(
            trace_files=[HELM_3_TRACE],
            eval_set_file=EVAL_SET,
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))

        assert len(result.trace_results) == 1
        tr = result.trace_results[0]
        assert len(tr.metric_results) == 1

        mr = tr.metric_results[0]
        assert mr.metric_name == "tool_trajectory_avg_score"
        assert mr.score == 0.0
        assert mr.eval_status == "FAILED"

        # Check that details are populated
        assert mr.details is not None
        assert "comparisons" in mr.details
        comparisons = mr.details["comparisons"]
        assert len(comparisons) == 1

        comp = comparisons[0]
        assert comp["matched"] is False
        assert len(comp["expected"]) == 1
        assert len(comp["actual"]) == 1

        # Expected has empty args
        assert comp["expected"][0]["name"] == "helm_list_releases"
        assert comp["expected"][0]["args"] == {}

        # Actual has args
        assert comp["actual"][0]["name"] == "helm_list_releases"
        assert comp["actual"][0]["args"] == {"all_namespaces": "true", "output": "json"}

    def test_multiple_metrics(self):
        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            eval_set_file=EVAL_SET,
            evaluators=[
                BuiltinMetricDef(name="tool_trajectory_avg_score"),
                BuiltinMetricDef(name="response_match_score"),
            ],
        )
        result = asyncio.run(run_evaluation(config))

        tr = result.trace_results[0]
        assert len(tr.metric_results) == 2

    def test_json_output_format(self):
        from agentevals.output import format_results

        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            eval_set_file=EVAL_SET,
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))
        output = format_results(result, fmt="json")

        import json

        data = json.loads(output)
        assert "traces" in data
        assert len(data["traces"]) == 1
        assert data["traces"][0]["metrics"][0]["score"] == 1.0

    def test_parallel_trace_error_isolation(self):
        config = EvalRunConfig(
            trace_files=[HELM_TRACE, "/nonexistent/file.json"],
            eval_set_file=EVAL_SET,
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))
        assert len(result.trace_results) >= 1
        assert len(result.errors) >= 1

    def testextract_trace_metadata_adk(self):
        from agentevals.loader.jaeger import JaegerJsonLoader

        loader = JaegerJsonLoader()
        traces = loader.load(HELM_TRACE)
        metadata = extract_trace_metadata(traces[0])

        assert metadata["agent_name"] == "helm_agent"
        assert metadata["model"] is not None
        assert metadata["start_time"] is not None
        assert metadata["start_time"] > 0
        assert metadata["user_input_preview"] is not None
        assert "helm" in metadata["user_input_preview"].lower()
        assert metadata["final_output_preview"] is not None
        assert len(metadata["final_output_preview"]) > 0


class TestTrajectoryMatchType:
    """Verify trajectory_match_type produces different scores on the same trace.

    Actual calls [get, list], expected calls [list, get].
    EXACT and IN_ORDER fail; ANY_ORDER passes.
    """

    def _run(self, match_type, tmp_path):
        conv_result = convert_traces([_make_tool_trace(["helm_get_release", "helm_list_releases"])])[0]

        eval_set_path = tmp_path / "eval_set.json"
        eval_set_path.write_text(json.dumps(_make_eval_set_json(["helm_list_releases", "helm_get_release"])))
        eval_set = load_eval_set(str(eval_set_path))

        return asyncio.run(
            _evaluate_trace(
                conv_result=conv_result,
                evaluators=[
                    BuiltinMetricDef(
                        name="tool_trajectory_avg_score",
                        threshold=0.5,
                        trajectory_match_type=match_type,
                    )
                ],
                eval_set=eval_set,
                eval_semaphore=asyncio.Semaphore(1),
            )
        )

    def test_exact_fails(self, tmp_path):
        mr = self._run(None, tmp_path).metric_results[0]
        assert mr.score == 0.0
        assert mr.eval_status == "FAILED"

    def test_any_order_passes(self, tmp_path):
        mr = self._run("ANY_ORDER", tmp_path).metric_results[0]
        assert mr.score == 1.0
        assert mr.eval_status == "PASSED"

    def test_in_order_fails(self, tmp_path):
        mr = self._run("IN_ORDER", tmp_path).metric_results[0]
        assert mr.score == 0.0
        assert mr.eval_status == "FAILED"


class TestBuiltinCustomEvaluatorOverrides:
    def test_builtin_custom_evaluator_uses_per_evaluator_match_type(self, tmp_path):
        conv_result = convert_traces([_make_tool_trace(["helm_get_release", "helm_list_releases"])])[0]

        eval_set_path = tmp_path / "eval_set.json"
        eval_set_path.write_text(json.dumps(_make_eval_set_json(["helm_list_releases", "helm_get_release"])))
        eval_set = load_eval_set(str(eval_set_path))

        trace_result = asyncio.run(
            _evaluate_trace(
                conv_result=conv_result,
                evaluators=[
                    BuiltinMetricDef(
                        name="tool_trajectory_avg_score",
                        threshold=0.5,
                        trajectory_match_type="ANY_ORDER",
                    )
                ],
                eval_set=eval_set,
                eval_semaphore=asyncio.Semaphore(1),
            )
        )

        assert len(trace_result.metric_results) == 1
        mr = trace_result.metric_results[0]
        assert mr.metric_name == "tool_trajectory_avg_score"
        assert mr.score == 1.0
        assert mr.eval_status == "PASSED"
