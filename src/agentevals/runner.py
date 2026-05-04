"""Evaluation runner — orchestrates trace loading, conversion, and scoring."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_set import EvalSet
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .builtin_metrics import evaluate_builtin_metric
from .config import (
    CustomEvaluatorDef,
    EvalParams,
    EvalRunConfig,
)
from .converter import ConversionResult, convert_traces
from .loader import load_traces
from .loader.base import Trace
from .trace_metrics import _calc_percentiles, extract_performance_metrics

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
TraceProgressCallback = Callable[["TraceResult"], Awaitable[None]]


class MetricResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    metric_name: str
    score: float | None = None
    eval_status: str = "NOT_EVALUATED"
    per_invocation_scores: list[float | None] = Field(default_factory=list)
    error: str | None = None
    details: dict[str, Any] | None = None
    duration_ms: float | None = None


class TraceResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    trace_id: str
    num_invocations: int = 0
    metric_results: list[MetricResult] = Field(default_factory=list)
    conversion_warnings: list[str] = Field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    trace_results: list[TraceResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None
    run_id: str | None = None


def load_eval_set(path: str) -> EvalSet:
    with open(path) as f:
        data = json.load(f)
    return EvalSet.model_validate(data)


def load_eval_set_from_dict(data: dict) -> EvalSet:
    """Parse an ADK EvalSet from a dict (for programmatic / API use)."""
    return EvalSet.model_validate(data)


async def run_evaluation_from_traces(
    traces: list[Trace],
    config: EvalParams,
    eval_set: EvalSet | None = None,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
) -> RunResult:
    """Evaluate pre-loaded traces. Skips file loading."""
    result = RunResult()

    if not traces:
        result.errors.append("No traces provided.")
        return result

    conversion_results = convert_traces(traces)

    trace_map = {t.trace_id: t for t in traces}

    perf_metrics_map: dict[str, dict[str, Any]] = {}
    for trace in traces:
        perf_metrics_map[trace.trace_id] = extract_performance_metrics(trace)

    total_traces = len(conversion_results)
    if progress_callback:
        await progress_callback(f"Evaluating {total_traces} trace{'s' if total_traces != 1 else ''}...")

    trace_semaphore = asyncio.Semaphore(config.max_concurrent_traces)
    eval_semaphore = asyncio.Semaphore(config.max_concurrent_evals)

    async def _evaluate_trace_bounded(idx: int, conv_result: ConversionResult) -> TraceResult:
        async with trace_semaphore:
            if progress_callback:
                trace_id_short = (
                    conv_result.trace_id[:12] + "..." if len(conv_result.trace_id) > 12 else conv_result.trace_id
                )
                await progress_callback(f"Trace {idx + 1}/{total_traces}: {trace_id_short}")

            trace = trace_map.get(conv_result.trace_id)

            return await _evaluate_trace(
                conv_result=conv_result,
                metrics=config.metrics,
                custom_evaluators=config.custom_evaluators,
                eval_set=eval_set,
                judge_model=config.judge_model,
                threshold=config.threshold,
                trajectory_match_type=config.trajectory_match_type,
                eval_semaphore=eval_semaphore,
                progress_callback=progress_callback,
                trace_progress_callback=trace_progress_callback,
                trace=trace,
                performance_metrics=perf_metrics_map.get(conv_result.trace_id),
            )

    trace_results = await asyncio.gather(
        *[_evaluate_trace_bounded(idx, conv_result) for idx, conv_result in enumerate(conversion_results)],
        return_exceptions=True,
    )

    for tr in trace_results:
        if isinstance(tr, Exception):
            logger.error("Unexpected error evaluating trace: %s", tr)
            result.errors.append(str(tr))
        else:
            result.trace_results.append(tr)

    if progress_callback:
        await progress_callback("Evaluation complete")

    if result.trace_results:
        all_tokens: dict[str, list[int]] = {"prompt": [], "output": [], "total": []}
        all_overall_latencies: list[float] = []
        total_llm_calls = 0
        total_tool_calls = 0
        all_models: set[str] = set()
        total_cache_creation = 0
        total_cache_read = 0

        for tr in result.trace_results:
            if tr.performance_metrics:
                perf = tr.performance_metrics
                all_tokens["prompt"].append(perf["tokens"]["total_prompt"])
                all_tokens["output"].append(perf["tokens"]["total_output"])
                all_tokens["total"].append(perf["tokens"]["total"])

                overall_lat = perf["latency"]["overall"]
                if overall_lat.get("count", 0) > 0:
                    all_overall_latencies.append(overall_lat["median"])

                counts = perf.get("counts", {})
                total_llm_calls += counts.get("llm_calls", 0)
                total_tool_calls += counts.get("tool_calls", 0)

                for m in perf.get("models", []):
                    all_models.add(m)

                total_cache_creation += perf["tokens"].get("cache_creation_tokens", 0)
                total_cache_read += perf["tokens"].get("cache_read_tokens", 0)

        if all_tokens["total"]:
            trace_count = len(result.trace_results)
            result.performance_metrics = {
                "tokens": {
                    "total_prompt": sum(all_tokens["prompt"]),
                    "total_output": sum(all_tokens["output"]),
                    "total": sum(all_tokens["total"]),
                    "avg_per_trace": {
                        "prompt": sum(all_tokens["prompt"]) / len(all_tokens["prompt"]),
                        "output": sum(all_tokens["output"]) / len(all_tokens["output"]),
                    },
                    "cache_creation_tokens": total_cache_creation,
                    "cache_read_tokens": total_cache_read,
                },
                "latency": {
                    "overall_per_trace": _calc_percentiles(all_overall_latencies),
                }
                if all_overall_latencies
                else {},
                "counts": {
                    "traces": trace_count,
                    "total_llm_calls": total_llm_calls,
                    "total_tool_calls": total_tool_calls,
                    "avg_llm_calls_per_trace": total_llm_calls / trace_count,
                    "avg_tool_calls_per_trace": total_tool_calls / trace_count,
                },
                "models": sorted(all_models) if all_models else [],
                "trace_count": trace_count,
            }

    return result


async def run_evaluation(
    config: EvalRunConfig,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
) -> RunResult:
    """Load traces from files, then evaluate. Delegates to ``run_evaluation_from_traces``."""
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


async def _evaluate_trace(
    conv_result: ConversionResult,
    metrics: list[str],
    custom_evaluators: list[CustomEvaluatorDef],
    eval_set: EvalSet | None,
    judge_model: str | None,
    threshold: float | None,
    eval_semaphore: asyncio.Semaphore,
    progress_callback: ProgressCallback | None = None,
    trace_progress_callback: TraceProgressCallback | None = None,
    trace=None,
    performance_metrics: dict[str, Any] | None = None,
    trajectory_match_type: str | None = None,
) -> TraceResult:
    trace_result = TraceResult(
        trace_id=conv_result.trace_id,
        num_invocations=len(conv_result.invocations),
        conversion_warnings=conv_result.warnings,
    )

    if performance_metrics:
        trace_result.performance_metrics = performance_metrics

    if not conv_result.invocations:
        trace_result.metric_results.append(
            MetricResult(
                metric_name="(all)",
                error="No invocations extracted from trace.",
            )
        )
        return trace_result

    actual_invocations = conv_result.invocations

    expected_invocations: list[Invocation] | None = None
    if eval_set:
        expected_invocations = _find_expected_invocations(actual_invocations, eval_set)

    async def _append_result(result: MetricResult) -> MetricResult:
        trace_result.metric_results.append(result)
        if trace_progress_callback:
            await trace_progress_callback(trace_result)
        return result

    async def _eval_builtin_with_semaphore(metric_name: str) -> MetricResult:
        async with eval_semaphore:
            if progress_callback:
                await progress_callback(f"Running {metric_name}...")
            t0 = time.monotonic()
            result = await evaluate_builtin_metric(
                metric_name=metric_name,
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
                judge_model=judge_model,
                threshold=threshold,
                match_type=trajectory_match_type,
            )
            result.duration_ms = (time.monotonic() - t0) * 1000
        return await _append_result(result)

    async def _eval_custom_with_semaphore(evaluator_def: CustomEvaluatorDef) -> MetricResult:
        async with eval_semaphore:
            if progress_callback:
                await progress_callback(f"Running {evaluator_def.name}...")
            from .custom_evaluators import evaluate_custom_evaluator

            t0 = time.monotonic()
            result = await evaluate_custom_evaluator(
                evaluator_def=evaluator_def,
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
                performance_metrics=performance_metrics,
            )
            result.duration_ms = (time.monotonic() - t0) * 1000
        return await _append_result(result)

    tasks = [_eval_builtin_with_semaphore(m) for m in metrics]
    tasks.extend(_eval_custom_with_semaphore(g) for g in custom_evaluators)

    await asyncio.gather(*tasks)

    return trace_result


def _find_expected_invocations(
    actual_invocations: list[Invocation],
    eval_set: EvalSet,
) -> list[Invocation] | None:
    """Match actual invocations to an eval case. Uses the sole eval case if
    there's only one, otherwise matches by user content text."""
    if not eval_set.eval_cases:
        return None

    if len(eval_set.eval_cases) == 1:
        case = eval_set.eval_cases[0]
        if case.conversation:
            return case.conversation
        return None

    actual_user_text = _get_user_text(actual_invocations[0]) if actual_invocations else None
    if not actual_user_text:
        case = eval_set.eval_cases[0]
        return case.conversation if case.conversation else None

    for case in eval_set.eval_cases:
        if not case.conversation:
            continue
        expected_user_text = _get_user_text(case.conversation[0])
        if expected_user_text and _text_matches(actual_user_text, expected_user_text):
            return case.conversation

    logger.warning(
        "No matching eval case found for user text: '%s'. Using first eval case.",
        actual_user_text[:100],
    )
    case = eval_set.eval_cases[0]
    return case.conversation if case.conversation else None


def _get_user_text(invocation: Invocation) -> str | None:
    if not invocation.user_content or not invocation.user_content.parts:
        return None
    texts = [p.text for p in invocation.user_content.parts if p.text]
    return " ".join(texts) if texts else None


def _text_matches(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()
