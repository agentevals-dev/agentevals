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
from ..runner import RunResult
from ..storage.models import Result

EvaluatorType = Literal["builtin", "code", "remote", "openai_eval"]


def classify_evaluator(metric_name: str, params: EvalParams) -> EvaluatorType:
    """Look up whether a metric was a built-in or a custom evaluator,
    falling back to ``builtin`` so unknown names round-trip cleanly rather
    than raising during persistence."""
    for ce in params.custom_evaluators:
        if ce.name == metric_name:
            return ce.type
    return "builtin"


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
        item_name = trace_result.trace_id
        for mr in trace_result.metric_results:
            out.append(
                Result.from_metric_result(
                    run_id=run_id,
                    eval_set_item_id=item_id,
                    eval_set_item_name=item_name,
                    trace_id=trace_result.trace_id,
                    evaluator_type=classify_evaluator(mr.metric_name, params),
                    metric_result=mr,
                )
            )
    return out


def summarize_run_result(run_result: RunResult) -> dict[str, Any]:
    """Summary blob persisted alongside the run row.

    Counts mirror :class:`agentevals.storage.models.ResultStatus` values so a
    caller polling ``GET /api/runs/{id}`` can compute pass/fail rates without
    fetching the full result list.
    """
    counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    for tr in run_result.trace_results:
        for mr in tr.metric_results:
            if mr.error:
                counts["errored"] += 1
            elif (mr.eval_status or "").upper() == "PASSED":
                counts["passed"] += 1
            elif (mr.eval_status or "").upper() == "FAILED":
                counts["failed"] += 1
            else:
                counts["skipped"] += 1
    return {
        "trace_count": len(run_result.trace_results),
        "result_counts": counts,
        "errors": list(run_result.errors),
        "performance_metrics": run_result.performance_metrics,
    }
