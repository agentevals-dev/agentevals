"""OpenAI Evals API backend — delegates grading to the OpenAI Evals API.

Builds testing criteria from the evaluator config, submits invocation pairs
as JSONL items, polls for completion, and maps per-item results back to a
MetricResult.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from google.adk.evaluation.eval_case import Invocation

from .config import OpenAIEvalDef
from .custom_evaluators import _content_to_text

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2

_TEXT_PAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "actual_response": {"type": "string"},
        "expected_response": {"type": "string"},
    },
    "required": ["actual_response", "expected_response"],
}


def _build_testing_criteria(evaluator_def: OpenAIEvalDef) -> dict[str, Any]:
    """Build the OpenAI testing_criteria dict from the evaluator config.

    Each grader type produces a different shape.  Extend this function
    when adding support for new OpenAI grader types.
    """
    grader = evaluator_def.grader
    grader_type = grader["type"]

    if grader_type == "text_similarity":
        return {
            "type": "text_similarity",
            "name": evaluator_def.name,
            "input": "{{ item.actual_response }}",
            "reference": "{{ item.expected_response }}",
            "evaluation_metric": grader["evaluation_metric"],
            "pass_threshold": evaluator_def.threshold,
        }

    raise ValueError(f"Unsupported grader type: {grader_type}")


def _build_jsonl_items(
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation],
) -> list[dict[str, Any]]:
    items = []
    for i, actual_inv in enumerate(actual_invocations):
        actual_text = _content_to_text(actual_inv.final_response)
        if i < len(expected_invocations):
            expected_text = _content_to_text(expected_invocations[i].final_response)
        else:
            expected_text = ""
        items.append(
            {
                "item": {
                    "actual_response": actual_text,
                    "expected_response": expected_text,
                }
            }
        )
    return items


def _get_openai_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for openai_eval evaluators. "
            "Install it with: pip install 'agentevals-cli[openai]'"
        ) from exc
    return OpenAI()


def _extract_item_score(output_item: Any) -> float | None:
    results = getattr(output_item, "results", None)
    if not results:
        return None
    for r in results:
        if getattr(r, "score", None) is not None:
            return float(r.score)
    return None


async def evaluate_openai_eval(
    evaluator_def: OpenAIEvalDef,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
) -> Any:
    """Run an evaluation via the OpenAI Evals API and return a MetricResult."""
    from .runner import MetricResult

    if not os.environ.get("OPENAI_API_KEY"):
        return MetricResult(
            metric_name=evaluator_def.name,
            error="OPENAI_API_KEY environment variable is not set.",
        )

    if expected_invocations is None:
        return MetricResult(
            metric_name=evaluator_def.name,
            error="OpenAI text_similarity grader requires expected invocations (golden eval set).",
        )

    items = _build_jsonl_items(actual_invocations, expected_invocations)
    if not items:
        return MetricResult(
            metric_name=evaluator_def.name,
            error="No invocations to evaluate.",
        )

    testing_criteria = _build_testing_criteria(evaluator_def)
    eval_id = None

    try:
        client = await asyncio.to_thread(_get_openai_client)

        eval_obj = await asyncio.to_thread(
            client.evals.create,
            name=f"agentevals-{evaluator_def.name}",
            data_source_config={
                "type": "custom",
                "item_schema": _TEXT_PAIR_SCHEMA,
                "include_sample_schema": False,
            },
            testing_criteria=[testing_criteria],
        )
        eval_id = eval_obj.id
        logger.info("Created OpenAI eval %s for '%s'", eval_id, evaluator_def.name)

        run = await asyncio.to_thread(
            client.evals.runs.create,
            eval_id=eval_id,
            name=f"agentevals-run-{evaluator_def.name}",
            data_source={
                "type": "jsonl",
                "source": {
                    "type": "file_content",
                    "content": items,
                },
            },
        )
        run_id = run.id
        logger.info("Created OpenAI eval run %s", run_id)

        run = await _poll_run(client, eval_id, run_id, evaluator_def)
        if isinstance(run, MetricResult):
            return run

        return await _collect_results(client, eval_id, run_id, run, evaluator_def)

    except ImportError:
        raise
    except Exception as exc:
        logger.exception("OpenAI eval failed for '%s'", evaluator_def.name)
        return MetricResult(
            metric_name=evaluator_def.name,
            error=f"OpenAI Evals API error: {exc}",
        )
    finally:
        if eval_id:
            try:
                await asyncio.to_thread(client.evals.delete, eval_id)
                logger.debug("Cleaned up OpenAI eval %s", eval_id)
            except Exception:
                logger.debug("Failed to clean up OpenAI eval %s", eval_id, exc_info=True)


async def _poll_run(client: Any, eval_id: str, run_id: str, evaluator_def: OpenAIEvalDef) -> Any:
    """Poll until the run completes. Returns the run object, or a MetricResult on error/timeout."""
    from .runner import MetricResult

    start_time = time.monotonic()
    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > evaluator_def.timeout:
            return MetricResult(
                metric_name=evaluator_def.name,
                error=f"OpenAI eval run timed out after {evaluator_def.timeout}s.",
            )

        run = await asyncio.to_thread(client.evals.runs.retrieve, run_id, eval_id=eval_id)

        if run.status == "completed":
            return run
        if run.status in ("failed", "canceled"):
            return MetricResult(
                metric_name=evaluator_def.name,
                error=f"OpenAI eval run {run.status}: {getattr(run, 'error', 'unknown')}",
            )

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _collect_results(client: Any, eval_id: str, run_id: str, run: Any, evaluator_def: OpenAIEvalDef) -> Any:
    """Extract scores from a completed run and return a MetricResult."""
    from .runner import MetricResult

    output_items_page = await asyncio.to_thread(client.evals.runs.output_items.list, run_id=run_id, eval_id=eval_id)
    output_items = list(output_items_page.data) if output_items_page.data else []

    per_invocation_scores: list[float | None] = [_extract_item_score(item) for item in output_items]

    valid_scores = [s for s in per_invocation_scores if s is not None]
    overall_score = sum(valid_scores) / len(valid_scores) if valid_scores else 0.0

    result_counts = run.result_counts
    passed = result_counts.passed if result_counts else 0
    failed = result_counts.failed if result_counts else 0
    total = result_counts.total if result_counts else 0
    eval_status = "PASSED" if failed == 0 and total > 0 else "FAILED"

    details: dict[str, Any] = {
        "openai_eval_id": eval_id,
        "openai_run_id": run_id,
        "evaluation_metric": evaluator_def.grader.get("evaluation_metric"),
        "result_counts": {"passed": passed, "failed": failed, "total": total},
    }
    per_criteria = getattr(run, "per_testing_criteria_results", None)
    if per_criteria:
        details["per_testing_criteria"] = [
            {"name": c.testing_criteria, "passed": c.passed, "failed": c.failed} for c in per_criteria
        ]

    return MetricResult(
        metric_name=evaluator_def.name,
        score=overall_score,
        eval_status=eval_status,
        per_invocation_scores=per_invocation_scores,
        details=details,
    )
