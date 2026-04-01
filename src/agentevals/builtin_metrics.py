"""Built-in ADK metric evaluation — criteria construction and evaluator resolution."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from google.adk.evaluation.eval_case import Invocation, get_all_tool_calls
from google.adk.evaluation.eval_metrics import (
    BaseCriterion,
    EvalMetric,
    HallucinationsCriterion,
    JudgeModelOptions,
    LlmAsAJudgeCriterion,
    LlmBackedUserSimulatorCriterion,
    RubricsBasedCriterion,
    ToolTrajectoryCriterion,
)
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent
from google.adk.evaluation.evaluator import EvaluationResult, Evaluator

logger = logging.getLogger(__name__)

METRICS_NEEDING_EXPECTED = {
    "tool_trajectory_avg_score",
    "response_match_score",
    "response_evaluation_score",
    "final_response_match_v2",
}

METRICS_NEEDING_LLM = {
    "final_response_match_v2",
    "rubric_based_final_response_quality_v1",
    "hallucinations_v1",
    "rubric_based_tool_use_quality_v1",
    "per_turn_user_simulator_quality_v1",
}

METRICS_NEEDING_GCP = {
    "response_evaluation_score",
    "safety_v1",
}


def rubric_strings_to_objects(rubric_texts: list[str]) -> list[Rubric]:
    """Convert plain-text rubric strings into ADK Rubric objects."""
    return [
        Rubric(
            rubric_id=f"rubric_{i}",
            rubric_content=RubricContent(text_property=text),
        )
        for i, text in enumerate(rubric_texts)
    ]


def build_eval_metric(
    metric_name: str,
    judge_model: str | None,
    threshold: float | None,
    rubrics: list[str] | None = None,
    match_type: str | None = None, 
) -> EvalMetric:
    """Construct an ADK ``EvalMetric`` with the appropriate criterion."""
    effective_threshold = threshold if threshold is not None else 0.5

    criterion: BaseCriterion | None = None

    if metric_name == "tool_trajectory_avg_score":
        _match = (
            ToolTrajectoryCriterion.MatchType[match_type.upper()]
            if match_type
            else ToolTrajectoryCriterion.MatchType.EXACT
        )
        criterion = ToolTrajectoryCriterion(threshold=effective_threshold, matchType=_match)
    elif metric_name == "final_response_match_v2":
        judge_opts = JudgeModelOptions()
        if judge_model:
            judge_opts.judge_model = judge_model
        criterion = LlmAsAJudgeCriterion(
            threshold=effective_threshold,
            judge_model_options=judge_opts,
        )
    elif metric_name == "hallucinations_v1":
        judge_opts = JudgeModelOptions()
        if judge_model:
            judge_opts.judge_model = judge_model
        criterion = HallucinationsCriterion(
            threshold=effective_threshold,
            judge_model_options=judge_opts,
        )
    elif metric_name in (
        "rubric_based_final_response_quality_v1",
        "rubric_based_tool_use_quality_v1",
    ):
        judge_opts = JudgeModelOptions()
        if judge_model:
            judge_opts.judge_model = judge_model
        rubric_objects = rubric_strings_to_objects(rubrics) if rubrics else []
        criterion = RubricsBasedCriterion(
            threshold=effective_threshold,
            judge_model_options=judge_opts,
            rubrics=rubric_objects,
        )
    elif metric_name == "per_turn_user_simulator_quality_v1":
        judge_opts = JudgeModelOptions()
        if judge_model:
            judge_opts.judge_model = judge_model
        criterion = LlmBackedUserSimulatorCriterion(
            threshold=effective_threshold,
            judge_model_options=judge_opts,
        )
    elif metric_name in ("response_match_score", "response_evaluation_score", "safety_v1"):
        criterion = BaseCriterion(threshold=effective_threshold)

    return EvalMetric(
        metric_name=metric_name,
        threshold=effective_threshold,
        criterion=criterion,
    )


def get_evaluator(eval_metric: EvalMetric) -> Evaluator:
    """Resolve an evaluator, using direct imports for known lightweight metrics
    to avoid pulling in heavy deps (numpy/rouge_score) via the full registry."""
    name = eval_metric.metric_name

    _DIRECT_EVALUATORS: dict[str, tuple[str, str]] = {
        "tool_trajectory_avg_score": (
            "google.adk.evaluation.trajectory_evaluator",
            "TrajectoryEvaluator",
        ),
    }

    if name in _DIRECT_EVALUATORS:
        import importlib

        mod_path, cls_name = _DIRECT_EVALUATORS[name]
        mod = importlib.import_module(mod_path)
        evaluator_cls = getattr(mod, cls_name)
        return evaluator_cls(eval_metric=eval_metric)  # type: ignore[call-arg]

    from google.adk.evaluation.metric_evaluator_registry import (
        DEFAULT_METRIC_EVALUATOR_REGISTRY,
    )

    return DEFAULT_METRIC_EVALUATOR_REGISTRY.get_evaluator(eval_metric)


def extract_trajectory_details(eval_result: EvaluationResult) -> dict[str, Any]:
    """Extract expected vs actual tool call details from trajectory evaluation."""
    comparisons = []

    for per_inv_result in eval_result.per_invocation_results:
        actual_inv = per_inv_result.actual_invocation
        expected_inv = per_inv_result.expected_invocation

        actual_tools = []
        expected_tools = []

        if actual_inv and actual_inv.intermediate_data:
            tool_calls = get_all_tool_calls(actual_inv.intermediate_data)
            actual_tools = [{"name": tc.name, "args": tc.args} for tc in tool_calls]

        if expected_inv and expected_inv.intermediate_data:
            tool_calls = get_all_tool_calls(expected_inv.intermediate_data)
            expected_tools = [{"name": tc.name, "args": tc.args} for tc in tool_calls]

        comparisons.append(
            {
                "invocation_id": actual_inv.invocation_id if actual_inv else None,
                "expected": expected_tools,
                "actual": actual_tools,
                "matched": per_inv_result.score == 1.0,
            }
        )

    return {"comparisons": comparisons}


async def evaluate_builtin_metric(
    metric_name: str,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    judge_model: str | None,
    threshold: float | None,
    match_type: str | None = None,
) -> dict[str, Any]:
    """Evaluate a single built-in ADK metric.

    Returns a dict with keys: metric_name, score, eval_status,
    per_invocation_scores, error, details.
    """
    from .runner import MetricResult

    if metric_name in METRICS_NEEDING_EXPECTED and not expected_invocations:
        return MetricResult(
            metric_name=metric_name,
            error=(
                f"Metric '{metric_name}' requires expected invocations "
                f"(golden eval set), but none were provided or matched."
            ),
        )

    try:
        eval_metric = build_eval_metric(metric_name, judge_model, threshold, match_type=match_type)
        evaluator: Evaluator = get_evaluator(eval_metric)

        if inspect.iscoroutinefunction(evaluator.evaluate_invocations):
            eval_result: EvaluationResult = await evaluator.evaluate_invocations(
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )
        else:
            eval_result: EvaluationResult = await asyncio.to_thread(
                evaluator.evaluate_invocations,
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )

        per_inv_scores = [r.score for r in eval_result.per_invocation_results]

        details = None
        if metric_name == "tool_trajectory_avg_score":
            details = extract_trajectory_details(eval_result)

        return MetricResult(
            metric_name=metric_name,
            score=eval_result.overall_score,
            eval_status=eval_result.overall_eval_status.name,
            per_invocation_scores=per_inv_scores,
            details=details,
        )

    except Exception as exc:
        logger.exception("Failed to evaluate metric '%s'", metric_name)
        return MetricResult(
            metric_name=metric_name,
            error=str(exc),
        )
