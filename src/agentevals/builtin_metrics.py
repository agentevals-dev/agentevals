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

from .resolvers import get_resolved_credential

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


def _to_invocation_events(inv: Invocation) -> Invocation:
    """Return a copy of *inv* with ``intermediate_data`` shaped as ``InvocationEvents``.

    ADK's own runtime (``EvaluationGenerator.convert_events_to_eval_invocations``) always
    emits actual invocations as ``InvocationEvents``, and several evaluators read only that
    variant of the ``IntermediateDataType`` union: ``hallucinations_v1`` and the multi-turn
    Vertex metrics treat ``IntermediateData`` as an empty trajectory, so the judge never
    sees tool outputs. agentevals' trace converters populate ``IntermediateData``, so every
    built-in metric is fed through this adapter.

    Each tool call is followed by its response, matched by ``id`` first, then by tool name
    in order among responses whose ``id`` matches no call (instrumentations disagree on call
    ids, e.g. positional ids in LLM messages vs per-execution ids on tool spans). Responses
    that still have no call are appended rather than dropped, because missing evidence makes
    the hallucinations judge label grounded claims as unsupported. ADK authors calls and
    responses with the agent name (no separate ``"tool"`` actor); ``"agent"`` mirrors that.
    """
    from google.genai import types as genai_types

    if inv.intermediate_data is None or isinstance(inv.intermediate_data, InvocationEvents):
        return inv

    id_: IntermediateData = inv.intermediate_data
    call_ids = {tc.id for tc in id_.tool_uses if tc.id}
    responses = list(id_.tool_responses)
    consumed = [False] * len(responses)

    def _claim(tool_call: genai_types.FunctionCall) -> genai_types.FunctionResponse | None:
        for j, tr in enumerate(responses):
            if not consumed[j] and tool_call.id and tr.id == tool_call.id:
                consumed[j] = True
                return tr
        for j, tr in enumerate(responses):
            if not consumed[j] and tr.id not in call_ids and tr.name == tool_call.name:
                consumed[j] = True
                return tr
        return None

    def _response_event(tr: genai_types.FunctionResponse) -> InvocationEvent:
        return InvocationEvent(
            author="agent",
            content=genai_types.Content(role="user", parts=[genai_types.Part(function_response=tr)]),
        )

    events: list[InvocationEvent] = []
    for tool_call in id_.tool_uses:
        events.append(
            InvocationEvent(
                author="agent",
                content=genai_types.Content(role="model", parts=[genai_types.Part(function_call=tool_call)]),
            )
        )
        match = _claim(tool_call)
        if match is not None:
            events.append(_response_event(match))

    events.extend(_response_event(tr) for j, tr in enumerate(responses) if not consumed[j])

    for author, parts in id_.intermediate_responses:
        events.append(
            InvocationEvent(
                author=author or "agent",
                content=genai_types.Content(role="model", parts=list(parts)),
            )
        )

    return inv.model_copy(update={"intermediate_data": InvocationEvents(invocation_events=events)})


def _enrich_app_details(invocations: list[Invocation]) -> list[Invocation]:
    """Synthesize minimal ``app_details`` so judges know which tools the agent had.

    ADK judges read tool declarations from ``app_details.agent_details``. Without them,
    ``multi_turn_tool_use_quality_v1`` has no schema to compare calls against, and the
    ``hallucinations_v1`` / ``rubric_based_*`` prompts state "Agent has no tools." next to
    the tool calls they show. Our trace converters do not populate ``app_details``, so we
    synthesize a minimal record from tool names observed across the conversation.
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


def _build_judge_model(model_id: str, api_key: str, base_url: str | None = None):
    """Build a judge ``BaseLlm`` carrying *api_key* directly, instead of reading it from env.

    LiteLlm-backed providers take ``api_key`` (and optional ``base_url``) as constructor
    kwargs that forward into every ``litellm.acompletion`` call. The Gemini-native model
    class takes no ``api_key``; its cached ``google.genai`` client is replaced with one
    built from the resolved key.

    Routing is by ADK's ``LLMRegistry`` class resolution, which is authoritative: the
    evaluator already resolved this same *model_id* to a model class when ``_setup_auto_rater``
    ran at construction, so this lookup cannot disagree or fail here.
    """
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.registry import LLMRegistry

    if issubclass(LLMRegistry().resolve(model_id), LiteLlm):
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return LiteLlm(model=model_id, **kwargs)

    from google.adk.models.google_llm import Gemini
    from google.genai import Client
    from google.genai import types as genai_types

    model = Gemini(model=model_id)
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["http_options"] = genai_types.HttpOptions(base_url=base_url)
    # api_client is a functools.cached_property that memoizes into the instance __dict__;
    # seeding that slot pre-empts the lazily-built client so the judge uses the resolved key.
    model.__dict__["api_client"] = Client(**client_kwargs)
    return model


def _inject_judge_credential(evaluator: Evaluator, api_key: str, base_url: str | None = None) -> None:
    """Replace a judge evaluator's auto-rater model with one built from *api_key*.

    Keyed on the ADK private seam (``_judge_model_options`` / ``_judge_model``, set by
    ``LlmAsJudge._setup_auto_rater``) rather than on a class, so this single path covers
    ``FinalResponseMatchV2Evaluator``, the ``rubric_based_*_v1`` evaluators, and
    ``HallucinationsV1Evaluator`` (which exposes the same attributes without subclassing
    ``LlmAsJudge``). ``get_evaluator`` returns a fresh instance per evaluation, so mutating
    it here carries no shared state and is safe across concurrent runs.

    TODO(upstream): propose that ADK ``JudgeModelOptions`` carry a credential or a prebuilt
    model instance, so judge auth no longer depends on this private seam or process env.
    """
    opts = getattr(evaluator, "_judge_model_options", None)
    if opts is None or not hasattr(evaluator, "_judge_model"):
        logger.warning("evaluator %s is not judge-backed; cannot inject credential", type(evaluator).__name__)
        return
    model_id = getattr(opts, "judge_model", None)
    if not model_id:
        logger.warning(
            "evaluator %s has no resolved judge_model; skipping credential injection", type(evaluator).__name__
        )
        return
    evaluator._judge_model = _build_judge_model(model_id, api_key, base_url)


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
    credential_ref: str | None = None,
    judge_base_url: str | None = None,
    expected_reason: str | None = None,
):
    """Evaluate a single built-in ADK metric and return a ``MetricResult``.

    A metric that compares against a golden conversation is ``NOT_EVALUATED`` when none was
    selected, with the reason in ``details``; it is never scored against an arbitrary case.
    """
    from .runner import MetricResult

    if metric_name in METRICS_NEEDING_EXPECTED and not expected_invocations:
        return MetricResult(
            metric_name=metric_name,
            eval_status="NOT_EVALUATED",
            details={"reason": expected_reason or "no eval set provided"},
        )

    try:
        eval_metric = build_eval_metric(metric_name, judge_model, threshold, match_type=match_type)
        evaluator: Evaluator = get_evaluator(eval_metric)

        if credential_ref:
            api_key = get_resolved_credential(credential_ref)
            if api_key is None:
                return MetricResult(
                    metric_name=metric_name,
                    error=(
                        f"Metric '{metric_name}' references credential '{credential_ref}', "
                        f"which was not provided in the run's credentialRefs."
                    ),
                )
            _inject_judge_credential(evaluator, api_key, judge_base_url)

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
