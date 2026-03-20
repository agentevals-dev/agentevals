from __future__ import annotations

import os
import tempfile
from typing import Any

import httpx
from mcp.server import FastMCP

from agentevals.config import EvalRunConfig
from agentevals.runner import run_evaluation

_DEFAULT_SERVER_URL = "http://localhost:8001"


def create_server(server_url: str | None = None) -> FastMCP:
    mcp = FastMCP("agentevals")
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

    def _summarize_run_result(result) -> dict[str, Any]:
        traces = []
        for tr in result.trace_results:
            traces.append(
                {
                    "trace_id": tr.trace_id,
                    "num_invocations": tr.num_invocations,
                    "metrics": [
                        {
                            "metric": mr.metric_name,
                            "score": mr.score,
                            "status": mr.eval_status,
                            **({"error": mr.error} if mr.error else {}),
                        }
                        for mr in tr.metric_results
                    ],
                    **({"warnings": tr.conversion_warnings} if tr.conversion_warnings else {}),
                }
            )
        return {
            "passed": all(mr["status"] != "FAILED" for tr in traces for mr in tr["metrics"]),
            "traces": traces,
            **({"errors": result.errors} if result.errors else {}),
        }

    @mcp.tool()
    async def list_metrics() -> list[dict[str, Any]]:
        """List all available evaluation metrics with their descriptions and requirements."""
        return await _get("/api/metrics")

    @mcp.tool()
    async def evaluate_traces(
        trace_files: list[str],
        metrics: list[str] | None = None,
        trace_format: str = "jaeger-json",
        eval_set_file: str | None = None,
        judge_model: str | None = None,
        threshold: float | None = None,
        eval_config_file: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate one or more local agent trace files.

        Does not require the agentevals server to be running. Returns a flat summary
        with a top-level 'passed' boolean and per-trace metric scores.

        Args:
            trace_files: Absolute paths to Jaeger JSON or OTLP JSON/JSONL trace files.
            metrics: Metric names to evaluate. Use list_metrics to see available options.
            trace_format: "jaeger-json" or "otlp-json".
            eval_set_file: Path to a golden eval set JSON for comparison metrics.
            judge_model: LLM model for judge-based metrics (e.g. "gemini-2.5-flash").
            threshold: Score threshold for PASS/FAIL classification (0.0–1.0).
            eval_config_file: Path to an eval config YAML file with custom evaluators.
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
        return _summarize_run_result(result)

    @mcp.tool()
    async def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
        """List streaming trace sessions, most recent first.

        Requires agentevals serve to be running.

        Args:
            limit: Maximum number of sessions to return (default: 20).
        """
        sessions = await _get("/api/streaming/sessions")
        sessions.sort(key=lambda s: s.get("startedAt", ""), reverse=True)
        return [
            {
                "sessionId": s["sessionId"],
                "isComplete": s["isComplete"],
                "spanCount": s["spanCount"],
                "startedAt": s["startedAt"],
            }
            for s in sessions[:limit]
        ]

    @mcp.tool()
    async def summarize_session(session_id: str) -> dict[str, Any]:
        """Get a structured summary of a session's invocations, tool calls, and messages.

        Parses the raw trace and returns human-readable invocation data: user messages,
        agent responses, and tool calls made. For the full span data, use get_session_trace.

        Args:
            session_id: Session ID from list_sessions.
        """
        from agentevals.converter import convert_traces
        from agentevals.loader.otlp import OtlpJsonLoader

        raw = await _post("/api/streaming/get-trace", {"session_id": session_id})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(raw["traceContent"])
            tmp_path = f.name

        traces = OtlpJsonLoader().load(tmp_path)
        if not traces:
            return {"session_id": session_id, "num_spans": raw["numSpans"], "invocations": []}

        invocations = []
        for conv in convert_traces(traces):
            for inv in conv.invocations:
                tool_calls = []
                if inv.intermediate_data:
                    tool_calls = [
                        {"tool": tu.name, "args": getattr(tu, "args", {})} for tu in inv.intermediate_data.tool_uses
                    ]
                invocations.append(
                    {
                        "user": next((p.text for p in inv.user_content.parts if p.text), "")
                        if inv.user_content
                        else "",
                        "response": next((p.text for p in inv.final_response.parts if p.text), "")
                        if inv.final_response
                        else "",
                        "tool_calls": tool_calls,
                    }
                )

        return {
            "session_id": session_id,
            "num_spans": raw["numSpans"],
            "num_invocations": len(invocations),
            "invocations": invocations,
        }

    @mcp.tool()
    async def evaluate_sessions(
        golden_session_id: str,
        metrics: list[str] | None = None,
        judge_model: str = "gemini-2.5-flash",
        eval_set_id: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate all completed sessions against a golden reference session.

        The server builds the eval set from the golden session automatically — no file
        creation or pre-existing eval set needed. Call list_sessions first to find session IDs.

        Requires agentevals serve to be running.

        Args:
            golden_session_id: Session ID of the reference/golden run.
            metrics: Metric names to evaluate. Use list_metrics to see available options.
            judge_model: LLM model for judge-based metrics.
            eval_set_id: A label for the eval set built from the golden session. You can use
                         any string or omit it — a default will be generated automatically.
        """
        if metrics is None:
            metrics = ["tool_trajectory_avg_score"]
        return await _post(
            "/api/streaming/evaluate-sessions",
            {
                "golden_session_id": golden_session_id,
                "eval_set_id": eval_set_id or f"eval-{golden_session_id[:12]}",
                "metrics": metrics,
                "judge_model": judge_model,
            },
        )

    return mcp
