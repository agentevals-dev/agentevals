"""API routes for agentevals."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic.alias_generators import to_camel

from agentevals import __version__
from ..config import EvalRunConfig
from ..extraction import get_extractor
from ..runner import RunResult, load_eval_set, run_evaluation, _extract_performance_metrics, _extract_trace_metadata, get_loader
from .models import (
    StandardResponse,
    HealthData,
    ConfigData,
    ApiKeyStatus,
    MetricInfo,
    EvalSetValidation,
    SSEProgressEvent,
    SSETraceProgressEvent,
    SSETraceProgress,
    SSEPerformanceMetricsEvent,
    SSEDoneEvent,
    SSEErrorEvent,
)

logger = logging.getLogger(__name__)


def _camel_keys(obj: Any) -> Any:
    """Recursively convert dict keys from snake_case to camelCase."""
    if isinstance(obj, dict):
        return {to_camel(k): _camel_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camel_keys(item) for item in obj]
    return obj

router = APIRouter()


@router.get("/health", response_model=StandardResponse[HealthData])
async def health_check():
    return StandardResponse(data=HealthData(status="ok", version=__version__))


@router.get("/config", response_model=StandardResponse[ConfigData])
async def get_config():
    return StandardResponse(data=ConfigData(
        api_keys=ApiKeyStatus(
            google=bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
            anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
            openai=bool(os.environ.get("OPENAI_API_KEY")),
        )
    ))


@router.get("/metrics", response_model=StandardResponse[list[MetricInfo]])
async def list_metrics():
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

            metrics.append(MetricInfo(
                name=m.metric_name,
                category=_METRIC_CATEGORIES.get(m.metric_name, "other"),
                requires_eval_set=m.metric_name in _METRICS_NEEDING_EXPECTED,
                requires_llm=m.metric_name in _METRICS_NEEDING_LLM,
                requires_gcp=m.metric_name in _METRICS_NEEDING_GCP,
                requires_rubrics=m.metric_name in _METRICS_NEEDING_RUBRICS,
                description=m.description or "No description available",
                working=m.metric_name not in _METRICS_NEEDING_RUBRICS,
            ))

        return StandardResponse(data=metrics)

    except ImportError:
        fallback = [
            MetricInfo(name="tool_trajectory_avg_score", category="trajectory", requires_eval_set=True, requires_llm=False, requires_gcp=False, requires_rubrics=False, working=True, description="Compare tool call sequences against expected trajectory"),
            MetricInfo(name="response_match_score", category="response", requires_eval_set=True, requires_llm=False, requires_gcp=False, requires_rubrics=False, working=True, description="Text similarity between actual and expected responses using ROUGE-1"),
            MetricInfo(name="response_evaluation_score", category="response", requires_eval_set=True, requires_llm=False, requires_gcp=True, requires_rubrics=False, working=True, description="Semantic evaluation of response quality using Vertex AI"),
            MetricInfo(name="final_response_match_v2", category="response", requires_eval_set=True, requires_llm=True, requires_gcp=False, requires_rubrics=False, working=True, description="LLM-based comparison of final responses"),
            MetricInfo(name="hallucinations_v1", category="safety", requires_eval_set=False, requires_llm=True, requires_gcp=False, requires_rubrics=False, working=True, description="Detect hallucinated information in responses"),
            MetricInfo(name="safety_v1", category="safety", requires_eval_set=False, requires_llm=False, requires_gcp=True, requires_rubrics=False, working=True, description="Safety and security assessment using Vertex AI"),
            MetricInfo(name="rubric_based_final_response_quality_v1", category="quality", requires_eval_set=False, requires_llm=True, requires_gcp=False, requires_rubrics=True, working=False, description="Rubric-based quality assessment of responses (requires rubrics config)"),
            MetricInfo(name="rubric_based_tool_use_quality_v1", category="quality", requires_eval_set=False, requires_llm=True, requires_gcp=False, requires_rubrics=True, working=False, description="Rubric-based assessment of tool usage quality (requires rubrics config)"),
        ]
        return StandardResponse(data=fallback)


@router.post("/validate/eval-set", response_model=StandardResponse[EvalSetValidation])
async def validate_eval_set(
    eval_set_file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        eval_set_path = os.path.join(temp_dir, eval_set_file.filename or "eval_set.json")
        with open(eval_set_path, "wb") as f:
            content = await eval_set_file.read()
            f.write(content)

        try:
            eval_set = load_eval_set(eval_set_path)
            return StandardResponse(data=EvalSetValidation(
                valid=True,
                eval_set_id=eval_set.eval_set_id,
                num_cases=len(eval_set.eval_cases),
            ))
        except Exception as exc:
            return StandardResponse(data=EvalSetValidation(
                valid=False,
                errors=[str(exc)],
            ))

    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate", response_model=StandardResponse[RunResult])
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

        result_dict = _camel_keys(result.model_dump(by_alias=True))
        return StandardResponse(data=result_dict)

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
                yield f"data: {SSEErrorEvent(error=f'Invalid config JSON: {exc}').model_dump_json(by_alias=True)}\n\n"
                return

            trace_paths = []
            for trace_file in trace_files:
                if not trace_file.filename:
                    continue

                if not (trace_file.filename.endswith(".json") or trace_file.filename.endswith(".jsonl")):
                    yield f"data: {SSEErrorEvent(error=f'Invalid file extension for {trace_file.filename}').model_dump_json(by_alias=True)}\n\n"
                    return

                trace_path = os.path.join(temp_dir, trace_file.filename)
                with open(trace_path, "wb") as f:
                    content = await trace_file.read()

                    if len(content) > 10 * 1024 * 1024:
                        yield f"data: {SSEErrorEvent(error=f'File {trace_file.filename} exceeds 10MB').model_dump_json(by_alias=True)}\n\n"
                        return

                    f.write(content)
                trace_paths.append(trace_path)

            if not trace_paths:
                yield f"data: {SSEErrorEvent(error='No valid trace files provided').model_dump_json(by_alias=True)}\n\n"
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
                    yield f"data: {SSEErrorEvent(error='Invalid file extension for eval set').model_dump_json(by_alias=True)}\n\n"
                    return

                eval_set_path = os.path.join(temp_dir, eval_set_file.filename)
                with open(eval_set_path, "wb") as f:
                    content = await eval_set_file.read()
                    if len(content) > 10 * 1024 * 1024:
                        yield f"data: {SSEErrorEvent(error='Eval set file exceeds 10MB').model_dump_json(by_alias=True)}\n\n"
                        return
                    f.write(content)

            metrics = config_dict.get("metrics", ["tool_trajectory_avg_score"])
            if not metrics or not isinstance(metrics, list):
                yield f"data: {SSEErrorEvent(error='Config must include metrics as a non-empty array').model_dump_json(by_alias=True)}\n\n"
                return

            threshold = config_dict.get("threshold")
            if threshold is not None and (threshold < 0 or threshold > 1):
                yield f"data: {SSEErrorEvent(error='Threshold must be between 0 and 1').model_dump_json(by_alias=True)}\n\n"
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
                        perf_metrics = _camel_keys(_extract_performance_metrics(trace, extractor))
                        trace_metadata = _camel_keys(_extract_trace_metadata(trace, extractor, source_file=trace_file_path))
                        evt = SSEPerformanceMetricsEvent(
                            trace_id=trace.trace_id,
                            performance_metrics=perf_metrics,
                            trace_metadata=trace_metadata,
                        )
                        yield f"event: performance_metrics\ndata: {evt.model_dump_json(by_alias=True)}\n\n"
                except Exception as e:
                    logger.error(f"Failed to extract early performance metrics from {trace_file_path}: {e}")

            queue: asyncio.Queue = asyncio.Queue()

            async def progress_callback(message: str):
                await queue.put(("progress", message))

            async def trace_progress_callback(trace_result):
                await queue.put(("trace_progress", trace_result))

            async def run_with_progress():
                result = await run_evaluation(eval_config, progress_callback, trace_progress_callback)
                await queue.put(("done", result))

            eval_task = asyncio.create_task(run_with_progress())

            try:
                while True:
                    msg = await queue.get()
                    tag, payload = msg

                    if tag == "done":
                        evt = SSEDoneEvent(
                            result=_camel_keys(payload.model_dump(by_alias=True)),
                        )
                        yield f"data: {evt.model_dump_json(by_alias=True)}\n\n"
                        break
                    elif tag == "trace_progress":
                        evt = SSETraceProgressEvent(
                            trace_progress=SSETraceProgress(
                                trace_id=payload.trace_id,
                                partial_result=_camel_keys(payload.model_dump(by_alias=True)),
                            )
                        )
                        yield f"data: {evt.model_dump_json(by_alias=True)}\n\n"
                    elif tag == "progress":
                        evt = SSEProgressEvent(message=payload)
                        yield f"data: {evt.model_dump_json(by_alias=True)}\n\n"
            finally:
                if not eval_task.done():
                    eval_task.cancel()
                    try:
                        await eval_task
                    except asyncio.CancelledError:
                        pass

        except Exception as exc:
            logger.exception("Evaluation stream failed")
            evt = SSEErrorEvent(error=str(exc))
            yield f"data: {evt.model_dump_json(by_alias=True)}\n\n"

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
