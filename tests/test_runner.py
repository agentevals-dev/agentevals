import asyncio
import json
import os

import pytest

from agentevals.config import BuiltinMetricDef, EvalParams, EvalRunConfig
from agentevals.genai.extract import extract_conversation
from agentevals.loader import Trace, load_traces
from agentevals.loader.otlp import OtlpJsonLoader
from agentevals.runner import load_eval_set, run_evaluation, run_evaluation_from_traces
from agentevals.trace_metrics import extract_trace_metadata


def _metadata(traces):
    return extract_trace_metadata(traces, extract_conversation(traces))


def _attrs(**kv) -> list[dict]:
    return [{"key": k.replace("__", "."), "value": {"stringValue": v}} for k, v in kv.items()]


def _make_tool_trace(tools: list[str], schema_url: str | None = None) -> Trace:
    """A minimal ADK trace: invoke_agent, call_llm, the given tools in order, call_llm."""

    def span(span_id, name, start, attrs, parent="1" * 16):
        out = {
            "traceId": "a" * 32,
            "spanId": span_id,
            "name": name,
            "startTimeUnixNano": str(start * 1000),
            "endTimeUnixNano": str((start + 100) * 1000),
            "attributes": attrs,
        }
        if parent:
            out["parentSpanId"] = parent
        return out

    spans = [
        span(
            "1" * 16,
            "invoke_agent test_agent",
            1000,
            _attrs(gen_ai__operation__name="invoke_agent", gen_ai__agent__name="test_agent"),
            parent=None,
        ),
        span(
            "2" * 16,
            "call_llm",
            2000,
            _attrs(
                gcp__vertex__agent__llm_request=json.dumps(
                    {"contents": [{"role": "user", "parts": [{"text": "do something"}]}]}
                )
            ),
        ),
        *[
            span(
                f"{i + 3:016x}",
                f"execute_tool {name}",
                3000 + i * 100,
                _attrs(gen_ai__operation__name="execute_tool", gen_ai__tool__name=name),
            )
            for i, name in enumerate(tools)
        ],
        span(
            "f" * 16,
            "call_llm",
            5000,
            _attrs(
                gcp__vertex__agent__llm_response=json.dumps({"content": {"role": "model", "parts": [{"text": "done"}]}})
            ),
        ),
    ]
    scope: dict = {"name": "gcp.vertex.agent"}
    scope_spans: dict = {"scope": scope, "spans": spans}
    if schema_url:
        scope_spans["schemaUrl"] = schema_url
    doc = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [scope_spans]}]}
    (trace,) = OtlpJsonLoader().load_from_dict(doc)
    return trace


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

    def test_missing_eval_set_not_evaluated(self):
        """A trajectory metric without an eval set is NOT_EVALUATED with the reason, not an error."""
        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            evaluators=[BuiltinMetricDef(name="tool_trajectory_avg_score")],
        )
        result = asyncio.run(run_evaluation(config))

        mr = result.trace_results[0].metric_results[0]
        assert mr.error is None
        assert mr.eval_status == "NOT_EVALUATED"
        assert mr.details == {"reason": "no eval set provided"}
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
        traces = load_traces(HELM_TRACE)
        metadata = _metadata(traces)

        assert metadata["agent_name"] == "helm_agent"
        assert metadata["model"] is not None
        assert metadata["start_time"] is not None
        assert metadata["start_time"] > 0
        assert metadata["user_input_preview"] is not None
        assert "helm" in metadata["user_input_preview"].lower()
        assert metadata["final_output_preview"] is not None
        assert len(metadata["final_output_preview"]) > 0

    def test_extract_trace_metadata_schema_version_unknown_when_schema_missing(self):
        metadata = _metadata([_make_tool_trace(["tool_a"])])
        assert metadata["schema_version"] is None

    def test_extract_trace_metadata_schema_version_unknown_when_schema_malformed(self):
        metadata = _metadata([_make_tool_trace(["tool_a"], schema_url="not-a-schema-version")])
        assert metadata["schema_version"] is None

    def test_extract_trace_metadata_schema_version_from_valid_schema_url(self):
        metadata = _metadata([_make_tool_trace(["tool_a"], schema_url="https://opentelemetry.io/schemas/1.39.0")])
        assert metadata["schema_version"] == "1.39.0"


class TestTrajectoryMatchType:
    """Verify trajectory_match_type produces different scores on the same trace.

    Actual calls [get, list], expected calls [list, get].
    EXACT and IN_ORDER fail; ANY_ORDER passes.
    """

    def _run(self, match_type, tmp_path):
        trace = _make_tool_trace(["helm_get_release", "helm_list_releases"])

        eval_set_path = tmp_path / "eval_set.json"
        eval_set_path.write_text(
            json.dumps(_make_eval_set_json(["helm_list_releases", "helm_get_release"])), encoding="utf-8"
        )
        eval_set = load_eval_set(str(eval_set_path))

        params = EvalParams(
            evaluators=[
                BuiltinMetricDef(name="tool_trajectory_avg_score", threshold=0.5, trajectory_match_type=match_type)
            ]
        )
        return asyncio.run(run_evaluation_from_traces([trace], params, eval_set)).trace_results[0]

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
        trace = _make_tool_trace(["helm_get_release", "helm_list_releases"])

        eval_set_path = tmp_path / "eval_set.json"
        eval_set_path.write_text(
            json.dumps(_make_eval_set_json(["helm_list_releases", "helm_get_release"])), encoding="utf-8"
        )
        eval_set = load_eval_set(str(eval_set_path))

        params = EvalParams(
            evaluators=[
                BuiltinMetricDef(name="tool_trajectory_avg_score", threshold=0.5, trajectory_match_type="ANY_ORDER")
            ]
        )
        trace_result = asyncio.run(run_evaluation_from_traces([trace], params, eval_set)).trace_results[0]

        assert len(trace_result.metric_results) == 1
        mr = trace_result.metric_results[0]
        assert mr.metric_name == "tool_trajectory_avg_score"
        assert mr.score == 1.0
        assert mr.eval_status == "PASSED"
