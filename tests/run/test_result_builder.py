"""Pure-function tests for build_results / summarize_run_result /
classify_evaluator / result_from_metric_result."""

from __future__ import annotations

from uuid import UUID, uuid4

from agentevals.config import BuiltinMetricDef, CodeEvaluatorDef, EvalParams
from agentevals.run.result_builder import (
    build_results,
    classify_evaluator,
    result_from_metric_result,
    summarize_run_result,
)
from agentevals.runner import MetricResult, RunResult, TraceResult
from agentevals.storage.models import ResultStatus, compute_result_id


def _params(evaluators=None) -> EvalParams:
    return EvalParams(evaluators=evaluators or [BuiltinMetricDef(name="m_builtin")])


def _trace_result(*metrics) -> TraceResult:
    return TraceResult(trace_id="trace-1", num_invocations=1, metric_results=list(metrics))


def _mr(name="m_builtin", **kw):
    kw.setdefault("eval_status", "PASSED")
    return MetricResult(metric_name=name, **kw)


class TestClassifyEvaluator:
    def test_unknown_falls_back_to_builtin(self):
        assert classify_evaluator("unknown", _params()) == "builtin"

    def test_custom_code_classified_correctly(self):
        params = _params(evaluators=[CodeEvaluatorDef(name="my_code", path="./e.py")])
        assert classify_evaluator("my_code", params) == "code"

    def test_builtin_in_evaluators_list(self):
        """Builtins remain classified as builtin even though every evaluator now
        lives in the shared params.evaluators list."""
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


class TestResultFromMetricResult:
    """Locks the renaming + status-mapping behavior between the in-pipeline
    MetricResult shape and the persisted Result shape."""

    def _mr(self, **overrides):
        defaults = dict(
            metric_name="tool_trajectory_avg_score",
            score=0.8,
            eval_status="PASSED",
            per_invocation_scores=[1.0, 0.6],
            error=None,
            details={"foo": "bar"},
            duration_ms=42.5,
        )
        defaults.update(overrides)
        return MetricResult(**defaults)

    def _build(self, mr):
        return result_from_metric_result(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            eval_set_item_id="item-1",
            eval_set_item_name="trace-abc",
            trace_id="trace-abc",
            evaluator_type="builtin",
            metric_result=mr,
        )

    def test_passed_maps_to_passed(self):
        r = self._build(self._mr(eval_status="PASSED"))
        assert r.status == ResultStatus.PASSED
        assert r.score == 0.8
        assert r.evaluator_name == "tool_trajectory_avg_score"
        assert r.evaluator_type == "builtin"
        assert r.eval_set_item_id == "item-1"
        assert r.trace_id == "trace-abc"

    def test_failed_maps_to_failed(self):
        r = self._build(self._mr(eval_status="FAILED"))
        assert r.status == ResultStatus.FAILED

    def test_not_evaluated_maps_to_skipped(self):
        r = self._build(self._mr(eval_status="NOT_EVALUATED", score=None, per_invocation_scores=[]))
        assert r.status == ResultStatus.SKIPPED

    def test_unknown_status_maps_to_skipped(self):
        """Defensive: ADK sometimes emits non-standard status strings;
        anything unknown should land as skipped, not crash."""
        r = self._build(self._mr(eval_status="MAYBE_PASSED"))
        assert r.status == ResultStatus.SKIPPED

    def test_error_dominates_status(self):
        """Even if eval_status says PASSED, a non-empty error means
        the row lands as 'errored' so downstream consumers can filter on
        status alone without special-casing the error column."""
        r = self._build(self._mr(eval_status="PASSED", error="boom"))
        assert r.status == ResultStatus.ERRORED
        assert r.error_text == "boom"

    def test_duration_ms_renamed_to_latency_ms(self):
        r = self._build(self._mr(duration_ms=42.7))
        assert r.latency_ms == 42

    def test_latency_ms_none_when_duration_missing(self):
        r = self._build(self._mr(duration_ms=None))
        assert r.latency_ms is None

    def test_per_invocation_scores_preserved(self):
        r = self._build(self._mr(per_invocation_scores=[0.0, 0.5, 1.0]))
        assert r.per_invocation_scores == [0.0, 0.5, 1.0]

    def test_details_default_to_empty_dict(self):
        r = self._build(self._mr(details=None))
        assert r.details == {}

    def test_result_id_matches_canonical_formula(self):
        r = self._build(self._mr())
        expected = compute_result_id(
            UUID("00000000-0000-0000-0000-000000000001"),
            "item-1",
            "tool_trajectory_avg_score",
        )
        assert r.result_id == expected


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
