"""API routes for streaming session management."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import EvalRunConfig
from ..converter import convert_traces
from ..loader.otlp import OtlpJsonLoader
from ..runner import run_evaluation
from ..trace_attrs import OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_REQUEST_MODEL
from ..utils.log_enrichment import enrich_spans_with_logs
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
    golden_session_id: str
    eval_set_id: str
    metrics: list[str] = ["tool_trajectory_avg_score"]
    judge_model: str = "gemini-2.5-flash"
    trajectory_match_type: Literal["EXACT", "IN_ORDER", "ANY_ORDER"] | None = None


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
        trace_file = await manager._save_spans_to_temp_file(session)
        logger.debug(
            "Session %s: %d spans, %d logs saved to %s",
            request.session_id,
            len(session.spans),
            len(session.logs),
            trace_file,
        )
        loader = OtlpJsonLoader()
        traces = loader.load(str(trace_file))

        if not traces:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No traces found in session (spans={len(session.spans)}, "
                    f"logs={len(session.logs)}). If using the SDK with langchain/openai, "
                    f"ensure opentelemetry-instrumentation-openai-v2 is installed."
                ),
            )

        conversion_results = convert_traces(traces)
        if not conversion_results:
            raise HTTPException(status_code=400, detail="Failed to convert trace")

        all_invocations = []
        for conv_result in conversion_results:
            all_invocations.extend(conv_result.invocations)

        logger.debug(f"Creating eval set from {len(all_invocations)} invocations")
        for i, inv in enumerate(all_invocations):
            tool_count = len(inv.intermediate_data.tool_uses) if inv.intermediate_data else 0
            logger.debug(f"  Invocation {i}: {tool_count} tool calls")

        conversation = []
        for inv in all_invocations:
            inv_dict = {
                "invocation_id": inv.invocation_id,
                "user_content": inv.user_content.model_dump(exclude_none=True) if inv.user_content else None,
            }
            if inv.final_response:
                inv_dict["final_response"] = inv.final_response.model_dump(exclude_none=True)
            if inv.intermediate_data:
                inv_dict["intermediate_data"] = inv.intermediate_data.model_dump(exclude_none=True)

            conversation.append(inv_dict)

        eval_set = {
            "eval_set_id": request.eval_set_id,
            "eval_cases": [
                {
                    "eval_id": "case_1",
                    "conversation": conversation,
                }
            ],
        }

        return StandardResponse(
            data=CreateEvalSetData(
                eval_set=eval_set,
                num_invocations=len(all_invocations),
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


@streaming_router.post("/evaluate-sessions", response_model=StandardResponse[EvaluateSessionsData])
async def evaluate_sessions(
    request: EvaluateSessionsRequest,
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

        import tempfile

        eval_set_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(eval_set_response.data.eval_set, eval_set_file)
        eval_set_file.close()

        sessions_to_evaluate = [
            (session_id, session) for session_id, session in manager.sessions.items() if session.is_complete
        ]

        logger.info("Evaluating %d complete sessions (of %d total)", len(sessions_to_evaluate), len(manager.sessions))

        sem = asyncio.Semaphore(5)

        async def eval_one_session(session_id: str, session) -> SessionEvalResult:
            async with sem:
                try:
                    trace_file = await manager._save_spans_to_temp_file(session)

                    config = EvalRunConfig(
                        trace_files=[str(trace_file)],
                        trace_format="otlp-json",
                        eval_set_file=eval_set_file.name,
                        metrics=request.metrics,
                        judge_model=request.judge_model,
                        trajectory_match_type=request.trajectory_match_type,
                    )

                    eval_result = await run_evaluation(config)

                    if eval_result.trace_results:
                        trace_result = eval_result.trace_results[0]
                        return SessionEvalResult(
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
                        )
                    else:
                        logger.warning("No trace results for session %s", session_id)
                        return SessionEvalResult(session_id=session_id, error="No trace results")

                except Exception as exc:
                    logger.error(f"Failed to evaluate session {session_id}: {exc}", exc_info=True)
                    return SessionEvalResult(session_id=session_id, error=str(exc))

        results = await asyncio.gather(*[eval_one_session(sid, sess) for sid, sess in sessions_to_evaluate])

        logger.info("Evaluation complete. Total results: %d", len(results))

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

        import os
        import tempfile

        temp_dir = tempfile.gettempdir()

        eval_set_file = os.path.join(temp_dir, f"eval_set_{request.golden_session_id}.json")
        with open(eval_set_file, "w") as f:  # noqa: ASYNC230
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
                eval_set_url=f"/api/streaming/download/{os.path.basename(eval_set_file)}",
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
    """Download a prepared trace or eval set file."""
    import os
    import tempfile

    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, filename)

    if not os.path.exists(file_path):  # noqa: ASYNC240
        raise HTTPException(status_code=404, detail="File not found")

    if not file_path.startswith(temp_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")

    return FileResponse(file_path, media_type="application/json", filename=filename)


@streaming_router.post("/get-trace", response_model=StandardResponse[GetTraceData])
async def get_trace(
    request: GetTraceRequest,
    manager: StreamingTraceManager = Depends(require_trace_manager),
):
    session = manager.sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        import tempfile

        temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)

        unified_trace_id = session.trace_id

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

        enriched_spans = enrich_spans_with_logs(session.spans, session.logs)

        for span in enriched_spans:
            span_copy = span.copy()
            span_copy["traceId"] = unified_trace_id
            temp_file.write(json.dumps(span_copy) + "\n")

        temp_file.close()

        with open(temp_file.name) as f:  # noqa: ASYNC230
            trace_content = f.read()

        return StandardResponse(
            data=GetTraceData(
                session_id=request.session_id,
                trace_content=trace_content,
                num_spans=len(enriched_spans),
            )
        )

    except Exception as exc:
        logger.exception("Failed to get trace")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
