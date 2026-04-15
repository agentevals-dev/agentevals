"""Built-in ADK metric evaluation — criteria construction and evaluator resolution."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from google.adk.evaluation.eval_case import (
    IntermediateData,
    Invocation,
    InvocationEvent,
    InvocationEvents,
    get_all_tool_calls,
)
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
    "multi_turn_task_success_v1",
    "multi_turn_trajectory_quality_v1",
    "multi_turn_tool_use_quality_v1",
}

_METRICS_NEEDING_INVOCATION_EVENTS = {
    "multi_turn_task_success_v1",
    "multi_turn_trajectory_quality_v1",
    "multi_turn_tool_use_quality_v1",
}


def _to_invocation_events(inv: Invocation) -> Invocation:
    """Return a copy of *inv* with ``intermediate_data`` shaped as ``InvocationEvents``.

    Multi-turn Vertex AI metrics read ``invocation.intermediate_data.invocation_events``
    directly, but agentevals' trace converters populate the ``IntermediateData`` variant
    of the ``IntermediateDataType`` union. This adapter pairs each tool call with its
    matching tool response (by ``id`` when present, else by position) and emits them
    interleaved as ``call -> response -> call -> response``. ADK's native runtime
    authors both calls and responses with the agent name (no separate ``"tool"``
    actor); we use ``"agent"`` to match that convention so the Vertex judges see
    the dialog in the shape they expect.
    """
    from google.genai import types as genai_types

    if inv.intermediate_data is None or isinstance(inv.intermediate_data, InvocationEvents):
        return inv

    id_: IntermediateData = inv.intermediate_data
    response_by_id: dict[str, genai_types.FunctionResponse] = {tr.id: tr for tr in id_.tool_responses if tr.id}

    events: list[InvocationEvent] = []
    for i, tool_call in enumerate(id_.tool_uses):
        events.append(
            InvocationEvent(
                author="agent",
                content=genai_types.Content(role="model", parts=[genai_types.Part(function_call=tool_call)]),
            )
        )

        match: genai_types.FunctionResponse | None = None
        if tool_call.id and tool_call.id in response_by_id:
            match = response_by_id[tool_call.id]
        elif not tool_call.id and i < len(id_.tool_responses):
            candidate = id_.tool_responses[i]
            if not candidate.id:
                match = candidate

        if match is not None:
            events.append(
                InvocationEvent(
                    author="agent",
                    content=genai_types.Content(role="user", parts=[genai_types.Part(function_response=match)]),
                )
            )

    for author, parts in id_.intermediate_responses:
        events.append(
            InvocationEvent(
                author=author or "agent",
                content=genai_types.Content(role="model", parts=list(parts)),
            )
        )

    return inv.model_copy(update={"intermediate_data": InvocationEvents(invocation_events=events)})


def _enrich_app_details(invocations: list[Invocation]) -> list[Invocation]:
    """Synthesize minimal ``app_details`` so multi-turn metrics can score tool quality.

    Vertex AI's multi-turn evaluators read each invocation's ``app_details.agent_details``
    to learn which tools the agent has access to (their declarations). Without this,
    ``multi_turn_tool_use_quality_v1`` cannot score tool use because it has no schema
    to compare calls against. Our trace converters do not populate ``app_details``, so
    we synthesize a minimal record from tool names observed across the conversation.
    """
    from google.adk.evaluation.app_details import AgentDetails, AppDetails
    from google.genai import types as genai_types

    if any(inv.app_details and inv.app_details.agent_details for inv in invocations):
        return invocations

    tool_names: dict[str, None] = {}
    for inv in invocations:
        data = inv.intermediate_data
        if data is None:
            continue
        if isinstance(data, IntermediateData):
            for tc in data.tool_uses:
                if tc.name:
                    tool_names.setdefault(tc.name)
        elif isinstance(data, InvocationEvents):
            for ev in data.invocation_events:
                if not (ev.content and ev.content.parts):
                    continue
                for part in ev.content.parts:
                    if part.function_call and part.function_call.name:
                        tool_names.setdefault(part.function_call.name)

    if not tool_names:
        return invocations

    function_declarations = [genai_types.FunctionDeclaration(name=name) for name in tool_names]
    tool = genai_types.Tool(function_declarations=function_declarations)
    agent_details = AgentDetails(name="agent", instructions="", tool_declarations=[tool])
    app_details = AppDetails(agent_details={"agent": agent_details})

    return [inv.model_copy(update={"app_details": app_details}) for inv in invocations]


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
            ToolTrajectoryCriterion.MatchType[match_type] if match_type else ToolTrajectoryCriterion.MatchType.EXACT
        )
        criterion = ToolTrajectoryCriterion(threshold=effective_threshold, match_type=_match)
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
    elif metric_name in (
        "response_match_score",
        "response_evaluation_score",
        "safety_v1",
        "multi_turn_task_success_v1",
        "multi_turn_trajectory_quality_v1",
        "multi_turn_tool_use_quality_v1",
    ):
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

        if metric_name in _METRICS_NEEDING_INVOCATION_EVENTS:
            actual_invocations = _enrich_app_details([_to_invocation_events(inv) for inv in actual_invocations])
            if expected_invocations is not None:
                expected_invocations = _enrich_app_details([_to_invocation_events(inv) for inv in expected_invocations])

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
