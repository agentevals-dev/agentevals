"""Shared helpers that project a :class:`agentevals.runner.RunResult` onto
the persisted shapes (:class:`agentevals.storage.models.Result` rows + a
JSON ``summary`` blob).

Used both by the async worker (when a queued run finishes) and by the
``/api/evaluate`` route handler (when a synchronous UI upload finishes), so
both paths produce identical persisted shapes.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from ..config import EvalParams
from ..runner import MetricResult, RunResult
from ..storage.models import Result, ResultStatus, compute_result_id

EvaluatorType = Literal["builtin", "code", "remote"]


def classify_evaluator(metric_name: str, params: EvalParams) -> EvaluatorType:
    """Look up whether a metric was a built-in or a custom evaluator,
    falling back to ``builtin`` so unknown names round-trip cleanly rather
    than raising during persistence."""
    for evaluator in params.evaluators:
        if evaluator.name == metric_name:
            return evaluator.type
    return "builtin"


def result_from_metric_result(
    *,
    run_id: UUID,
    eval_set_item_id: str,
    eval_set_item_name: str,
    trace_id: str | None,
    evaluator_type: EvaluatorType,
    metric_result: MetricResult,
) -> Result:
    """Project an ADK :class:`MetricResult` onto a persistable :class:`Result`.

    The status mapping treats a non-empty ``error`` field as ``ERRORED`` even
    when ``eval_status`` would have been ``PASSED`` / ``FAILED``, so
    downstream consumers can filter on status alone without special-casing
    the error column.
    """
    if metric_result.error:
        status = ResultStatus.ERRORED
    else:
        raw = (metric_result.eval_status or "NOT_EVALUATED").upper()
        status = {
            "PASSED": ResultStatus.PASSED,
            "FAILED": ResultStatus.FAILED,
        }.get(raw, ResultStatus.SKIPPED)

    latency_ms = int(metric_result.duration_ms) if metric_result.duration_ms is not None else None

    return Result(
        result_id=compute_result_id(run_id, eval_set_item_id, metric_result.metric_name),
        run_id=run_id,
        eval_set_item_id=eval_set_item_id,
        eval_set_item_name=eval_set_item_name,
        evaluator_name=metric_result.metric_name,
        evaluator_type=evaluator_type,
        status=status,
        score=metric_result.score,
        per_invocation_scores=list(metric_result.per_invocation_scores or []),
        trace_id=trace_id,
        details=metric_result.details or {},
        error_text=metric_result.error,
        latency_ms=latency_ms,
    )


def build_results(run_id: UUID, params: EvalParams, run_result: RunResult) -> list[Result]:
    """Flatten ``run_result.trace_results[*].metric_results[*]`` into a list
    of persistable :class:`Result` rows.

    The ``eval_set_item_id`` and ``eval_set_item_name`` both default to the
    trace_id, since OSS doesn't currently extract a stable per-eval-case
    identifier from the ADK :class:`EvalSet`. Callers may post-process to
    attach their own identifiers.
    """
    out: list[Result] = []
    for trace_result in run_result.trace_results:
        item_id = trace_result.trace_id
        for mr in trace_result.metric_results:
            out.append(
                result_from_metric_result(
                    run_id=run_id,
                    eval_set_item_id=item_id,
                    eval_set_item_name=item_id,
                    trace_id=trace_result.trace_id,
                    evaluator_type=classify_evaluator(mr.metric_name, params),
                    metric_result=mr,
                )
            )
    return out


def _status_key(mr: MetricResult) -> str:
    """Map a :class:`MetricResult` onto a :class:`ResultStatus` string,
    matching :func:`result_from_metric_result` so the summary counts and the
    persisted ``result`` rows never disagree."""
    if mr.error:
        return "errored"
    status = (mr.eval_status or "").upper()
    if status == "PASSED":
        return "passed"
    if status == "FAILED":
        return "failed"
    return "skipped"


def summarize_run_result(run_result: RunResult) -> dict[str, Any]:
    """Summary blob persisted alongside the run row.

    Counts mirror :class:`agentevals.storage.models.ResultStatus` values so a
    caller polling ``GET /api/runs/{id}`` can compute pass/fail rates without
    fetching the full result list.

    ``per_metric`` aggregates the same statuses by ``evaluator_name`` plus a
    mean ``score``, so the run-history dashboard can chart per-metric trends
    across runs from the list response alone, without an N+1 over
    ``/api/runs/{id}/results``. ``avg_score`` is ``None`` when no invocation of
    that evaluator produced a numeric score (e.g. it only errored).
    """
    counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    per_metric: dict[str, dict[str, Any]] = {}
    agents: set[str] = set()
    for tr in run_result.trace_results:
        identity = tr.service_name or tr.agent_name
        if identity:
            agents.add(identity)
        for mr in tr.metric_results:
            key = _status_key(mr)
            counts[key] += 1

            metric = per_metric.setdefault(
                mr.metric_name,
                {"passed": 0, "failed": 0, "errored": 0, "skipped": 0, "_score_sum": 0.0, "_score_count": 0},
            )
            metric[key] += 1
            if mr.score is not None:
                metric["_score_sum"] += mr.score
                metric["_score_count"] += 1

    for metric in per_metric.values():
        score_count = metric.pop("_score_count")
        score_sum = metric.pop("_score_sum")
        metric["avg_score"] = (score_sum / score_count) if score_count else None

    return {
        "trace_count": len(run_result.trace_results),
        "result_counts": counts,
        "per_metric": per_metric,
        "agents": sorted(agents),
        "errors": list(run_result.errors),
        "performance_metrics": run_result.performance_metrics,
    }
