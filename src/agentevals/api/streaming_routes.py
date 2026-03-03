"""API routes for streaming session management."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..converter import convert_traces
from ..loader.otlp import OtlpJsonLoader
from ..runner import run_evaluation
from ..config import EvalRunConfig
from ..utils.log_enrichment import enrich_spans_with_logs

logger = logging.getLogger(__name__)

streaming_router = APIRouter()

trace_manager = None


def set_trace_manager(manager):
    """Set the trace manager instance."""
    global trace_manager
    trace_manager = manager


class CreateEvalSetRequest(BaseModel):
    session_id: str
    eval_set_id: str


class EvaluateSessionsRequest(BaseModel):
    golden_session_id: str
    eval_set_id: str
    metrics: list[str] = ["tool_trajectory_avg_score"]
    judge_model: str = "gemini-2.5-flash"


class PrepareEvaluationRequest(BaseModel):
    golden_session_id: str
    session_ids: list[str]


class GetTraceRequest(BaseModel):
    session_id: str


@streaming_router.get("/sessions")
async def list_sessions():
    """Get all active and completed sessions."""
    sessions_data = []

    for session_id, session in trace_manager.sessions.items():
        sessions_data.append({
            "sessionId": session_id,
            "traceId": session.trace_id,
            "evalSetId": session.eval_set_id,
            "spanCount": len(session.spans),
            "isComplete": session.is_complete,
            "startedAt": session.started_at.isoformat(),
            "metadata": session.metadata,
        })

    return sessions_data


@streaming_router.post("/create-eval-set")
async def create_eval_set_from_session(request: CreateEvalSetRequest):
    """Convert a session's trace into an EvalSet."""
    session = trace_manager.sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        trace_file = await trace_manager._save_spans_to_temp_file(session)
        loader = OtlpJsonLoader()
        traces = loader.load(str(trace_file))

        if not traces:
            raise HTTPException(status_code=400, detail="No traces found in session")

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
            ]
        }

        return {
            "eval_set": eval_set,
            "num_invocations": len(all_invocations),
        }

    except Exception as exc:
        logger.exception("Failed to create eval set")
        raise HTTPException(status_code=500, detail=str(exc))


@streaming_router.post("/evaluate-sessions")
async def evaluate_sessions(request: EvaluateSessionsRequest):
    """Evaluate all sessions against a golden session converted to EvalSet."""
    golden_session = trace_manager.sessions.get(request.golden_session_id)
    if not golden_session:
        raise HTTPException(status_code=404, detail="Golden session not found")

    try:
        eval_set_response = await create_eval_set_from_session(
            CreateEvalSetRequest(
                session_id=request.golden_session_id,
                eval_set_id=request.eval_set_id,
            )
        )

        import tempfile
        eval_set_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(eval_set_response["eval_set"], eval_set_file)
        eval_set_file.close()

        results = []

        logger.info("Evaluating sessions. Total sessions: %d", len(trace_manager.sessions))

        for session_id, session in trace_manager.sessions.items():
            logger.info("Checking session %s: is_complete=%s", session_id, session.is_complete)

            if not session.is_complete:
                logger.info("Skipping incomplete session: %s", session_id)
                continue

            logger.info("Evaluating session: %s", session_id)

            try:
                trace_file = await trace_manager._save_spans_to_temp_file(session)
                logger.info("Saved trace file for session %s: %s", session_id, trace_file)

                config = EvalRunConfig(
                    trace_files=[str(trace_file)],
                    trace_format="otlp-json",
                    eval_set_file=eval_set_file.name,
                    metrics=request.metrics,
                    judge_model=request.judge_model,
                )

                logger.info("Running evaluation for session %s with metrics: %s", session_id, request.metrics)
                eval_result = await run_evaluation(config)
                logger.info("Evaluation complete for session %s. Trace results: %d", session_id, len(eval_result.trace_results) if eval_result.trace_results else 0)

                if eval_result.trace_results:
                    trace_result = eval_result.trace_results[0]

                    results.append({
                        "sessionId": session_id,
                        "traceId": trace_result.trace_id,
                        "numInvocations": trace_result.num_invocations,
                        "metricResults": [
                            {
                                "metricName": mr.metric_name,
                                "score": mr.score,
                                "evalStatus": mr.eval_status,
                                "error": mr.error,
                            }
                            for mr in trace_result.metric_results
                        ],
                    })
                    logger.info("Added result for session %s", session_id)
                else:
                    logger.warning("No trace results for session %s", session_id)

            except Exception as exc:
                logger.error(f"Failed to evaluate session {session_id}: {exc}", exc_info=True)
                results.append({
                    "sessionId": session_id,
                    "error": str(exc),
                })

        logger.info("Evaluation complete. Total results: %d", len(results))

        return {
            "goldenSessionId": request.golden_session_id,
            "evalSetId": request.eval_set_id,
            "results": results,
        }

    except Exception as exc:
        logger.exception("Failed to evaluate sessions")
        raise HTTPException(status_code=500, detail=str(exc))


@streaming_router.post("/prepare-evaluation")
async def prepare_evaluation(request: PrepareEvaluationRequest):
    """Prepare evaluation by saving traces and eval set as downloadable files."""
    golden_session = trace_manager.sessions.get(request.golden_session_id)
    if not golden_session:
        raise HTTPException(status_code=404, detail="Golden session not found")

    try:
        eval_set_response = await create_eval_set_from_session(
            CreateEvalSetRequest(
                session_id=request.golden_session_id,
                eval_set_id=f"golden_{request.golden_session_id}",
            )
        )

        import tempfile
        import os

        temp_dir = tempfile.gettempdir()

        eval_set_file = os.path.join(temp_dir, f"eval_set_{request.golden_session_id}.json")
        with open(eval_set_file, 'w') as f:
            json.dump(eval_set_response["eval_set"], f)

        trace_files = []
        for session_id in request.session_ids:
            session = trace_manager.sessions.get(session_id)
            if not session or not session.is_complete:
                continue

            trace_file = await trace_manager._save_spans_to_temp_file(session)
            trace_files.append({
                "session_id": session_id,
                "file_path": str(trace_file),
            })

        return {
            "eval_set_url": f"http://localhost:8001/api/streaming/download/{os.path.basename(eval_set_file)}",
            "trace_urls": [
                f"http://localhost:8001/api/streaming/download/{os.path.basename(tf['file_path'])}"
                for tf in trace_files
            ],
            "num_traces": len(trace_files),
        }

    except Exception as exc:
        logger.exception("Failed to prepare evaluation")
        raise HTTPException(status_code=500, detail=str(exc))


@streaming_router.get("/download/{filename}")
async def download_file(filename: str):
    """Download a prepared trace or eval set file."""
    import tempfile
    import os

    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, filename)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not file_path.startswith(temp_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")

    return FileResponse(file_path, media_type="application/json", filename=filename)




@streaming_router.post("/get-trace")
async def get_trace(request: GetTraceRequest):
    """Get the OTLP JSONL trace content for a session."""
    session = trace_manager.sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        import tempfile

        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)

        unified_trace_id = session.trace_id

        has_genai_spans = any(
            span.get("attributes", [])
            and any(
                attr.get("key") in ("gen_ai.request.model", "gen_ai.input.messages")
                for attr in span.get("attributes", [])
            )
            for span in session.spans
        )

        if has_genai_spans and not session.logs:
            logger.warning(
                "Session %s has GenAI spans but no logs. "
                "Message content will be missing unless spans already enriched.",
                request.session_id
            )

        enriched_spans = enrich_spans_with_logs(session.spans, session.logs)

        for span in enriched_spans:
            span_copy = span.copy()
            span_copy['traceId'] = unified_trace_id
            temp_file.write(json.dumps(span_copy) + "\n")

        temp_file.close()

        with open(temp_file.name, 'r') as f:
            trace_content = f.read()

        return {
            "session_id": request.session_id,
            "trace_content": trace_content,
            "num_spans": len(enriched_spans),
        }

    except Exception as exc:
        logger.exception("Failed to get trace")
        raise HTTPException(status_code=500, detail=str(exc))
