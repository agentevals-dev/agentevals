import asyncio
import os

import pytest

from agentevals.config import EvalRunConfig
from agentevals.runner import load_eval_set, run_evaluation
from agentevals.trace_metrics import extract_trace_metadata

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
            metrics=["tool_trajectory_avg_score"],
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

    def test_missing_eval_set_error(self):
        """Trajectory metric without eval set should report a clear error."""
        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            metrics=["tool_trajectory_avg_score"],
        )
        result = asyncio.run(run_evaluation(config))

        mr = result.trace_results[0].metric_results[0]
        assert mr.error is not None
        assert "requires expected invocations" in mr.error

    def test_bad_trace_file(self):
        config = EvalRunConfig(
            trace_files=["/nonexistent/file.json"],
            metrics=["tool_trajectory_avg_score"],
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
            metrics=["tool_trajectory_avg_score"],
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
            metrics=["tool_trajectory_avg_score", "tool_trajectory_avg_score"],
        )
        result = asyncio.run(run_evaluation(config))

        tr = result.trace_results[0]
        assert len(tr.metric_results) == 2

    def test_json_output_format(self):
        from agentevals.output import format_results

        config = EvalRunConfig(
            trace_files=[HELM_TRACE],
            eval_set_file=EVAL_SET,
            metrics=["tool_trajectory_avg_score"],
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
            metrics=["tool_trajectory_avg_score"],
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
