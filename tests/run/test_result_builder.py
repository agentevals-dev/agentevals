"""Pure-function tests for build_results / summarize_run_result / classify_evaluator."""

from __future__ import annotations

from uuid import UUID, uuid4

from agentevals.config import BuiltinMetricDef, CodeEvaluatorDef, EvalParams
from agentevals.run.result_builder import build_results, classify_evaluator, summarize_run_result
from agentevals.runner import MetricResult, RunResult, TraceResult
from agentevals.storage.models import ResultStatus


def _params(custom_evaluators=None) -> EvalParams:
    return EvalParams(metrics=["m_builtin"], custom_evaluators=custom_evaluators or [])


def _trace_result(*metrics) -> TraceResult:
    return TraceResult(trace_id="trace-1", num_invocations=1, metric_results=list(metrics))


def _mr(name="m_builtin", **kw):
    kw.setdefault("eval_status", "PASSED")
    return MetricResult(metric_name=name, **kw)


class TestClassifyEvaluator:
    def test_unknown_falls_back_to_builtin(self):
        assert classify_evaluator("unknown", _params()) == "builtin"

    def test_custom_code_classified_correctly(self):
        params = _params(custom_evaluators=[CodeEvaluatorDef(name="my_code", path="./e.py")])
        assert classify_evaluator("my_code", params) == "code"

    def test_builtin_in_metrics_list(self):
        """Even when explicitly listed in params.metrics, the absence of a
        matching custom_evaluators entry defaults to 'builtin'. This is
        intentional: the persisted result row needs a stable type label and
        custom evaluators are the only ones we can disambiguate by name."""
        assert classify_evaluator("m_builtin", _params()) == "builtin"


class TestBuildResults:
    def test_one_metric_per_trace_yields_one_result(self):
        run_id = uuid4()
        rr = RunResult(trace_results=[_trace_result(_mr())])
        results = build_results(run_id, _params(), rr)
        assert len(results) == 1
        assert results[0].run_id == run_id
        assert results[0].evaluator_name == "m_builtin"

    def test_multiple_metrics_flatten(self):
        rr = RunResult(
            trace_results=[
                _trace_result(_mr(name="a"), _mr(name="b"), _mr(name="c")),
                _trace_result(_mr(name="a")),
            ]
        )
        results = build_results(uuid4(), _params(), rr)
        assert len(results) == 4
        names = sorted(r.evaluator_name for r in results)
        assert names == ["a", "a", "b", "c"]

    def test_eval_set_item_id_defaults_to_trace_id(self):
        """OSS scope: no per-eval-case id extraction. Trace id is the stable
        identifier for both eval_set_item_id and eval_set_item_name. Test
        locks this so future changes are deliberate."""
        rr = RunResult(trace_results=[_trace_result(_mr())])
        result = build_results(uuid4(), _params(), rr)[0]
        assert result.eval_set_item_id == "trace-1"
        assert result.eval_set_item_name == "trace-1"
        assert result.trace_id == "trace-1"


class TestSummarizeRunResult:
    def test_counts_pass_fail_skip_error(self):
        rr = RunResult(
            trace_results=[
                _trace_result(
                    _mr(eval_status="PASSED"),
                    _mr(eval_status="FAILED"),
                    _mr(eval_status="NOT_EVALUATED"),
                    _mr(error="boom"),
                )
            ]
        )
        summary = summarize_run_result(rr)
        assert summary["result_counts"] == {"passed": 1, "failed": 1, "skipped": 1, "errored": 1}
        assert summary["trace_count"] == 1

    def test_propagates_errors_and_perf(self):
        rr = RunResult(errors=["loader failure"], performance_metrics={"p50": 100})
        summary = summarize_run_result(rr)
        assert summary["errors"] == ["loader failure"]
        assert summary["performance_metrics"] == {"p50": 100}
