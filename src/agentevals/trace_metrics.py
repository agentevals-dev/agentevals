"""Extract performance and metadata from trace spans."""

from __future__ import annotations

from typing import Any

from .extraction import (
    extract_agent_response_from_attrs,
    extract_token_usage_from_attrs,
    extract_user_text_from_attrs,
    get_extractor,
)
from .trace_attrs import OTEL_GENAI_AGENT_NAME, OTEL_GENAI_REQUEST_MODEL


def _truncate(text: str, max_length: int = 200) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def _calc_percentiles(values: list[float]) -> dict[str, float]:
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


def extract_performance_metrics(trace, extractor=None) -> dict[str, Any]:
    """Extract latency and token usage metrics from trace spans."""
    agent_latencies = []
    llm_latencies = []
    tool_latencies = []
    prompt_tokens = []
    output_tokens = []
    total_tokens = []

    if extractor is None:
        extractor = get_extractor(trace)
    invocation_spans = extractor.find_invocation_spans(trace)

    if not invocation_spans and trace.root_spans:
        for root_span in trace.root_spans:
            agent_latencies.append(root_span.duration / 1000.0)

    for inv_span in invocation_spans:
        agent_latencies.append(inv_span.duration / 1000.0)

    for span in trace.all_spans:
        duration_ms = span.duration / 1000.0
        role = extractor.classify_span(span)

        if role == "llm":
            llm_latencies.append(duration_ms)
            in_toks, out_toks, _ = extract_token_usage_from_attrs(span.tags)
            if in_toks or out_toks:
                prompt_tokens.append(in_toks)
                output_tokens.append(out_toks)
                total_tokens.append(in_toks + out_toks)
        elif role == "tool":
            tool_latencies.append(duration_ms)

    return {
        "latency": {
            "overall": _calc_percentiles(agent_latencies),
            "llm_calls": _calc_percentiles(llm_latencies),
            "tool_executions": _calc_percentiles(tool_latencies),
        },
        "tokens": {
            "total_prompt": sum(prompt_tokens) if prompt_tokens else 0,
            "total_output": sum(output_tokens) if output_tokens else 0,
            "total": sum(total_tokens) if total_tokens else 0,
            "per_llm_call": _calc_percentiles(total_tokens) if total_tokens else {"p50": 0.0, "p95": 0.0, "p99": 0.0},
        },
    }


def extract_trace_metadata(trace, extractor=None) -> dict[str, Any]:
    """Extract agent name, model, timing, and preview text from a trace."""
    metadata: dict[str, Any] = {
        "agent_name": None,
        "model": None,
        "start_time": None,
        "user_input_preview": None,
        "final_output_preview": None,
    }

    if extractor is None:
        extractor = get_extractor(trace)
    invocation_spans = extractor.find_invocation_spans(trace)

    if invocation_spans:
        first_inv = invocation_spans[0]
        metadata["agent_name"] = first_inv.get_tag(OTEL_GENAI_AGENT_NAME)
        metadata["start_time"] = first_inv.start_time

        llm_spans = extractor.find_llm_spans_in(first_inv)
        if llm_spans:
            metadata["model"] = llm_spans[0].get_tag(OTEL_GENAI_REQUEST_MODEL)

            user_text = extract_user_text_from_attrs(llm_spans[0].tags)
            if user_text:
                metadata["user_input_preview"] = _truncate(user_text)

            agent_text = extract_agent_response_from_attrs(llm_spans[-1].tags)
            if agent_text:
                metadata["final_output_preview"] = _truncate(agent_text)

    if not metadata["agent_name"] and trace.root_spans:
        metadata["agent_name"] = trace.root_spans[0].operation_name

    if not metadata["model"]:
        for span in trace.all_spans:
            model = span.get_tag(OTEL_GENAI_REQUEST_MODEL)
            if model:
                metadata["model"] = model
                break

    return metadata
