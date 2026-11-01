"""Built-in ADK metric evaluation — criteria construction and evaluator resolution."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
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

from .config import INFORMATIONAL_METRICS, RUBRIC_METRIC_TYPES
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


_METRIC_CATEGORIES = {
    "tool_trajectory_avg_score": "trajectory",
    "response_match_score": "response",
    "response_evaluation_score": "response",
    "final_response_match_v2": "response",
    "rubric_based_final_response_quality_v1": "quality",
    "rubric_based_tool_use_quality_v1": "quality",
    "hallucinations_v1": "safety",
    "safety_v1": "safety",
    "per_turn_user_simulator_quality_v1": "simulation",
    "multi_turn_task_success_v1": "multi-turn",
    "multi_turn_trajectory_quality_v1": "multi-turn",
    "multi_turn_tool_use_quality_v1": "multi-turn",
    "rubric_based_multi_turn_trajectory_quality_v1": "multi-turn",
    **dict.fromkeys(INFORMATIONAL_METRICS, "efficiency"),
}


@dataclass(frozen=True, slots=True)
class MetricDescription:
    name: str
    category: str
    description: str
    requires_eval_set: bool
    requires_llm: bool
    requires_gcp: bool
    requires_rubrics: bool
    working: bool


def metric_catalog() -> list[MetricDescription]:
    """The built-in metrics agentevals can run, from ADK's registry."""
    from google.adk.evaluation.metric_evaluator_registry import DEFAULT_METRIC_EVALUATOR_REGISTRY

    from .config import UNSUPPORTED_BUILTIN_METRICS

    out = []
    for metric in DEFAULT_METRIC_EVALUATOR_REGISTRY.get_registered_metrics():
        name = metric.metric_name
        if name in UNSUPPORTED_BUILTIN_METRICS:
            continue
        rubrics = name.startswith("rubric_based_")
        out.append(
            MetricDescription(
                name=name,
                category=_METRIC_CATEGORIES.get(name, "other"),
                description=metric.description or "No description available",
                requires_eval_set=name in METRICS_NEEDING_EXPECTED,
                requires_llm=name in METRICS_NEEDING_LLM,
                requires_gcp=name in METRICS_NEEDING_GCP,
                requires_rubrics=rubrics,
                working=not rubrics or name in RUBRIC_METRIC_TYPES,
            )
        )
    return out


def _to_invocation_events(inv: Invocation, model_calls: Sequence[InvocationEvent] = ()) -> Invocation:
    """Return a copy of *inv* with ``intermediate_data`` shaped as ``InvocationEvents``.

    ADK's own runtime (``EvaluationGenerator.convert_events_to_eval_invocations``) always
    emits actual invocations as ``InvocationEvents``, and several evaluators read only that
    variant of the ``IntermediateDataType`` union: ``hallucinations_v1`` and the multi-turn
    Vertex metrics treat ``IntermediateData`` as an empty trajectory, so the judge never
    sees tool outputs. The bridge preserves source-owned events; older eval sets may still
    carry flat ``IntermediateData`` and pass through this compatibility adapter.

    Each tool call is followed by its response, matched by ``id`` first, then by tool name
    in order among responses whose ``id`` matches no call (instrumentations disagree on call
    ids, e.g. positional ids in LLM messages vs per-execution ids on tool spans). Responses
    that still have no call are appended rather than dropped, because missing evidence makes
    the hallucinations judge label grounded claims as unsupported. Legacy flat data uses
    its sole captured agent owner, or ``"agent"`` when no ownership metadata is available.
    Multiple captured owners are ambiguous after flattening and cannot be reconstructed.

    *model_calls* are appended as given: content-less model call events that ADK's token and
    inference call counts read, and that every other evaluator skips.
    """
    from google.genai import types as genai_types

    if inv.intermediate_data is None or isinstance(inv.intermediate_data, InvocationEvents):
        if not model_calls:
            return inv
        events = list(inv.intermediate_data.invocation_events) if inv.intermediate_data else []
        if events[-len(model_calls) :] != list(model_calls):
            events.extend(model_calls)
        return inv.model_copy(update={"intermediate_data": InvocationEvents(invocation_events=events)})

    id_: IntermediateData = inv.intermediate_data
    owners = list(inv.app_details.agent_details) if inv.app_details else []
    if len(owners) > 1 and (id_.tool_uses or id_.tool_responses):
        raise ValueError(
            "Legacy flat tool evidence has multiple agent owners; source-owned invocation events are required"
        )
    owner = owners[0] if len(owners) == 1 else "agent"
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
            author=owner,
            content=genai_types.Content(role="user", parts=[genai_types.Part(function_response=tr)]),
        )

    events: list[InvocationEvent] = []
    for tool_call in id_.tool_uses:
        events.append(
            InvocationEvent(
                author=owner,
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
                author=author or owner,
                content=genai_types.Content(role="model", parts=list(parts)),
            )
        )
    events.extend(model_calls)

    return inv.model_copy(update={"intermediate_data": InvocationEvents(invocation_events=events)})


def _enrich_app_details(invocations: list[Invocation]) -> list[Invocation]:
    """Fill missing event owners with names-only metadata, preserving captured declarations."""
    from google.adk.evaluation.app_details import AgentDetails, AppDetails
    from google.genai import types as genai_types

    enriched = []
    for inv in invocations:
        data = _to_invocation_events(inv).intermediate_data
        captured = inv.app_details.agent_details if inv.app_details else {}
        names_by_owner: dict[str, dict[str, None]] = {}
        for event in data.invocation_events if data else []:
            names = names_by_owner.setdefault(event.author, {})
            for part in event.content.parts or [] if event.content else []:
                if part.function_call and part.function_call.name:
                    names.setdefault(part.function_call.name)
        missing = {
            owner: AgentDetails(
                name=owner,
                tool_declarations=[
                    genai_types.Tool(
                        function_declarations=[genai_types.FunctionDeclaration(name=name) for name in names]
                    )
                ]
                if names
                else [],
            )
            for owner, names in names_by_owner.items()
            if owner not in captured and (names or captured)
        }
        enriched.append(
            inv.model_copy(update={"app_details": AppDetails(agent_details={**captured, **missing})})
            if missing
            else inv
        )
    return enriched


def rubric_strings_to_objects(rubric_texts: list[str]) -> list[Rubric]:
    """Convert plain-text rubric strings into ADK Rubric objects."""
    return [
        Rubric(
            rubric_id=f"rubric_{i}",
            rubric_content=RubricContent(text_property=text),
        )
        for i, text in enumerate(rubric_texts)
    ]


def to_rubric_objects(rubrics: list[str | Rubric] | None) -> list[Rubric]:
    """Plain strings become positional rubrics; ADK ``Rubric`` objects pass through."""
    return [
        Rubric(rubric_id=f"rubric_{index}", rubric_content=RubricContent(text_property=r)) if isinstance(r, str) else r
        for index, r in enumerate(rubrics or [])
    ]


def attach_case_rubrics(
    metric_name: str,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    case_rubrics: list[Rubric] | None,
    criterion_rubrics: list[Rubric] | None = None,
) -> tuple[list[Invocation], list[str]]:
    """Bind case and invocation rubrics without changing ADK types or turn scope.

    Check effective properties before the first judge call, and report dropped
    properties by id and source instead of silently defaulting their type.
    """
    metric_type = RUBRIC_METRIC_TYPES.get(metric_name)
    attached = []
    warnings = []
    for index, actual in enumerate(actual_invocations):
        expected = expected_invocations[index] if expected_invocations and index < len(expected_invocations) else None
        sources = [
            (f"actual invocation {index}", actual.rubrics or []),
            ("eval case", case_rubrics or []),
            (f"expected invocation {index}", expected.rubrics or [] if expected else []),
        ]
        combined = [r for _, rubrics in sources for r in rubrics]
        for source, rubrics in sources:
            for rubric in rubrics:
                if rubric.type is None:
                    warning = (
                        f"{source} rubric '{rubric.rubric_id}' ignored: type {rubric.type!r}; "
                        f"metric requires {metric_type!r}"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
        effective = [*(criterion_rubrics or []), *(r for r in combined if r.type == metric_type)]
        ids = [r.rubric_id for r in effective]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Rubric ids must be distinct across metric, case and invocation {index}: {ids}")
        attached.append(actual.model_copy(update={"rubrics": combined}))
    return attached, warnings


def extract_rubric_details(eval_result: EvaluationResult) -> dict[str, Any]:
    """Per-rubric scores, per invocation and overall, from a rubric metric."""

    def _score(rubric_score: Any) -> dict[str, Any]:
        return {
            "rubric_id": rubric_score.rubric_id,
            "score": rubric_score.score,
            "rationale": rubric_score.rationale,
        }

    per_invocation = []
    for per_inv_result in eval_result.per_invocation_results:
        actual_inv = per_inv_result.actual_invocation
        per_invocation.append(
            {
                "invocation_id": actual_inv.invocation_id if actual_inv else None,
                "rubric_scores": [_score(r) for r in per_inv_result.rubric_scores or []],
            }
        )
    return {
        "per_invocation": per_invocation,
        "overall_rubric_scores": [_score(r) for r in eval_result.overall_rubric_scores or []],
    }


def build_eval_metric(
    metric_name: str,
    judge_model: str | None,
    threshold: float | None,
    rubrics: list[str | Rubric] | None = None,
    match_type: str | None = None,
) -> EvalMetric:
    """Construct an ADK ``EvalMetric`` with the appropriate criterion.

    ``rubrics`` feed the ``rubric_based_*`` metrics' criterion: plain strings
    get positional ids, ADK ``Rubric`` objects are used as given.
    """
    if metric_name in INFORMATIONAL_METRICS:
        return EvalMetric(metric_name=metric_name, threshold=threshold)

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
    elif metric_name in RUBRIC_METRIC_TYPES:
        judge_opts = JudgeModelOptions()
        if judge_model:
            judge_opts.judge_model = judge_model
        rubric_objects = to_rubric_objects(rubrics)
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
    rubrics: list[str | Rubric] | None = None,
    case_rubrics: list[Rubric] | None = None,
    model_calls: list[list[InvocationEvent]] | None = None,
):
    """Evaluate a built-in metric. Case rubrics apply to every invocation;
    invocation rubrics apply by position, with their ADK types unchanged.
    Unmatched golden comparisons remain NOT_EVALUATED.
    """
    from .runner import MetricResult

    if metric_name in METRICS_NEEDING_EXPECTED and not expected_invocations:
        return MetricResult(
            metric_name=metric_name,
            eval_status="NOT_EVALUATED",
            details={"reason": expected_reason or "no eval set provided"},
        )

    rubric_warnings: list[str] = []
    if metric_name in RUBRIC_METRIC_TYPES:
        rubric_objects = to_rubric_objects(rubrics)
        try:
            actual_invocations, rubric_warnings = attach_case_rubrics(
                metric_name, actual_invocations, expected_invocations, case_rubrics, rubric_objects
            )
        except ValueError as exc:
            return MetricResult(metric_name=metric_name, error=str(exc), error_type="ValueError")
        missing = [
            index
            for index, inv in enumerate(actual_invocations)
            if not rubric_objects and not any(r.type == RUBRIC_METRIC_TYPES[metric_name] for r in inv.rubrics or [])
        ]
        if missing or not actual_invocations:
            return MetricResult(
                metric_name=metric_name,
                error=(
                    f"Metric '{metric_name}' requires rubrics applicable to invocations {missing}: "
                    "set 'rubrics' on the evaluator in the eval config, or correctly typed 'rubrics' "
                    "on the matched eval case or its invocations."
                ),
                error_type="ValueError",
                details={"warnings": rubric_warnings} if rubric_warnings else None,
            )

    try:
        eval_metric = build_eval_metric(metric_name, judge_model, threshold, rubrics=rubrics, match_type=match_type)
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

        calls = model_calls or [[] for _ in actual_invocations]
        actual_invocations = _enrich_app_details(
            [_to_invocation_events(inv, c) for inv, c in zip(actual_invocations, calls, strict=True)]
        )
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
        per_inv_statuses = [r.eval_status.name for r in eval_result.per_invocation_results]

        details = None
        if metric_name == "tool_trajectory_avg_score":
            details = extract_trajectory_details(eval_result)
        elif metric_name in RUBRIC_METRIC_TYPES:
            details = extract_rubric_details(eval_result)
            if rubric_warnings:
                details["warnings"] = rubric_warnings

        return MetricResult(
            metric_name=metric_name,
            score=eval_result.overall_score,
            eval_status=eval_result.overall_eval_status.name,
            per_invocation_scores=per_inv_scores,
            per_invocation_statuses=per_inv_statuses,
            details=details,
        )

    except Exception as exc:
        logger.exception("Failed to evaluate metric '%s'", metric_name)
        return MetricResult(
            metric_name=metric_name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
