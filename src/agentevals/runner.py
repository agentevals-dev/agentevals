"""Evaluation runner: groups traces into conversations, selects goldens, runs evaluators."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .adk_bridge import (
    EvalSet,
    expected_conversations,
    load_eval_set,
    load_eval_set_from_dict,
    multi_turn_cases,
)
from .config import EvalParams, EvalRunConfig, EvaluatorDef
from .genai.extract import extract_conversation
from .genai.grouping import coerce_key, group_traces, has_session_name
from .genai.matching import EVAL_CASE_ID, ExpectedConversation, select_case
from .genai.model import Conversation
from .loader import load_traces
from .otel import emit
from .otel.model import Trace
from .trace_metrics import _calc_percentiles, extract_agent_identity, extract_performance_metrics

__all__ = [
    "MetricResult",
    "RunResult",
    "TraceResult",
    "evaluation_groups",
    "load_eval_set",
    "load_eval_set_from_dict",
    "run_evaluation",
    "run_evaluation_from_traces",
]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
TraceProgressCallback = Callable[["TraceResult"], Awaitable[None]]


class MetricResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    metric_name: str
    score: float | None = None
    eval_status: str = "NOT_EVALUATED"
    per_invocation_scores: list[float | None] = Field(default_factory=list)
    per_invocation_statuses: list[str] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    details: dict[str, Any] | None = None
    duration_ms: float | None = None


class TurnRef(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    trace_id: str
    span_id: str


class TraceResult(BaseModel):
    """Results for one evaluation group: a trace, or a conversation of several traces.

    ``trace_id`` is the group's earliest trace, so single trace groups read as before.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    trace_id: str
    num_invocations: int = 0
    metric_results: list[MetricResult] = Field(default_factory=list)
    conversion_warnings: list[str] = Field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None
    service_name: str | None = None
    agent_name: str | None = None
    trace_ids: list[str] = Field(default_factory=list)
    conversation_id: str | None = None
    group_key: str | None = None
    eval_case_id: str | None = None
    eval_case_match: str | None = None
    turn_refs: list[TurnRef] = Field(default_factory=list)


class RunResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    trace_results: list[TraceResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None
    run_id: str | None = None


def evaluation_groups(
    traces: list[Trace], group_by: str, eval_set: EvalSet | None = None
) -> list[tuple[str, list[Trace]]]:
    """The evaluation groups for ``group_by``: one per trace, or one per conversation key.

    ``auto`` groups by conversation when any trace carries ``agentevals.session_name`` or the eval
    set has a case with more than one invocation. Every view of the same traces (results, early
    performance events, ``/api/convert`` with ``group_by``) uses this, so they line up by trace id.
    """
    if group_by == "auto":
        by_conversation = has_session_name(traces) or multi_turn_cases(eval_set)
    else:
        by_conversation = group_by == "conversation"
    return group_traces(traces, by_conversation)


def _explicit_case_id(traces: list[Trace]) -> str | None:
    for trace in traces:
        for span in trace.spans.values():
            for attrs in (span.attributes, span.resource.attributes):
                value = coerce_key(attrs.get(EVAL_CASE_ID))
                if value:
                    return value
    return None


async def run_evaluation_from_traces(
    traces: list[Trace],
    config: EvalParams,
    eval_set: EvalSet | None = None,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
    group_key: str | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Evaluate already loaded traces. ``group_key`` evaluates all of them as one conversation.

    When evaluation result events are on (:mod:`agentevals.otel.emit`), each group's results are
    emitted as ``gen_ai.evaluation.result`` events parented to the evaluated spans; ``run_id`` is
    attached to them when known.
    """
    result = RunResult()
    if not traces:
        result.errors.append("No traces provided.")
        return result

    cases = expected_conversations(eval_set)
    if group_key is not None:
        groups = [(group_key, sorted(traces, key=lambda t: t.start_time_unix_nano))]
    else:
        groups = evaluation_groups(traces, config.group_by, eval_set)
    total = len(groups)
    if progress_callback:
        await progress_callback(f"Evaluating {total} trace{'s' if total != 1 else ''}...")

    group_semaphore = asyncio.Semaphore(config.max_concurrent_traces)
    eval_semaphore = asyncio.Semaphore(config.max_concurrent_evals)

    async def _bounded(idx: int, key: str, members: list[Trace]) -> TraceResult:
        async with group_semaphore:
            if progress_callback:
                short = key[:12] + "..." if len(key) > 12 else key
                await progress_callback(f"Trace {idx + 1}/{total}: {short}")
            return await _evaluate_group(
                key=key,
                traces=members,
                evaluators=config.evaluators,
                eval_set=eval_set,
                cases=cases,
                single_pairing=len(cases) == 1 and total == 1,
                eval_semaphore=eval_semaphore,
                progress_callback=progress_callback,
                trace_progress_callback=trace_progress_callback,
                run_id=run_id,
            )

    outcomes = await asyncio.gather(
        *[_bounded(i, key, members) for i, (key, members) in enumerate(groups)],
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            logger.error("Unexpected error evaluating trace group: %s", outcome)
            result.errors.append(str(outcome))
        else:
            result.trace_results.append(outcome)

    if progress_callback:
        await progress_callback("Evaluation complete")
    result.performance_metrics = _aggregate_performance(result.trace_results)
    return result


async def run_evaluation(
    config: EvalRunConfig,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
) -> RunResult:
    """Load trace files, then evaluate them with :func:`run_evaluation_from_traces`."""
    load_errors: list[str] = []
    all_traces: list[Trace] = []
    for trace_file in config.trace_files:
        try:
            all_traces.extend(load_traces(trace_file, format=config.trace_format))
        except Exception as exc:
            msg = f"Failed to load trace file '{trace_file}': {exc}"
            logger.error(msg)
            load_errors.append(msg)

    if not all_traces:
        return RunResult(errors=[*load_errors, "No traces loaded."])

    eval_set: EvalSet | None = None
    if config.eval_set_file:
        try:
            eval_set = load_eval_set(config.eval_set_file)
        except Exception as exc:
            msg = f"Failed to load eval set '{config.eval_set_file}': {exc}"
            logger.error(msg)
            load_errors.append(msg)

    result = await run_evaluation_from_traces(
        traces=all_traces,
        config=config,
        eval_set=eval_set,
        progress_callback=progress_callback,
        trace_progress_callback=trace_progress_callback,
    )
    if load_errors:
        result.errors = load_errors + result.errors
    return result


async def _evaluate_group(
    key: str,
    traces: list[Trace],
    evaluators: list[EvaluatorDef],
    eval_set: EvalSet | None,
    cases: list[ExpectedConversation],
    eval_semaphore: asyncio.Semaphore,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
    single_pairing: bool = False,
    run_id: str | None = None,
) -> TraceResult:
    trace_result = TraceResult(trace_id=traces[0].trace_id, group_key=key, trace_ids=[t.trace_id for t in traces])
    try:
        conversation: Conversation = extract_conversation(traces, key)
        trace_result.performance_metrics = extract_performance_metrics(conversation, traces)
        identity = extract_agent_identity(traces, conversation)
    except Exception as exc:
        logger.exception("Failed to extract turns for trace group %s", key)
        trace_result.metric_results.append(MetricResult(metric_name="(all)", error=f"Turn extraction failed: {exc}"))
        return trace_result

    trace_result.service_name = identity["service_name"]
    trace_result.agent_name = identity["agent_name"]
    trace_result.num_invocations = len(conversation.turns)
    trace_result.conversation_id = conversation.conversation_id
    trace_result.turn_refs = [TurnRef(trace_id=t.ref.trace_id, span_id=t.ref.span_id) for t in conversation.turns]
    trace_result.conversion_warnings = sorted(
        {w for trace in traces for w in trace.warnings} | {w for t in conversation.turns for w in t.warnings}
    )

    if not conversation.turns:
        trace_result.metric_results.append(
            MetricResult(metric_name="(all)", error="No invocations extracted from trace.")
        )
        return trace_result

    expected: ExpectedConversation | None = None
    expected_reason: str | None = "no eval set provided"
    if eval_set is not None:
        match = select_case(conversation, cases, _explicit_case_id(traces), single_pairing=single_pairing)
        expected, expected_reason = match.case, match.reason
        if expected is not None:
            trace_result.eval_case_id = expected.case_id
            trace_result.eval_case_match = match.method
        else:
            logger.warning("Trace group %s: %s", key, match.reason)

    async def _run(evaluator_def: EvaluatorDef) -> None:
        async with eval_semaphore:
            if progress_callback:
                await progress_callback(f"Running {evaluator_def.name}...")
            from .custom_evaluators import evaluate_custom_evaluator

            t0 = time.monotonic()
            metric = await evaluate_custom_evaluator(
                evaluator_def=evaluator_def,
                actual=conversation,
                expected=expected,
                performance_metrics=trace_result.performance_metrics,
                expected_reason=expected_reason,
            )
            metric.duration_ms = (time.monotonic() - t0) * 1000
        trace_result.metric_results.append(metric)
        if trace_progress_callback:
            await trace_progress_callback(trace_result)

    await asyncio.gather(*[_run(e) for e in evaluators])

    emitter = emit.get_emitter()
    if emitter is not None:
        emitter.emit(
            conversation=conversation,
            metrics=trace_result.metric_results,
            evaluators=evaluators,
            spans={(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()},
            eval_case_id=trace_result.eval_case_id,
            eval_set_id=eval_set.eval_set_id if eval_set is not None else None,
            run_id=run_id,
        )
    return trace_result


def _aggregate_performance(trace_results: list[TraceResult]) -> dict[str, Any] | None:
    perfs = [tr.performance_metrics for tr in trace_results if tr.performance_metrics]
    if not perfs:
        return None
    prompt = [p["tokens"]["total_prompt"] for p in perfs]
    output = [p["tokens"]["total_output"] for p in perfs]
    total = [p["tokens"]["total"] for p in perfs]
    latencies = [p["latency"]["overall"]["median"] for p in perfs if p["latency"]["overall"].get("count", 0) > 0]
    llm_calls = sum(p["counts"].get("llm_calls", 0) for p in perfs)
    tool_calls = sum(p["counts"].get("tool_calls", 0) for p in perfs)
    models = sorted({m for p in perfs for m in p.get("models", [])})
    count = len(trace_results)
    return {
        "tokens": {
            "total_prompt": sum(prompt),
            "total_output": sum(output),
            "total": sum(total),
            "avg_per_trace": {"prompt": sum(prompt) / len(prompt), "output": sum(output) / len(output)},
            "cache_creation_tokens": sum(p["tokens"].get("cache_creation_tokens", 0) for p in perfs),
            "cache_read_tokens": sum(p["tokens"].get("cache_read_tokens", 0) for p in perfs),
        },
        "latency": {"overall_per_trace": _calc_percentiles(latencies)} if latencies else {},
        "counts": {
            "traces": count,
            "total_llm_calls": llm_calls,
            "total_tool_calls": tool_calls,
            "avg_llm_calls_per_trace": llm_calls / count,
            "avg_tool_calls_per_trace": tool_calls / count,
        },
        "models": models,
        "trace_count": count,
    }
