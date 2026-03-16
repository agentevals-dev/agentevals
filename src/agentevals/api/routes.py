"""API routes for agentevals."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from agentevals import __version__
from ..config import EvalRunConfig
from ..extraction import get_extractor
from ..runner import RunResult, load_eval_set, run_evaluation, _extract_performance_metrics, _extract_trace_metadata, get_loader

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": __version__}


@router.get("/config")
async def get_config():
    """Return environment configuration, including which API keys are available."""
    return {
        "apiKeys": {
            "google": bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
        }
    }


@router.get("/metrics")
async def list_metrics():
    """List available metrics with metadata.

    Dynamically loads metrics from ADK registry and adds agentevals metadata.
    """
    from ..runner import _METRICS_NEEDING_EXPECTED, _METRICS_NEEDING_LLM

    _METRICS_NEEDING_GCP = {
        "response_evaluation_score",
        "safety_v1",
    }

    _METRICS_NEEDING_RUBRICS = {
        "rubric_based_final_response_quality_v1",
        "rubric_based_tool_use_quality_v1",
    }

    _METRIC_CATEGORIES = {
        "tool_trajectory_avg_score": "trajectory",
        "response_match_score": "response",
        "response_evaluation_score": "response",
        "final_response_match_v2": "response",
        "rubric_based_final_response_quality_v1": "quality",
        "rubric_based_tool_use_quality_v1": "quality",
        "hallucinations_v1": "safety",
        "safety_v1": "safety",
    }

    try:
        # Try to load from ADK registry (like CLI does)
        from google.adk.evaluation.metric_evaluator_registry import (
            DEFAULT_METRIC_EVALUATOR_REGISTRY,
        )

        registry_metrics = DEFAULT_METRIC_EVALUATOR_REGISTRY.get_registered_metrics()

        # Filter out per_turn_user_simulator_quality_v1 (not applicable to trace eval)
        metrics = []
        for m in registry_metrics:
            if m.metric_name == "per_turn_user_simulator_quality_v1":
                continue

            metrics.append({
                "name": m.metric_name,
                "category": _METRIC_CATEGORIES.get(m.metric_name, "other"),
                "requiresEvalSet": m.metric_name in _METRICS_NEEDING_EXPECTED,
                "requiresLLM": m.metric_name in _METRICS_NEEDING_LLM,
                "requiresGCP": m.metric_name in _METRICS_NEEDING_GCP,
                "requiresRubrics": m.metric_name in _METRICS_NEEDING_RUBRICS,
                "description": m.description or "No description available",
                "working": m.metric_name not in _METRICS_NEEDING_RUBRICS,
            })

        return metrics

    except ImportError:
        return [
            {
                "name": "tool_trajectory_avg_score",
                "category": "trajectory",
                "requiresEvalSet": True,
                "requiresLLM": False,
                "requiresGCP": False,
                "requiresRubrics": False,
                "working": True,
                "description": "Compare tool call sequences against expected trajectory",
            },
            {
                "name": "response_match_score",
                "category": "response",
                "requiresEvalSet": True,
                "requiresLLM": False,
                "requiresGCP": False,
                "requiresRubrics": False,
                "working": True,
                "description": "Text similarity between actual and expected responses using ROUGE-1",
            },
            {
                "name": "response_evaluation_score",
                "category": "response",
                "requiresEvalSet": True,
                "requiresLLM": False,
                "requiresGCP": True,
                "requiresRubrics": False,
                "working": True,
                "description": "Semantic evaluation of response quality using Vertex AI",
            },
            {
                "name": "final_response_match_v2",
                "category": "response",
                "requiresEvalSet": True,
                "requiresLLM": True,
                "requiresGCP": False,
                "requiresRubrics": False,
                "working": True,
                "description": "LLM-based comparison of final responses",
            },
            {
                "name": "hallucinations_v1",
                "category": "safety",
                "requiresEvalSet": False,
                "requiresLLM": True,
                "requiresGCP": False,
                "requiresRubrics": False,
                "working": True,
                "description": "Detect hallucinated information in responses",
            },
            {
                "name": "safety_v1",
                "category": "safety",
                "requiresEvalSet": False,
                "requiresLLM": False,
                "requiresGCP": True,
                "requiresRubrics": False,
                "working": True,
                "description": "Safety and security assessment using Vertex AI",
            },
            {
                "name": "rubric_based_final_response_quality_v1",
                "category": "quality",
                "requiresEvalSet": False,
                "requiresLLM": True,
                "requiresGCP": False,
                "requiresRubrics": True,
                "working": False,
                "description": "Rubric-based quality assessment of responses (requires rubrics config)",
            },
            {
                "name": "rubric_based_tool_use_quality_v1",
                "category": "quality",
                "requiresEvalSet": False,
                "requiresLLM": True,
                "requiresGCP": False,
                "requiresRubrics": True,
                "working": False,
                "description": "Rubric-based assessment of tool usage quality (requires rubrics config)",
            },
        ]


@router.post("/validate/eval-set")
async def validate_eval_set(
    eval_set_file: UploadFile = File(...),
):
    """Validate an eval set file structure."""
    temp_dir = tempfile.mkdtemp()
    try:
        eval_set_path = os.path.join(temp_dir, eval_set_file.filename or "eval_set.json")
        with open(eval_set_path, "wb") as f:
            content = await eval_set_file.read()
            f.write(content)

        try:
            eval_set = load_eval_set(eval_set_path)
            return {
                "valid": True,
                "eval_set_id": eval_set.eval_set_id,
                "num_cases": len(eval_set.eval_cases),
                "errors": [],
            }
        except Exception as exc:
            return {
                "valid": False,
                "errors": [str(exc)],
            }

    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate", response_model=RunResult)
async def evaluate_traces(
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: Optional[UploadFile] = File(None),
):
    """
    Evaluate agent traces using specified metrics.

    Args:
        trace_files: List of Jaeger JSON trace files
        config: JSON string with evaluation configuration
        eval_set_file: Optional golden eval set file

    Returns:
        RunResult with trace results and any errors
    """
    temp_dir = tempfile.mkdtemp()
    try:
        try:
            config_dict = json.loads(config)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config JSON: {exc}")

        trace_paths = []
        for trace_file in trace_files:
            if not trace_file.filename:
                continue

            if not (trace_file.filename.endswith(".json") or trace_file.filename.endswith(".jsonl")):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file extension for {trace_file.filename}. Only .json and .jsonl files are allowed.",
                )

            trace_path = os.path.join(temp_dir, trace_file.filename)
            with open(trace_path, "wb") as f:
                content = await trace_file.read()

                if len(content) > 10 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File {trace_file.filename} exceeds 10MB limit",
                    )

                f.write(content)
            trace_paths.append(trace_path)

        if not trace_paths:
            raise HTTPException(
                status_code=400,
                detail="No valid trace files provided",
            )

        trace_format = config_dict.get("trace_format")
        if not trace_format:
            first_file = trace_paths[0]
            if first_file.endswith(".jsonl"):
                trace_format = "otlp-json"
            else:
                trace_format = "jaeger-json"

        eval_set_path = None
        if eval_set_file and eval_set_file.filename:
            if not eval_set_file.filename.endswith(".json"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file extension for eval set. Only .json files are allowed.",
                )

            eval_set_path = os.path.join(temp_dir, eval_set_file.filename)
            with open(eval_set_path, "wb") as f:
                content = await eval_set_file.read()
                if len(content) > 10 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="Eval set file exceeds 10MB limit",
                    )
                f.write(content)

        metrics = config_dict.get("metrics", ["tool_trajectory_avg_score"])
        if not metrics or not isinstance(metrics, list):
            raise HTTPException(
                status_code=400,
                detail="Config must include 'metrics' as a non-empty array",
            )

        threshold = config_dict.get("threshold")
        if threshold is not None and (threshold < 0 or threshold > 1):
            raise HTTPException(
                status_code=400,
                detail="Threshold must be between 0 and 1",
            )

        eval_config = EvalRunConfig(
            trace_files=trace_paths,
            eval_set_file=eval_set_path,
            metrics=metrics,
            trace_format=trace_format,
            judge_model=config_dict.get("judgeModel"),  # camelCase from UI
            threshold=threshold,
        )

        logger.info(f"Evaluating {len(trace_paths)} trace file(s) with metrics: {metrics}")
        result = await run_evaluation(eval_config)

        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Evaluation failed")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}")

    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate/stream")
async def evaluate_traces_stream(
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: Optional[UploadFile] = File(None),
):
    """Evaluate traces with real-time progress via SSE."""
    temp_dir = tempfile.mkdtemp()

    async def event_generator():
        try:
            try:
                config_dict = json.loads(config)
            except json.JSONDecodeError as exc:
                yield f"data: {json.dumps({'error': f'Invalid config JSON: {exc}'})}\n\n"
                return

            trace_paths = []
            for trace_file in trace_files:
                if not trace_file.filename:
                    continue

                if not (trace_file.filename.endswith(".json") or trace_file.filename.endswith(".jsonl")):
                    yield f"data: {json.dumps({'error': f'Invalid file extension for {trace_file.filename}'})}\n\n"
                    return

                trace_path = os.path.join(temp_dir, trace_file.filename)
                with open(trace_path, "wb") as f:
                    content = await trace_file.read()

                    if len(content) > 10 * 1024 * 1024:
                        yield f"data: {json.dumps({'error': f'File {trace_file.filename} exceeds 10MB'})}\n\n"
                        return

                    f.write(content)
                trace_paths.append(trace_path)

            if not trace_paths:
                yield f"data: {json.dumps({'error': 'No valid trace files provided'})}\n\n"
                return

            trace_format = config_dict.get("trace_format")
            if not trace_format:
                first_file = trace_paths[0]
                if first_file.endswith(".jsonl"):
                    trace_format = "otlp-json"
                else:
                    trace_format = "jaeger-json"

            eval_set_path = None
            if eval_set_file and eval_set_file.filename:
                if not eval_set_file.filename.endswith(".json"):
                    yield f"data: {json.dumps({'error': 'Invalid file extension for eval set'})}\n\n"
                    return

                eval_set_path = os.path.join(temp_dir, eval_set_file.filename)
                with open(eval_set_path, "wb") as f:
                    content = await eval_set_file.read()
                    if len(content) > 10 * 1024 * 1024:
                        yield f"data: {json.dumps({'error': 'Eval set file exceeds 10MB'})}\n\n"
                        return
                    f.write(content)

            metrics = config_dict.get("metrics", ["tool_trajectory_avg_score"])
            if not metrics or not isinstance(metrics, list):
                yield f"data: {json.dumps({'error': 'Config must include metrics as a non-empty array'})}\n\n"
                return

            threshold = config_dict.get("threshold")
            if threshold is not None and (threshold < 0 or threshold > 1):
                yield f"data: {json.dumps({'error': 'Threshold must be between 0 and 1'})}\n\n"
                return

            eval_config = EvalRunConfig(
                trace_files=trace_paths,
                eval_set_file=eval_set_path,
                metrics=metrics,
                trace_format=trace_format,
                judge_model=config_dict.get("judgeModel"),
                threshold=threshold,
            )

            loader = get_loader(eval_config.trace_format)
            for trace_file_path in trace_paths:
                try:
                    traces = loader.load(trace_file_path)
                    for trace in traces:
                        extractor = get_extractor(trace)
                        perf_metrics = _extract_performance_metrics(trace, extractor)
                        trace_metadata = _extract_trace_metadata(trace, extractor)
                        event_data = {
                            "traceId": trace.trace_id,
                            "performanceMetrics": perf_metrics,
                            "traceMetadata": trace_metadata,
                        }
                        yield f"event: performance_metrics\ndata: {json.dumps(event_data)}\n\n"
                except Exception as e:
                    logger.error(f"Failed to extract early performance metrics from {trace_file_path}: {e}")

            queue: asyncio.Queue = asyncio.Queue()

            async def progress_callback(message: str):
                await queue.put({"message": message})

            async def trace_progress_callback(trace_result):
                partial_result = {
                    "traceId": trace_result.trace_id,
                    "numInvocations": trace_result.num_invocations,
                    "metricResults": [
                        {
                            "metricName": mr.metric_name,
                            "score": mr.score,
                            "evalStatus": mr.eval_status,
                            "perInvocationScores": mr.per_invocation_scores,
                            "error": mr.error,
                            "details": mr.details,
                        }
                        for mr in trace_result.metric_results
                    ],
                    "conversionWarnings": trace_result.conversion_warnings,
                    "performanceMetrics": trace_result.performance_metrics,
                }
                await queue.put({
                    "traceProgress": {
                        "traceId": trace_result.trace_id,
                        "partialResult": partial_result,
                    }
                })

            async def run_with_progress():
                result = await run_evaluation(eval_config, progress_callback, trace_progress_callback)
                await queue.put({"done": True, "result": result})

            eval_task = asyncio.create_task(run_with_progress())

            try:
                while True:
                    event = await queue.get()

                    if "done" in event:
                        result = event["result"]
                        final_event = {
                            "done": True,
                            "result": {
                                "traceResults": [
                                    {
                                        "traceId": tr.trace_id,
                                        "numInvocations": tr.num_invocations,
                                        "metricResults": [
                                            {
                                                "metricName": mr.metric_name,
                                                "score": mr.score,
                                                "evalStatus": mr.eval_status,
                                                "perInvocationScores": mr.per_invocation_scores,
                                                "error": mr.error,
                                                "details": mr.details,
                                            }
                                            for mr in tr.metric_results
                                        ],
                                        "conversionWarnings": tr.conversion_warnings,
                                        "performanceMetrics": tr.performance_metrics,
                                    }
                                    for tr in result.trace_results
                                ],
                                "errors": result.errors,
                                "performanceMetrics": result.performance_metrics,
                            },
                        }
                        yield f"data: {json.dumps(final_event)}\n\n"
                        break
                    else:
                        yield f"data: {json.dumps(event)}\n\n"
            finally:
                if not eval_task.done():
                    eval_task.cancel()
                    try:
                        await eval_task
                    except asyncio.CancelledError:
                        pass

        except Exception as exc:
            logger.exception("Evaluation stream failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        finally:
            shutil.rmtree(temp_dir)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
