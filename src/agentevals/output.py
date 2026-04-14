"""Output formatting for evaluation results."""

from __future__ import annotations

import json
from typing import Any

from .runner import MetricResult, RunResult


def _format_duration(ms: float | None) -> str:
    if ms is None:
        return ""
    ms = round(ms)
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    total_secs = round(seconds)
    minutes, secs = divmod(total_secs, 60)
    return f"{minutes}m {secs}s"


def format_results(run_result: RunResult, fmt: str = "table") -> str:
    if fmt == "json":
        return _format_json(run_result)
    elif fmt == "summary":
        return _format_summary(run_result)
    else:
        return _format_table(run_result)


def _format_table(run_result: RunResult) -> str:
    try:
        from tabulate import tabulate
    except ImportError:
        return _format_summary(run_result)

    lines: list[str] = []

    if run_result.errors:
        lines.append("Errors:")
        for err in run_result.errors:
            lines.append(f"  - {err}")
        lines.append("")

    for trace_result in run_result.trace_results:
        lines.append(f"Trace: {trace_result.trace_id}")
        lines.append(f"Invocations: {trace_result.num_invocations}")

        if trace_result.conversion_warnings:
            for w in trace_result.conversion_warnings:
                lines.append(f"  Warning: {w}")

        rows = []
        for mr in trace_result.metric_results:
            status_icon = _status_icon(mr.eval_status)
            score_str = f"{mr.score:.4f}" if mr.score is not None else "N/A"
            error_str = mr.error or ""
            per_inv = (
                ", ".join(f"{s:.4f}" if s is not None else "N/A" for s in mr.per_invocation_scores)
                if mr.per_invocation_scores
                else ""
            )
            rows.append(
                [
                    status_icon,
                    mr.metric_name,
                    score_str,
                    mr.eval_status,
                    per_inv,
                    _format_duration(mr.duration_ms),
                    error_str,
                ]
            )

        if rows:
            table = tabulate(
                rows,
                headers=["", "Metric", "Score", "Status", "Per-Invocation", "Time", "Error"],
                tablefmt="simple",
            )
            lines.append(table)

        for mr in trace_result.metric_results:
            if mr.details and mr.eval_status == "FAILED":
                lines.append(_format_metric_details(mr))
                lines.append("")

        if trace_result.performance_metrics:
            perf = trace_result.performance_metrics
            lines.append("\n  Performance Metrics:")

            models = perf.get("models", [])
            if models:
                lines.append(f"    Model: {', '.join(models)}")

            counts = perf.get("counts", {})
            count_parts = []
            if counts.get("llm_calls"):
                count_parts.append(f"{counts['llm_calls']} LLM calls")
            if counts.get("tool_calls"):
                count_parts.append(f"{counts['tool_calls']} tool calls")
            if counts.get("invocations"):
                count_parts.append(f"{counts['invocations']} invocations")
            if count_parts:
                lines.append(f"    Counts: {', '.join(count_parts)}")

            lat = perf["latency"]
            if lat["overall"].get("count", 0) > 0:
                lines.append(
                    f"    Overall Latency: min={_format_duration(lat['overall']['min'])}"
                    f", median={_format_duration(lat['overall']['median'])}"
                    f", max={_format_duration(lat['overall']['max'])}"
                )
            if lat["llm_calls"].get("count", 0) > 0:
                lines.append(
                    f"    LLM Latency:     min={_format_duration(lat['llm_calls']['min'])}"
                    f", median={_format_duration(lat['llm_calls']['median'])}"
                    f", max={_format_duration(lat['llm_calls']['max'])}"
                )
            if lat["tool_executions"].get("count", 0) > 0:
                lines.append(
                    f"    Tool Latency:    min={_format_duration(lat['tool_executions']['min'])}"
                    f", median={_format_duration(lat['tool_executions']['median'])}"
                    f", max={_format_duration(lat['tool_executions']['max'])}"
                )

            tok = perf["tokens"]
            token_line = (
                f"    Tokens: {tok['total']} total ({tok['total_prompt']} prompt + {tok['total_output']} output)"
            )
            cache_parts = []
            if tok.get("cache_read_tokens"):
                cache_parts.append(f"{tok['cache_read_tokens']} cache read")
            if tok.get("cache_creation_tokens"):
                cache_parts.append(f"{tok['cache_creation_tokens']} cache write")
            if cache_parts:
                token_line += f" [{', '.join(cache_parts)}]"
            lines.append(token_line)

        lines.append("")

    if run_result.performance_metrics:
        lines.append("Overall Performance:")
        perf = run_result.performance_metrics

        counts = perf.get("counts", {})
        if counts:
            lines.append(
                f"  Traces: {counts.get('traces', 0)}"
                f", LLM Calls: {counts.get('total_llm_calls', 0)}"
                f", Tool Calls: {counts.get('total_tool_calls', 0)}"
            )

        models = perf.get("models", [])
        if models:
            lines.append(f"  Models: {', '.join(models)}")

        lat = perf.get("latency", {})
        overall_lat = lat.get("overall_per_trace", {})
        if overall_lat and overall_lat.get("p50", 0) > 0:
            lines.append(
                f"  Latency per Trace: p50={_format_duration(overall_lat['p50'])}"
                f", p95={_format_duration(overall_lat['p95'])}"
                f", p99={_format_duration(overall_lat['p99'])}"
            )

        tok = perf["tokens"]
        lines.append(f"  Total Tokens: {tok['total']} ({tok['total_prompt']} prompt + {tok['total_output']} output)")
        avg = tok.get("avg_per_trace", {})
        if avg:
            lines.append(f"  Avg per Trace: {avg['prompt']:.0f} prompt, {avg['output']:.0f} output")
        cache_parts = []
        if tok.get("cache_read_tokens"):
            cache_parts.append(f"{tok['cache_read_tokens']} cache read")
        if tok.get("cache_creation_tokens"):
            cache_parts.append(f"{tok['cache_creation_tokens']} cache write")
        if cache_parts:
            lines.append(f"  Cache Tokens: {', '.join(cache_parts)}")

        lines.append("")

    return "\n".join(lines)


def _format_json(run_result: RunResult) -> str:
    data: dict[str, Any] = {
        "traces": [],
        "errors": run_result.errors,
    }

    for tr in run_result.trace_results:
        trace_data: dict[str, Any] = {
            "trace_id": tr.trace_id,
            "num_invocations": tr.num_invocations,
            "conversion_warnings": tr.conversion_warnings,
            "metrics": [],
        }
        for mr in tr.metric_results:
            metric_data = {
                "metric_name": mr.metric_name,
                "score": mr.score,
                "eval_status": mr.eval_status,
                "per_invocation_scores": mr.per_invocation_scores,
                "duration_ms": mr.duration_ms,
                "error": mr.error,
            }
            if mr.details:
                metric_data["details"] = mr.details
            trace_data["metrics"].append(metric_data)
        if tr.performance_metrics:
            trace_data["performance_metrics"] = tr.performance_metrics
        data["traces"].append(trace_data)

    if run_result.performance_metrics:
        data["performance_metrics"] = run_result.performance_metrics

    return json.dumps(data, indent=2)


def _format_summary(run_result: RunResult) -> str:
    lines: list[str] = []

    if run_result.errors:
        lines.append("Errors:")
        for err in run_result.errors:
            lines.append(f"  - {err}")
        lines.append("")

    for tr in run_result.trace_results:
        lines.append(f"Trace {tr.trace_id} ({tr.num_invocations} invocations):")
        for mr in tr.metric_results:
            icon = _status_icon(mr.eval_status)
            duration_suffix = f" [{_format_duration(mr.duration_ms)}]" if mr.duration_ms is not None else ""
            if mr.error:
                lines.append(f"  {icon} {mr.metric_name}: ERROR - {mr.error}{duration_suffix}")
            elif mr.score is not None:
                lines.append(f"  {icon} {mr.metric_name}: {mr.score:.4f} ({mr.eval_status}){duration_suffix}")
            else:
                lines.append(f"  {icon} {mr.metric_name}: N/A ({mr.eval_status}){duration_suffix}")
        lines.append("")

    return "\n".join(lines)


def _format_metric_details(mr: MetricResult) -> str:
    """Format detailed comparison for metrics with details field."""
    lines = []

    if mr.metric_name == "tool_trajectory_avg_score" and mr.details:
        comparisons = mr.details.get("comparisons", [])
        for i, comp in enumerate(comparisons, 1):
            if not comp.get("matched", True):
                lines.append(f"  Invocation {i} trajectory mismatch:")
                lines.append("    Expected:")
                for tool in comp.get("expected", []):
                    args_str = json.dumps(tool["args"]) if tool["args"] else "{}"
                    lines.append(f"      - {tool['name']}({args_str})")
                if not comp.get("expected"):
                    lines.append("      (none)")

                lines.append("    Actual:")
                for tool in comp.get("actual", []):
                    args_str = json.dumps(tool["args"]) if tool["args"] else "{}"
                    lines.append(f"      - {tool['name']}({args_str})")
                if not comp.get("actual"):
                    lines.append("      (none)")

    return "\n".join(lines)


def _status_icon(status: str) -> str:
    return {
        "PASSED": "[PASS]",
        "FAILED": "[FAIL]",
        "NOT_EVALUATED": "[----]",
    }.get(status, "[????]")
