from __future__ import annotations

import os
import tempfile
from typing import Any

import httpx
from mcp.server import FastMCP
from pydantic import BaseModel, Field

from agentevals.config import EvalRunConfig
from agentevals.runner import run_evaluation

_DEFAULT_SERVER_URL = "http://localhost:8001"


# ---------------------------------------------------------------------------
# MCP tool response models
# ---------------------------------------------------------------------------


class MetricInfoResponse(BaseModel):
    name: str
    category: str
    requires_eval_set: bool
    requires_llm: bool
    requires_gcp: bool
    requires_rubrics: bool
    description: str
    working: bool


class MetricScoreResponse(BaseModel):
    metric: str
    score: float | None = None
    status: str
    error: str | None = None


class TraceEvalResponse(BaseModel):
    trace_id: str
    num_invocations: int
    metrics: list[MetricScoreResponse]
    warnings: list[str] | None = None


class EvaluateTracesResponse(BaseModel):
    passed: bool
    traces: list[TraceEvalResponse]
    errors: list[str] | None = None


class SessionSummaryResponse(BaseModel):
    session_id: str
    is_complete: bool
    span_count: int
    started_at: str


class ToolCallResponse(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class InvocationSummaryResponse(BaseModel):
    user: str
    response: str
    tool_calls: list[ToolCallResponse]


class SummarizeSessionResponse(BaseModel):
    session_id: str
    num_spans: int
    num_invocations: int = 0
    invocations: list[InvocationSummaryResponse]


class SessionEvalResultResponse(BaseModel):
    session_id: str
    trace_id: str | None = None
    num_invocations: int | None = None
    metric_results: list[dict[str, Any]] | None = None
    error: str | None = None


class EvaluateSessionsResponse(BaseModel):
    golden_session_id: str
    eval_set_id: str
    results: list[SessionEvalResultResponse]


# ---------------------------------------------------------------------------
# Result transformation
# ---------------------------------------------------------------------------


def summarize_run_result(result) -> EvaluateTracesResponse:
    """Transform a RunResult into the MCP tool response shape."""
    traces = []
    for tr in result.trace_results:
        metrics = [
            MetricScoreResponse(
                metric=mr.metric_name,
                score=mr.score,
                status=mr.eval_status,
                error=mr.error if mr.error else None,
            )
            for mr in tr.metric_results
        ]
        traces.append(
            TraceEvalResponse(
                trace_id=tr.trace_id,
                num_invocations=tr.num_invocations,
                metrics=metrics,
                warnings=tr.conversion_warnings if tr.conversion_warnings else None,
            )
        )
    return EvaluateTracesResponse(
        passed=all(m.status != "FAILED" for t in traces for m in t.metrics),
        traces=traces,
        errors=result.errors if result.errors else None,
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server(server_url: str | None = None, **fastmcp_kwargs: Any) -> FastMCP:
    """Build the FastMCP server. Extra keyword arguments are passed to :class:`FastMCP` (e.g. ``host``, ``port``)."""
    mcp = FastMCP("agentevals", **fastmcp_kwargs)
    _url = (server_url or os.environ.get("AGENTEVALS_SERVER_URL", _DEFAULT_SERVER_URL)).rstrip("/")

    def _unwrap(response_json: dict) -> Any:
        if response_json.get("error"):
            raise RuntimeError(f"API error: {response_json['error']}")
        return response_json["data"]

    async def _get(path: str) -> Any:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{_url}{path}")
                r.raise_for_status()
                return _unwrap(r.json())
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach agentevals server at {_url}. Start it with: uv run agentevals serve --dev"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Server error {exc.response.status_code}: {exc.response.text}") from exc

    async def _post(path: str, body: dict) -> Any:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f"{_url}{path}", json=body)
                r.raise_for_status()
                return _unwrap(r.json())
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot reach agentevals server at {_url}. Start it with: uv run agentevals serve --dev"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Server error {exc.response.status_code}: {exc.response.text}") from exc

    @mcp.tool()
    async def list_metrics() -> list[MetricInfoResponse]:
        """List all available evaluation metrics with their descriptions and requirements.

        Call this first to discover which metrics you can pass to evaluate_traces
        or evaluate_sessions. Each metric has requirement flags that indicate what
        it needs to produce results (an eval set for comparison, an LLM judge, GCP
        credentials, or rubric configuration).

        Returns:
            A list of metric objects, each containing:
            - name: metric identifier to pass to evaluation tools
            - category: grouping such as "trajectory", "response", "safety", "quality"
            - requires_eval_set: whether a golden eval set file is needed
            - requires_llm: whether an LLM judge model is needed
            - requires_gcp: whether GCP/Vertex AI credentials are needed
            - requires_rubrics: whether rubric configuration is needed
            - description: what the metric measures
            - working: whether the metric is currently functional

        Common metrics:
            "tool_trajectory_avg_score": compares actual tool call sequences
                against expected trajectory from an eval set.
            "response_match_score": ROUGE-1 text similarity against expected
                response (requires eval set).
            "hallucinations_v1": detects hallucinated information (requires
                LLM judge).
            "final_response_match_v2": LLM-based response comparison (requires
                eval set and LLM judge).
            "safety_v1": safety assessment via Vertex AI (requires GCP).
        """
        data = await _get("/api/metrics")
        return [
            MetricInfoResponse(
                name=m["name"],
                category=m["category"],
                requires_eval_set=m["requiresEvalSet"],
                requires_llm=m["requiresLLM"],
                requires_gcp=m["requiresGCP"],
                requires_rubrics=m["requiresRubrics"],
                description=m["description"],
                working=m["working"],
            )
            for m in data
        ]

    @mcp.tool()
    async def evaluate_traces(
        trace_files: list[str],
        metrics: list[str] | None = None,
        trace_format: str | None = None,
        eval_set_file: str | None = None,
        judge_model: str | None = None,
        threshold: float | None = None,
        eval_config_file: str | None = None,
    ) -> EvaluateTracesResponse:
        """Evaluate one or more local agent trace files against selected metrics.

        This is the primary offline evaluation tool. It loads trace files from disk,
        converts them to the internal invocation format, and runs each requested
        metric. Does not require the agentevals server to be running.

        Typical workflow:
            1. Call list_metrics to discover available metrics and their requirements.
            2. Call evaluate_traces with trace file paths and chosen metrics.
            3. Check the top-level "passed" field for a quick pass/fail summary.
            4. Inspect per-trace metric scores and errors for details.

        Args:
            trace_files: Absolute paths to trace files on disk. Supports Jaeger
                JSON and OTLP JSON/JSONL (including Tempo exports). Each file
                may contain one or more traces.
            metrics: Metric names to evaluate (from list_metrics). Defaults to
                ["tool_trajectory_avg_score"] if not specified.
            trace_format: Optional explicit format override ("jaeger-json" or
                "otlp-json"). Auto-detected from file contents when omitted.
            eval_set_file: Absolute path to a golden eval set JSON file (ADK
                EvalSet format). Required by comparison metrics such as
                "tool_trajectory_avg_score" and "response_match_score".
            judge_model: LLM model name for judge-based metrics, for example
                "gemini-2.5-flash" or "gemini-2.0-flash". Required by metrics
                like "hallucinations_v1" and "final_response_match_v2".
            threshold: Score threshold for PASS/FAIL classification, between
                0.0 and 1.0. Metric scores below this value are marked FAILED.
            eval_config_file: Absolute path to an eval config YAML file that
                defines custom evaluators. When provided, its settings are
                merged with the other arguments (explicit arguments take
                precedence over the config file).

        Returns:
            An EvaluateTracesResponse with:
            - passed: true if no metric across any trace has status "FAILED"
            - traces: list of per-trace results, each containing:
                - trace_id: identifier of the evaluated trace
                - num_invocations: number of agent invocations in the trace
                - metrics: list of metric results, each with:
                    - metric: the metric name
                    - score: numeric score (0.0 to 1.0), or null if not scored
                    - status: "PASSED", "FAILED", or "NOT_EVALUATED"
                    - error: error message if the metric failed to run
                - warnings: conversion warnings, if any
            - errors: top-level errors (e.g. trace files that failed to load)
        """
        if metrics is None:
            metrics = ["tool_trajectory_avg_score"]
        if eval_config_file:
            from agentevals.eval_config_loader import load_eval_config, merge_configs

            file_config = load_eval_config(eval_config_file)
            cli_config = EvalRunConfig(
                trace_files=trace_files,
                metrics=metrics,
                trace_format=trace_format,
                eval_set_file=eval_set_file,
                judge_model=judge_model,
                threshold=threshold,
            )
            config = merge_configs(file_config, cli_config)
        else:
            config = EvalRunConfig(
                trace_files=trace_files,
                metrics=metrics,
                trace_format=trace_format,
                eval_set_file=eval_set_file,
                judge_model=judge_model,
                threshold=threshold,
            )
        result = await run_evaluation(config)
        return summarize_run_result(result)

    @mcp.tool()
    async def list_sessions(limit: int = 20) -> list[SessionSummaryResponse]:
        """List recent streaming trace sessions, ordered most recent first.

        Use this to discover session IDs for summarize_session or
        evaluate_sessions. Sessions are created when agents stream traces to the
        agentevals server via the SDK or an OpenTelemetry exporter.

        Requires the agentevals server to be running (start with:
        uv run agentevals serve --dev).

        Args:
            limit: Maximum number of sessions to return. Defaults to 20.

        Returns:
            A list of SessionSummaryResponse objects, each containing:
            - session_id: unique identifier to pass to other session tools
            - is_complete: whether the session has finished receiving spans
            - span_count: number of OpenTelemetry spans recorded
            - started_at: ISO 8601 timestamp of when the session began
        """
        sessions = await _get("/api/streaming/sessions")
        sessions.sort(key=lambda s: s.get("startedAt", ""), reverse=True)
        return [
            SessionSummaryResponse(
                session_id=s["sessionId"],
                is_complete=s["isComplete"],
                span_count=s["spanCount"],
                started_at=s["startedAt"],
            )
            for s in sessions[:limit]
        ]

    @mcp.tool()
    async def summarize_session(session_id: str) -> SummarizeSessionResponse:
        """Get a structured summary of a session showing its invocations, tool
        calls, and messages in human-readable form.

        Use this to understand what an agent did during a session before running
        evaluation. Parses the raw OpenTelemetry trace and extracts the
        conversation flow: what the user said, what tools the agent called, and
        how it responded.

        Requires the agentevals server to be running.

        Args:
            session_id: Session ID obtained from list_sessions.

        Returns:
            A SummarizeSessionResponse with:
            - session_id: the requested session ID
            - num_spans: total OpenTelemetry spans in the session
            - num_invocations: number of agent invocations extracted
            - invocations: chronological list of invocations, each containing:
                - user: the user's input text
                - response: the agent's final response text
                - tool_calls: tools the agent called, each with:
                    - tool: the tool/function name
                    - args: arguments passed to the tool
        """
        from agentevals.converter import convert_traces
        from agentevals.loader.otlp import OtlpJsonLoader

        raw = await _post("/api/streaming/get-trace", {"session_id": session_id})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(raw["traceContent"])
            tmp_path = f.name

        traces = OtlpJsonLoader().load(tmp_path)
        if not traces:
            return SummarizeSessionResponse(
                session_id=session_id,
                num_spans=raw["numSpans"],
                invocations=[],
            )

        invocations = []
        for conv in convert_traces(traces):
            for inv in conv.invocations:
                tool_calls = []
                if inv.intermediate_data:
                    tool_calls = [
                        ToolCallResponse(tool=tu.name, args=getattr(tu, "args", {}))
                        for tu in inv.intermediate_data.tool_uses
                    ]
                invocations.append(
                    InvocationSummaryResponse(
                        user=next((p.text for p in inv.user_content.parts if p.text), "") if inv.user_content else "",
                        response=next((p.text for p in inv.final_response.parts if p.text), "")
                        if inv.final_response
                        else "",
                        tool_calls=tool_calls,
                    )
                )

        return SummarizeSessionResponse(
            session_id=session_id,
            num_spans=raw["numSpans"],
            num_invocations=len(invocations),
            invocations=invocations,
        )

    @mcp.tool()
    async def evaluate_sessions(
        golden_session_id: str,
        metrics: list[str] | None = None,
        judge_model: str = "gemini-2.5-flash",
        eval_set_id: str | None = None,
    ) -> EvaluateSessionsResponse:
        """Evaluate all completed sessions against a golden reference session.

        This is the primary tool for regression testing streamed agent sessions.
        The server automatically builds an eval set from the golden session's
        trace, then evaluates every other completed session against it. No file
        creation or pre-existing eval set is needed.

        Typical workflow:
            1. Call list_sessions to find session IDs.
            2. Call summarize_session on a candidate to verify it represents the
               expected "golden" behavior.
            3. Call evaluate_sessions with that session as the golden reference.
            4. Inspect per-session results to find regressions.

        Requires the agentevals server to be running.

        Args:
            golden_session_id: Session ID of the reference run. All other
                completed sessions will be compared against this one.
            metrics: Metric names to evaluate (from list_metrics). Defaults to
                ["tool_trajectory_avg_score"]. Only metrics that support eval
                set comparison are meaningful here.
            judge_model: LLM model for judge-based metrics. Defaults to
                "gemini-2.5-flash".
            eval_set_id: A label for the eval set built from the golden session.
                Any string is accepted. If omitted, a default is generated from
                the golden session ID.

        Returns:
            An EvaluateSessionsResponse with:
            - golden_session_id: the reference session used
            - eval_set_id: the label assigned to the generated eval set
            - results: list of per-session evaluation results, each containing:
                - session_id: the evaluated session
                - trace_id: trace identifier, if available
                - num_invocations: number of invocations evaluated, if available
                - metric_results: list of metric score dicts (with metricName,
                  score, evalStatus, perInvocationScores, error, details fields)
                - error: error message if evaluation of this session failed
        """
        if metrics is None:
            metrics = ["tool_trajectory_avg_score"]
        data = await _post(
            "/api/streaming/evaluate-sessions",
            {
                "golden_session_id": golden_session_id,
                "eval_set_id": eval_set_id or f"eval-{golden_session_id[:12]}",
                "metrics": metrics,
                "judge_model": judge_model,
            },
        )
        return EvaluateSessionsResponse(
            golden_session_id=data["goldenSessionId"],
            eval_set_id=data["evalSetId"],
            results=[
                SessionEvalResultResponse(
                    session_id=r["sessionId"],
                    trace_id=r.get("traceId"),
                    num_invocations=r.get("numInvocations"),
                    metric_results=r.get("metricResults"),
                    error=r.get("error"),
                )
                for r in data["results"]
            ],
        )

    return mcp
