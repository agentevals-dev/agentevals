"""Performance metrics and metadata derived from extracted conversations.

Counts and token totals come from logical LLM calls, so a wrapper span over a provider span
is counted once. Latencies come from the spans each turn, call and tool call refer to.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from typing import Any

from .genai import semconv as sc
from .genai.messages import text_of
from .genai.model import Conversation
from .otel.model import Span, Trace, attr_str


def _calc_percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    sorted_values = sorted(values)
    n = len(sorted_values)
    return {
        "p50": statistics.median(sorted_values),
        "p95": sorted_values[int(n * 0.95)] if n > 1 else sorted_values[0],
        "p99": sorted_values[int(n * 0.99)] if n > 1 else sorted_values[0],
    }


def _calc_summary_stats(values: list[float]) -> dict[str, float | int]:
    """min/median/max/count plus the p50/p95/p99 keys earlier clients read."""
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0, "count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    sorted_values = sorted(values)
    n = len(sorted_values)
    med = statistics.median(sorted_values)
    return {
        "min": sorted_values[0],
        "median": med,
        "max": sorted_values[-1],
        "count": n,
        "p50": med,
        "p95": sorted_values[int(n * 0.95)] if n > 1 else sorted_values[0],
        "p99": sorted_values[int(n * 0.99)] if n > 1 else sorted_values[0],
    }


_SCHEMA_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def schema_version(schema_url: str | None) -> str | None:
    """The last path segment of a schema URL when it is a version (``1.37.0``); never raises."""
    if not schema_url or not isinstance(schema_url, str):
        return None
    last = schema_url.rstrip("/").rsplit("/", 1)[-1]
    return last if _SCHEMA_VERSION.match(last) else None


def _truncate(text: str, max_length: int = 200) -> str:
    return text if len(text) <= max_length else text[:max_length] + "..."


def _span_index(traces: Sequence[Trace]) -> dict[tuple[str, str], Span]:
    return {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}


def _ms(span: Span | None) -> float | None:
    return span.duration_unix_nano / 1e6 if span is not None else None


def first_service_name(traces: Sequence[Trace]) -> str | None:
    """``service.name`` of the first span's resource: the stable cross framework grouping key."""
    for trace in traces:
        for span in trace.spans.values():
            name = attr_str(span.resource.attributes, sc.SERVICE_NAME)
            if name:
                return name
    return None


def extract_agent_identity(traces: Sequence[Trace], conversation: Conversation) -> dict[str, str | None]:
    """Prefers ``service.name``; the agent name is a real ``gen_ai.agent.name`` or nothing."""
    agent = next((t.agent_name for t in conversation.turns if t.agent_name), None)
    return {"service_name": first_service_name(traces), "agent_name": agent}


def extract_performance_metrics(conversation: Conversation, traces: Sequence[Trace]) -> dict[str, Any]:
    spans = _span_index(traces)
    turn_latencies = [(t.end_ns - t.start_ns) / 1e6 for t in conversation.turns]
    calls = [c for t in conversation.turns for c in t.llm_calls]
    call_latencies = [(c.end_ns - c.start_ns) / 1e6 for c in calls]
    tools = [tc for t in conversation.turns for tc in t.tool_calls]
    tool_latencies = [
        ms for ms in (_ms(spans.get((tc.ref.trace_id, tc.ref.span_id))) for tc in tools if tc.ref) if ms is not None
    ]

    with_usage = [c.usage for c in calls if c.usage is not None]
    per_call_totals = [u.input_tokens + u.output_tokens for u in with_usage]
    models = sorted({c.request_model or c.response_model for c in calls if c.request_model or c.response_model})

    if not turn_latencies:
        turn_latencies = [t.duration_unix_nano / 1e6 for trace in traces for t in trace.root_spans()]

    return {
        "latency": {
            "overall": _calc_summary_stats(turn_latencies),
            "llm_calls": _calc_summary_stats(call_latencies),
            "tool_executions": _calc_summary_stats(tool_latencies),
        },
        "tokens": {
            "total_prompt": sum(u.input_tokens for u in with_usage),
            "total_output": sum(u.output_tokens for u in with_usage),
            "total": sum(per_call_totals),
            "per_llm_call": _calc_summary_stats([float(x) for x in per_call_totals]),
            "cache_creation_tokens": sum(u.cache_write_input_tokens for u in with_usage),
            "cache_read_tokens": sum(u.cache_read_input_tokens for u in with_usage),
        },
        "counts": {
            "llm_calls": len(calls),
            "tool_calls": len(tools),
            "invocations": len(conversation.turns),
        },
        "models": models,
        "tool_names": sorted({tc.name for tc in tools}),
    }


def extract_trace_metadata(traces: Sequence[Trace], conversation: Conversation) -> dict[str, Any]:
    """Agent, model, start time (microseconds) and text previews of the first turn."""
    metadata: dict[str, Any] = {
        "agent_name": None,
        "agent_id": None,
        "service_name": first_service_name(traces),
        "model": None,
        "response_model": None,
        "provider": None,
        "schema_version": None,
        "start_time": None,
        "user_input_preview": None,
        "final_output_preview": None,
    }
    if not conversation.turns:
        return metadata
    first = conversation.turns[0]
    spans = _span_index(traces)
    anchor = spans.get((first.ref.trace_id, first.ref.span_id))
    metadata["agent_name"] = first.agent_name
    metadata["agent_id"] = attr_str(anchor.attributes, sc.AGENT_ID) if anchor else None
    metadata["start_time"] = first.start_ns // 1000
    call = first.llm_calls[0] if first.llm_calls else None
    if call is not None:
        metadata["model"] = call.request_model or call.response_model
        metadata["response_model"] = call.response_model
        metadata["provider"] = call.provider
        leaf = spans.get((call.ref.trace_id, call.ref.span_id))
        if leaf is not None:
            metadata["schema_version"] = schema_version(leaf.scope.schema_url or leaf.resource.schema_url)
    user = text_of(first.user_input)
    final = text_of(first.final_output)
    metadata["user_input_preview"] = _truncate(user) if user else None
    metadata["final_output_preview"] = _truncate(final) if final else None
    return metadata
