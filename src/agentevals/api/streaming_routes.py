"""API routes for streaming session management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..adk_bridge import eval_set_from_turns, load_eval_set_from_dict
from ..config import BuiltinMetricDef, EvalParams, EvaluatorDef
from ..genai.extract import extract_conversation
from ..otel.encode import encode_traces
from ..runner import RunResult, run_evaluation_from_traces
from ..streaming.exports import EXPORT_DIR, export_name
from ..trace_attrs import OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_REQUEST_MODEL
from .dependencies import require_trace_manager
from .models import (
    CreateEvalSetData,
    EvaluateSessionsData,
    GetTraceData,
    PrepareEvaluationData,
    SessionEvalResult,
    SessionInfo,
    StandardResponse,
)

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

streaming_router = APIRouter()


class CreateEvalSetRequest(BaseModel):
    session_id: str
    eval_set_id: str


class EvaluateSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    golden_session_id: str
    eval_set_id: str
    evaluators: list[EvaluatorDef] = Field(default_factory=lambda: [BuiltinMetricDef(name="tool_trajectory_avg_score")])


class PrepareEvaluationRequest(BaseModel):
    golden_session_id: str
    session_ids: list[str]


class GetTraceRequest(BaseModel):
    session_id: str


async def _do_create_eval_set(
    request: CreateEvalSetRequest, manager: StreamingTraceManager
) -> StandardResponse[CreateEvalSetData]:
    """Shared logic for creating an EvalSet from a session's trace."""
    session = manager.sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        traces = manager.session_traces(session)
        if not traces:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No traces found in session (spans={len(session.spans)}, "
                    f"logs={len(session.logs)}). If using the SDK with langchain/openai, "
                    f"ensure opentelemetry-instrumentation-openai-v2 is installed."
                ),
            )
        conversation = extract_conversation(traces, request.session_id)
        if not conversation.turns:
            raise HTTPException(status_code=400, detail="No turns found in session")
        return StandardResponse(
            data=CreateEvalSetData(
                eval_set=eval_set_from_turns(request.eval_set_id, conversation.turns),
                num_invocations=len(conversation.turns),
            )
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to create eval set")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@streaming_router.get("/sessions", response_model=StandardResponse[list[SessionInfo]])
async def list_sessions(manager: StreamingTraceManager = Depends(require_trace_manager)):
    sessions_data = []

    for session_id, session in manager.sessions.items():
        info = SessionInfo(
            session_id=session_id,
            trace_id=session.trace_id,
            eval_set_id=session.eval_set_id,
            span_count=len(session.spans),
            is_complete=session.is_complete,
            started_at=session.started_at.isoformat(),
            metadata=session.metadata,
            invocations=session.invocations if session.is_complete and session.invocations else None,
        )
        sessions_data.append(info)

    return StandardResponse(data=sessions_data)


@streaming_router.post("/create-eval-set", response_model=StandardResponse[CreateEvalSetData])
async def create_eval_set_from_session(
    request: CreateEvalSetRequest,
    manager: StreamingTraceManager = Depends(require_trace_manager),
):
    """Convert a session's trace into an EvalSet."""
    return await _do_create_eval_set(request, manager)


async def _persist_sessions_run(
    http_request: Request,
    *,
    evaluators: list[EvaluatorDef],
    eval_set_dict: dict,
    session_ids: list[str],
    run_results: list[RunResult],
    session_errors: list[str],
) -> None:
    """Best-effort: persist a UI-driven session evaluation as a single Run row
    (plus Result rows) when ``app.state.run_service`` is configured (postgres
    backend), mirroring the offline ``/api/evaluate`` persistence.

    Every evaluated session's traces are aggregated into one run so the run
    history counts one evaluation per "Evaluate" click, the same way one
    offline upload of N traces produces one run. No-op (and never raises) on
    the memory backend or on persistence failure, so the live eval results are
    always returned to the caller regardless."""
    service = getattr(http_request.app.state, "run_service", None)
    if service is None:
        return
    trace_results = [tr for rr in run_results for tr in rr.trace_results]
    if not trace_results:
        return
    combined = RunResult(trace_results=trace_results, errors=session_errors)
    try:
        await service.record_eval_run(
            params=EvalParams(evaluators=evaluators),
            eval_set_dict=eval_set_dict,
            trace_format="otlp-json",
            upload_filenames=session_ids,
            run_result=combined,
        )
    except Exception:
        logger.exception("failed to persist evaluate-sessions run; results still returned to caller")


@streaming_router.post("/evaluate-sessions", response_model=StandardResponse[EvaluateSessionsData])
async def evaluate_sessions(
    request: EvaluateSessionsRequest,
    http_request: Request,
    manager: StreamingTraceManager = Depends(require_trace_manager),
):
    """Evaluate all sessions against a golden session converted to EvalSet."""
    golden_session = manager.sessions.get(request.golden_session_id)
    if not golden_session:
        raise HTTPException(status_code=404, detail="Golden session not found")

    try:
        eval_set_response = await _do_create_eval_set(
            CreateEvalSetRequest(
                session_id=request.golden_session_id,
                eval_set_id=request.eval_set_id,
            ),
            manager,
        )
        eval_set = load_eval_set_from_dict(eval_set_response.data.eval_set)
        params = EvalParams(evaluators=request.evaluators)

        sessions_to_evaluate = [
            (session_id, session) for session_id, session in manager.sessions.items() if session.is_complete
        ]

        logger.info("Evaluating %d complete sessions (of %d total)", len(sessions_to_evaluate), len(manager.sessions))

        sem = asyncio.Semaphore(5)

        async def eval_one_session(session_id: str, session) -> tuple[SessionEvalResult, RunResult | None]:
            async with sem:
                try:
                    traces = manager.session_traces(session)
                    eval_result = await run_evaluation_from_traces(traces, params, eval_set, group_key=session_id)

                    if eval_result.trace_results:
                        trace_result = eval_result.trace_results[0]
                        return (
                            SessionEvalResult(
                                session_id=session_id,
                                trace_id=trace_result.trace_id,
                                num_invocations=trace_result.num_invocations,
                                metric_results=[
                                    {
                                        "metricName": mr.metric_name,
                                        "score": mr.score,
                                        "evalStatus": mr.eval_status,
                                        "error": mr.error,
                                        "perInvocationScores": mr.per_invocation_scores,
                                        "details": mr.details,
                                    }
                                    for mr in trace_result.metric_results
                                ],
                            ),
                            eval_result,
                        )
                    logger.warning("No trace results for session %s", session_id)
                    return SessionEvalResult(session_id=session_id, error="No trace results"), None

                except Exception as exc:
                    logger.error(f"Failed to evaluate session {session_id}: {exc}", exc_info=True)
                    return SessionEvalResult(session_id=session_id, error=str(exc)), None

        evaluated = await asyncio.gather(*[eval_one_session(sid, sess) for sid, sess in sessions_to_evaluate])
        results = [session_result for session_result, _ in evaluated]

        logger.info("Evaluation complete. Total results: %d", len(results))

        await _persist_sessions_run(
            http_request,
            evaluators=request.evaluators,
            eval_set_dict=eval_set_response.data.eval_set,
            session_ids=[sid for sid, _ in sessions_to_evaluate],
            run_results=[run_result for _, run_result in evaluated if run_result is not None],
            session_errors=[f"{r.session_id}: {r.error}" for r in results if r.error],
        )

        return StandardResponse(
            data=EvaluateSessionsData(
                golden_session_id=request.golden_session_id,
                eval_set_id=request.eval_set_id,
                results=results,
            )
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to evaluate sessions")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@streaming_router.post("/prepare-evaluation", response_model=StandardResponse[PrepareEvaluationData])
async def prepare_evaluation(
    request: PrepareEvaluationRequest,
    manager: StreamingTraceManager = Depends(require_trace_manager),
):
    """Prepare evaluation by saving traces and eval set as downloadable files."""
    golden_session = manager.sessions.get(request.golden_session_id)
    if not golden_session:
        raise HTTPException(status_code=404, detail="Golden session not found")

    try:
        eval_set_response = await _do_create_eval_set(
            CreateEvalSetRequest(
                session_id=request.golden_session_id,
                eval_set_id=f"golden_{request.golden_session_id}",
            ),
            manager,
        )

        eval_set_file = EXPORT_DIR / export_name(request.golden_session_id, prefix="eval_set_", suffix=".json")
        with open(eval_set_file, "w", encoding="utf-8") as f:  # noqa: ASYNC230
            json.dump(eval_set_response.data.eval_set, f)

        trace_files = []
        for session_id in request.session_ids:
            session = manager.sessions.get(session_id)
            if not session or not session.is_complete:
                continue

            trace_file = await manager._save_spans_to_temp_file(session)
            trace_files.append(
                {
                    "session_id": session_id,
                    "file_path": str(trace_file),
                }
            )

        return StandardResponse(
            data=PrepareEvaluationData(
                eval_set_url=f"/api/streaming/download/{eval_set_file.name}",
                trace_urls=[f"/api/streaming/download/{os.path.basename(tf['file_path'])}" for tf in trace_files],
                num_traces=len(trace_files),
            )
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to prepare evaluation")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@streaming_router.get("/download/{filename}")
async def download_file(filename: str):
    """Download a prepared trace or eval set file from the export directory.

    Only bare filenames are accepted, and the resolved path must stay inside
    the export directory. Anything that resolves outside it returns the same
    404 as a missing file.
    """
    if filename in {"", ".", ".."} or filename != os.path.basename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    export_root = EXPORT_DIR.resolve()
    candidate = (export_root / filename).resolve()

    if not candidate.is_relative_to(export_root):
        raise HTTPException(status_code=404, detail="File not found")

    if not candidate.is_file():  # noqa: ASYNC240
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(candidate, media_type="application/json", filename=filename)


@streaming_router.get("/sessions/{session_id}/otlp")
async def export_session_otlp(session_id: str, manager: StreamingTraceManager = Depends(require_trace_manager)) -> dict:
    """The session's spans and joined logs as one OTLP/JSON document (``resourceSpans`` and ``resourceLogs``)."""
    session = manager.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return encode_traces(manager.session_traces(session))


@streaming_router.post("/get-trace", response_model=StandardResponse[GetTraceData])
async def get_trace(
    request: GetTraceRequest,
    manager: StreamingTraceManager = Depends(require_trace_manager),
):
    session = manager.sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        has_genai_spans = any(
            span.get("attributes", [])
            and any(
                attr.get("key") in (OTEL_GENAI_REQUEST_MODEL, OTEL_GENAI_INPUT_MESSAGES)
                for attr in span.get("attributes", [])
            )
            for span in session.spans
        )

        if has_genai_spans and not session.logs:
            logger.warning(
                "Session %s has GenAI spans but no logs. "
                "Message content will be missing unless spans already enriched.",
                request.session_id,
            )

        # Reuse the canonical serializer so the trace handed to the UI (and
        # re-uploaded to /api/evaluate) carries service.name, exactly like the
        # evaluate-sessions path. Serializing spans here independently would
        # drop the resource attribute and lose agent identity on the run.
        trace_file = await manager._save_spans_to_temp_file(session)
        with open(trace_file, encoding="utf-8") as f:  # noqa: ASYNC230
            trace_content = f.read()
        num_spans = sum(1 for line in trace_content.splitlines() if line.strip())

        return StandardResponse(
            data=GetTraceData(
                session_id=request.session_id,
                trace_content=trace_content,
                num_spans=num_spans,
            )
        )

    except Exception as exc:
        logger.exception("Failed to get trace")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
