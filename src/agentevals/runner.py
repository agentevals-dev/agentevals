"""Evaluation runner — orchestrates trace loading, conversion, and scoring."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from google.adk.evaluation.eval_case import EvalCase, Invocation, get_all_tool_calls
from google.adk.evaluation.eval_metrics import (
    BaseCriterion,
    EvalMetric,
    JudgeModelOptions,
    LlmAsAJudgeCriterion,
    HallucinationsCriterion,
    RubricsBasedCriterion,
    ToolTrajectoryCriterion,
)
from google.adk.evaluation.eval_set import EvalSet
from google.adk.evaluation.evaluator import EvaluationResult, Evaluator

from .config import EvalRunConfig
from .converter import (
    ConversionResult,
    convert_traces,
    _detect_trace_format,
    _find_adk_spans,
    _parse_json_tag,
)
from .utils.genai_messages import (
    parse_json_attr,
    extract_text_from_message,
    USER_ROLES,
    ASSISTANT_ROLES,
)
from .loader.base import TraceLoader
from .loader.jaeger import JaegerJsonLoader
from .loader.otlp import OtlpJsonLoader

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
TraceProgressCallback = Callable[["TraceResult"], Awaitable[None]]

_METRICS_NEEDING_EXPECTED = {
    "tool_trajectory_avg_score",
    "response_match_score",
    "response_evaluation_score",
    "final_response_match_v2",
}

_METRICS_NEEDING_LLM = {
    "final_response_match_v2",
    "rubric_based_final_response_quality_v1",
    "hallucinations_v1",
    "safety_v1",
    "rubric_based_tool_use_quality_v1",
    "per_turn_user_simulator_quality_v1",
}


@dataclass
class MetricResult:
    metric_name: str
    score: float | None = None
    eval_status: str = "NOT_EVALUATED"
    per_invocation_scores: list[float | None] = field(default_factory=list)
    error: str | None = None
    details: dict[str, Any] | None = None  # Additional details (e.g., expected vs actual)


@dataclass
class TraceResult:
    trace_id: str
    num_invocations: int = 0
    metric_results: list[MetricResult] = field(default_factory=list)
    conversion_warnings: list[str] = field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None


@dataclass
class RunResult:
    trace_results: list[TraceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    performance_metrics: dict[str, Any] | None = None


def get_loader(format_name: str) -> TraceLoader:
    loaders: dict[str, type[TraceLoader]] = {
        "jaeger-json": JaegerJsonLoader,
        "otlp-json": OtlpJsonLoader,
    }
    if format_name not in loaders:
        raise ValueError(
            f"Unknown trace format '{format_name}'. Available: {list(loaders.keys())}"
        )
    return loaders[format_name]()


def load_eval_set(path: str) -> EvalSet:
    with open(path, "r") as f:
        data = json.load(f)
    return EvalSet.model_validate(data)


async def run_evaluation(
    config: EvalRunConfig,
    progress_callback: Optional[ProgressCallback] = None,
    trace_progress_callback: Optional[TraceProgressCallback] = None,
) -> RunResult:
    result = RunResult()

    loader = get_loader(config.trace_format)
    all_traces = []
    for trace_file in config.trace_files:
        try:
            traces = loader.load(trace_file)
            all_traces.extend(traces)
        except Exception as exc:
            msg = f"Failed to load trace file '{trace_file}': {exc}"
            logger.error(msg)
            result.errors.append(msg)

    if not all_traces:
        result.errors.append("No traces loaded.")
        return result

    conversion_results = convert_traces(all_traces)

    trace_map = {t.trace_id: t for t in all_traces}

    perf_metrics_map: dict[str, dict[str, Any]] = {}
    for trace in all_traces:
        perf_metrics_map[trace.trace_id] = _extract_performance_metrics(trace)

    eval_set: EvalSet | None = None
    if config.eval_set_file:
        try:
            eval_set = load_eval_set(config.eval_set_file)
        except Exception as exc:
            msg = f"Failed to load eval set '{config.eval_set_file}': {exc}"
            logger.error(msg)
            result.errors.append(msg)

    total_traces = len(conversion_results)
    if progress_callback:
        await progress_callback(f"Evaluating {total_traces} trace{'s' if total_traces != 1 else ''}...")

    for idx, conv_result in enumerate(conversion_results):
        if progress_callback:
            trace_id_short = conv_result.trace_id[:12] + "..." if len(conv_result.trace_id) > 12 else conv_result.trace_id
            await progress_callback(f"Trace {idx + 1}/{total_traces}: {trace_id_short}")

        trace = trace_map.get(conv_result.trace_id)

        trace_result = await _evaluate_trace(
            conv_result=conv_result,
            metrics=config.metrics,
            eval_set=eval_set,
            judge_model=config.judge_model,
            threshold=config.threshold,
            progress_callback=progress_callback,
            trace_progress_callback=trace_progress_callback,
            trace=trace,
            performance_metrics=perf_metrics_map.get(conv_result.trace_id),
        )

        result.trace_results.append(trace_result)

    if progress_callback:
        await progress_callback("Evaluation complete")

    if result.trace_results:
        all_tokens = {"prompt": [], "output": [], "total": []}

        for tr in result.trace_results:
            if tr.performance_metrics:
                perf = tr.performance_metrics
                all_tokens["prompt"].append(perf["tokens"]["total_prompt"])
                all_tokens["output"].append(perf["tokens"]["total_output"])
                all_tokens["total"].append(perf["tokens"]["total"])

        if all_tokens["total"]:
            result.performance_metrics = {
                "tokens": {
                    "total_prompt": sum(all_tokens["prompt"]),
                    "total_output": sum(all_tokens["output"]),
                    "total": sum(all_tokens["total"]),
                    "avg_per_trace": {
                        "prompt": sum(all_tokens["prompt"]) / len(all_tokens["prompt"]),
                        "output": sum(all_tokens["output"]) / len(all_tokens["output"]),
                    },
                },
                "trace_count": len(result.trace_results),
            }

    return result


async def _evaluate_trace(
    conv_result: ConversionResult,
    metrics: list[str],
    eval_set: EvalSet | None,
    judge_model: str | None,
    threshold: float | None,
    progress_callback: Optional[ProgressCallback] = None,
    trace_progress_callback: Optional[TraceProgressCallback] = None,
    trace = None,
    performance_metrics: dict[str, Any] | None = None,
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

    for metric_name in metrics:
        if progress_callback:
            await progress_callback(f"Running {metric_name}...")

        metric_result = await _evaluate_metric(
            metric_name=metric_name,
            actual_invocations=actual_invocations,
            expected_invocations=expected_invocations,
            judge_model=judge_model,
            threshold=threshold,
        )
        trace_result.metric_results.append(metric_result)

        if trace_progress_callback:
            await trace_progress_callback(trace_result)

    return trace_result


def _extract_trajectory_details(eval_result: EvaluationResult) -> dict[str, Any]:
    """Extract expected vs actual tool call details from trajectory evaluation."""
    comparisons = []

    for per_inv_result in eval_result.per_invocation_results:
        actual_inv = per_inv_result.actual_invocation
        expected_inv = per_inv_result.expected_invocation

        actual_tools = []
        expected_tools = []

        if actual_inv and actual_inv.intermediate_data:
            tool_calls = get_all_tool_calls(actual_inv.intermediate_data)
            actual_tools = [
                {"name": tc.name, "args": tc.args}
                for tc in tool_calls
            ]

        if expected_inv and expected_inv.intermediate_data:
            tool_calls = get_all_tool_calls(expected_inv.intermediate_data)
            expected_tools = [
                {"name": tc.name, "args": tc.args}
                for tc in tool_calls
            ]

        comparisons.append({
            "invocation_id": actual_inv.invocation_id if actual_inv else None,
            "expected": expected_tools,
            "actual": actual_tools,
            "matched": per_inv_result.score == 1.0,
        })

    return {"comparisons": comparisons}


async def _evaluate_metric(
    metric_name: str,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    judge_model: str | None,
    threshold: float | None,
) -> MetricResult:
    if metric_name in _METRICS_NEEDING_EXPECTED and not expected_invocations:
        return MetricResult(
            metric_name=metric_name,
            error=(
                f"Metric '{metric_name}' requires expected invocations "
                f"(golden eval set), but none were provided or matched."
            ),
        )

    try:
        eval_metric = _build_eval_metric(metric_name, judge_model, threshold)
        evaluator: Evaluator = _get_evaluator(eval_metric)

        if inspect.iscoroutinefunction(evaluator.evaluate_invocations):
            eval_result: EvaluationResult = await evaluator.evaluate_invocations(
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )
        else:
            eval_result: EvaluationResult = evaluator.evaluate_invocations(
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )

        per_inv_scores = [r.score for r in eval_result.per_invocation_results]

        details = None
        if metric_name == "tool_trajectory_avg_score":
            details = _extract_trajectory_details(eval_result)

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


def _build_eval_metric(
    metric_name: str,
    judge_model: str | None,
    threshold: float | None,
) -> EvalMetric:
    effective_threshold = threshold if threshold is not None else 0.5

    criterion: BaseCriterion | None = None

    if metric_name == "tool_trajectory_avg_score":
        criterion = ToolTrajectoryCriterion(threshold=effective_threshold)
    elif metric_name in (
        "final_response_match_v2",
        "safety_v1",
    ):
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
        criterion = RubricsBasedCriterion(
            threshold=effective_threshold,
            judge_model_options=judge_opts,
        )
    elif metric_name in ("response_match_score", "response_evaluation_score"):
        criterion = BaseCriterion(threshold=effective_threshold)

    return EvalMetric(
        metric_name=metric_name,
        threshold=effective_threshold,
        criterion=criterion,
    )


def _get_evaluator(eval_metric: EvalMetric) -> Evaluator:
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

    # Full registry — may trigger heavy imports (numpy, rouge_score, etc.)
    from google.adk.evaluation.metric_evaluator_registry import (
        DEFAULT_METRIC_EVALUATOR_REGISTRY,
    )

    return DEFAULT_METRIC_EVALUATOR_REGISTRY.get_evaluator(eval_metric)


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

    actual_user_text = (
        _get_user_text(actual_invocations[0]) if actual_invocations else None
    )
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


def _extract_performance_metrics(trace) -> dict[str, Any]:
    """Extract latency and token usage metrics from trace spans."""
    agent_latencies = []
    llm_latencies = []
    tool_latencies = []
    prompt_tokens = []
    output_tokens = []
    total_tokens = []

    has_adk_invoke = any(s.operation_name.startswith("invoke_agent") for s in trace.all_spans)

    if not has_adk_invoke and trace.root_spans:
        for root_span in trace.root_spans:
            root_duration_ms = root_span.duration / 1000.0
            agent_latencies.append(root_duration_ms)

    for span in trace.all_spans:
        duration_ms = span.duration / 1000.0

        is_adk_invoke = span.operation_name.startswith("invoke_agent")
        is_adk_llm = span.operation_name.startswith("call_llm")
        is_adk_tool = span.operation_name.startswith("execute_tool")
        is_genai_llm = span.tags.get("gen_ai.request.model") is not None
        is_genai_tool = span.tags.get("gen_ai.tool.name") is not None

        if is_adk_invoke:
            agent_latencies.append(duration_ms)
        elif is_adk_llm or is_genai_llm:
            llm_latencies.append(duration_ms)

            if is_adk_llm:
                llm_response_raw = span.tags.get("gcp.vertex.agent.llm_response", "{}")
                try:
                    if isinstance(llm_response_raw, dict):
                        llm_response = llm_response_raw
                    else:
                        llm_response = json.loads(llm_response_raw) if llm_response_raw else {}

                    usage = llm_response.get("usage_metadata", {})
                    if usage:
                        prompt_tokens.append(usage.get("prompt_token_count", 0))
                        output_tokens.append(usage.get("candidates_token_count", 0))
                        total_tokens.append(usage.get("total_token_count", 0))
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
            elif is_genai_llm:
                input_toks = span.tags.get("gen_ai.usage.input_tokens", 0)
                output_toks = span.tags.get("gen_ai.usage.output_tokens", 0)
                if isinstance(input_toks, (int, float)) and isinstance(output_toks, (int, float)):
                    prompt_tokens.append(int(input_toks))
                    output_tokens.append(int(output_toks))
                    total_tokens.append(int(input_toks) + int(output_toks))

        elif is_adk_tool or is_genai_tool:
            tool_latencies.append(duration_ms)

    def calc_percentiles(values: list[float]) -> dict[str, float]:
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        import statistics
        sorted_values = sorted(values)
        n = len(sorted_values)
        return {
            "p50": statistics.median(sorted_values),
            "p95": sorted_values[int(n * 0.95)] if n > 1 else sorted_values[0],
            "p99": sorted_values[int(n * 0.99)] if n > 1 else sorted_values[0],
        }

    return {
        "latency": {
            "overall": calc_percentiles(agent_latencies),
            "llm_calls": calc_percentiles(llm_latencies),
            "tool_executions": calc_percentiles(tool_latencies),
        },
        "tokens": {
            "total_prompt": sum(prompt_tokens) if prompt_tokens else 0,
            "total_output": sum(output_tokens) if output_tokens else 0,
            "total": sum(total_tokens) if total_tokens else 0,
            "per_llm_call": calc_percentiles(total_tokens) if total_tokens else {"p50": 0.0, "p95": 0.0, "p99": 0.0},
        },
    }


def _truncate(text: str, max_length: int = 200) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def _extract_trace_metadata(trace) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "agent_name": None,
        "model": None,
        "start_time": None,
        "user_input_preview": None,
        "final_output_preview": None,
    }

    trace_format = _detect_trace_format(trace)

    if trace_format == "adk":
        invoke_spans = _find_adk_spans(trace, "invoke_agent")
        if invoke_spans:
            metadata["agent_name"] = invoke_spans[0].get_tag("gen_ai.agent.name")
            metadata["start_time"] = invoke_spans[0].start_time

        call_llm_spans = _find_adk_spans(trace, "call_llm")
        if call_llm_spans:
            metadata["model"] = call_llm_spans[0].get_tag("gen_ai.request.model")

            llm_request_raw = call_llm_spans[0].get_tag("gcp.vertex.agent.llm_request", "{}")
            llm_request = _parse_json_tag(llm_request_raw, "llm_request")
            for content in reversed(llm_request.get("contents", [])):
                if content.get("role") != "user":
                    continue
                text_parts = [p["text"] for p in content.get("parts", []) if "text" in p]
                if text_parts:
                    metadata["user_input_preview"] = _truncate(" ".join(text_parts))
                    break

            last_llm = call_llm_spans[-1]
            llm_response_raw = last_llm.get_tag("gcp.vertex.agent.llm_response", "{}")
            llm_response = _parse_json_tag(llm_response_raw, "llm_response")
            response_content = llm_response.get("content", {})
            text_parts = [p["text"] for p in response_content.get("parts", []) if "text" in p]
            if text_parts:
                metadata["final_output_preview"] = _truncate(" ".join(text_parts))
    else:
        llm_spans = [s for s in trace.all_spans if s.get_tag("gen_ai.request.model") is not None]
        if llm_spans:
            metadata["model"] = llm_spans[0].get_tag("gen_ai.request.model")
            metadata["start_time"] = llm_spans[0].start_time

            agent_name = llm_spans[0].get_tag("gen_ai.agent.name")
            if agent_name:
                metadata["agent_name"] = agent_name
            elif trace.root_spans:
                metadata["agent_name"] = trace.root_spans[0].operation_name

            messages_raw = llm_spans[0].get_tag("gen_ai.input.messages", "[]")
            messages = parse_json_attr(messages_raw, "gen_ai.input.messages")
            if isinstance(messages, list):
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") in USER_ROLES:
                        text = extract_text_from_message(msg)
                        if text:
                            metadata["user_input_preview"] = _truncate(text)
                            break

            out_raw = llm_spans[-1].get_tag("gen_ai.output.messages", "[]")
            out_messages = parse_json_attr(out_raw, "gen_ai.output.messages")
            if isinstance(out_messages, list):
                for msg in reversed(out_messages):
                    if isinstance(msg, dict) and msg.get("role") in ASSISTANT_ROLES:
                        text = extract_text_from_message(msg)
                        if text:
                            metadata["final_output_preview"] = _truncate(text)
                            break

    return metadata
