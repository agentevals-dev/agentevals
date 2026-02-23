"""Output formatting for evaluation results."""

from __future__ import annotations

import json
from typing import Any

from .runner import MetricResult, RunResult, TraceResult


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
                ", ".join(
                    f"{s:.4f}" if s is not None else "N/A"
                    for s in mr.per_invocation_scores
                )
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
                    error_str,
                ]
            )

        if rows:
            table = tabulate(
                rows,
                headers=["", "Metric", "Score", "Status", "Per-Invocation", "Error"],
                tablefmt="simple",
            )
            lines.append(table)

        if trace_result.performance_metrics:
            perf = trace_result.performance_metrics
            lines.append("\n  Performance Metrics:")

            lat = perf["latency"]
            lines.append(f"    Overall Latency: p50={lat['overall']['p50']:.0f}ms, p95={lat['overall']['p95']:.0f}ms, p99={lat['overall']['p99']:.0f}ms")
            lines.append(f"    LLM Latency:     p50={lat['llm_calls']['p50']:.0f}ms, p95={lat['llm_calls']['p95']:.0f}ms, p99={lat['llm_calls']['p99']:.0f}ms")
            lines.append(f"    Tool Latency:    p50={lat['tool_executions']['p50']:.0f}ms, p95={lat['tool_executions']['p95']:.0f}ms, p99={lat['tool_executions']['p99']:.0f}ms")

            tok = perf["tokens"]
            lines.append(f"    Tokens: {tok['total']} total ({tok['total_prompt']} prompt + {tok['total_output']} output)")
            lines.append(f"    Per LLM Call:    p50={tok['per_llm_call']['p50']:.0f}, p95={tok['per_llm_call']['p95']:.0f}, p99={tok['per_llm_call']['p99']:.0f}")

        lines.append("")

    if run_result.performance_metrics:
        lines.append("Overall Performance:")
        perf = run_result.performance_metrics
        lines.append(f"  Total Tokens: {perf['tokens']['total']} ({perf['tokens']['total_prompt']} prompt + {perf['tokens']['total_output']} output)")
        lines.append(f"  Avg per Trace: {perf['tokens']['avg_per_trace']['prompt']:.0f} prompt, {perf['tokens']['avg_per_trace']['output']:.0f} output")
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
            trace_data["metrics"].append(
                {
                    "metric_name": mr.metric_name,
                    "score": mr.score,
                    "eval_status": mr.eval_status,
                    "per_invocation_scores": mr.per_invocation_scores,
                    "error": mr.error,
                }
            )
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
            if mr.error:
                lines.append(f"  {icon} {mr.metric_name}: ERROR - {mr.error}")
            elif mr.score is not None:
                lines.append(
                    f"  {icon} {mr.metric_name}: {mr.score:.4f} ({mr.eval_status})"
                )
            else:
                lines.append(f"  {icon} {mr.metric_name}: N/A ({mr.eval_status})")
        lines.append("")

    return "\n".join(lines)


def _status_icon(status: str) -> str:
    return {
        "PASSED": "[PASS]",
        "FAILED": "[FAIL]",
        "NOT_EVALUATED": "[----]",
    }.get(status, "[????]")
