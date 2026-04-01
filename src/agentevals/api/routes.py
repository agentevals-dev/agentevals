"""API routes for agentevals."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic.alias_generators import to_camel

from agentevals import __version__

from ..builtin_metrics import METRICS_NEEDING_EXPECTED, METRICS_NEEDING_GCP, METRICS_NEEDING_LLM
from ..config import (
    BuiltinMetricDef,
    CodeEvaluatorDef,
    CustomEvaluatorDef,
    EvalRunConfig,
    OpenAIEvalDef,
)
from ..converter import convert_traces
from ..extraction import get_extractor
from ..runner import RunResult, get_loader, load_eval_set, run_evaluation
from ..trace_metrics import extract_performance_metrics, extract_trace_metadata
from .models import (
    ApiKeyStatus,
    ConfigData,
    ConvertTracesData,
    EvalSetValidation,
    HealthData,
    MetricInfo,
    SSEDoneEvent,
    SSEErrorEvent,
    SSEPerformanceMetricsEvent,
    SSEProgressEvent,
    SSETraceProgress,
    SSETraceProgressEvent,
    StandardResponse,
    TraceConversionEntry,
    TraceConversionMetadata,
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

_TYPE_TO_MODEL = {
    "builtin": BuiltinMetricDef,
    "code": CodeEvaluatorDef,
    "openai_eval": OpenAIEvalDef,
}


def _parse_custom_evaluators(raw: list[dict]) -> list[CustomEvaluatorDef]:
    """Parse a list of custom evaluator dicts from the API config JSON."""
    defs: list[CustomEvaluatorDef] = []
    for entry in raw:
        evaluator_type = entry.get("type", "builtin")
        model_cls = _TYPE_TO_MODEL.get(evaluator_type)
        if not model_cls:
            raise ValueError(f"Unknown custom evaluator type: {evaluator_type}")
        defs.append(model_cls.model_validate(entry))
    return defs


@router.get("/health", response_model=StandardResponse[HealthData])
async def health_check():
    return StandardResponse(data=HealthData(status="ok", version=__version__))


@router.get("/config", response_model=StandardResponse[ConfigData])
async def get_config():
    return StandardResponse(
        data=ConfigData(
            api_keys=ApiKeyStatus(
                google=bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
                openai=bool(os.environ.get("OPENAI_API_KEY")),
            )
        )
    )


@router.get("/metrics", response_model=StandardResponse[list[MetricInfo]])
async def list_metrics():
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
        "per_turn_user_simulator_quality_v1": "simulation",
    }

    try:
        from google.adk.evaluation.metric_evaluator_registry import (
            DEFAULT_METRIC_EVALUATOR_REGISTRY,
        )

        registry_metrics = DEFAULT_METRIC_EVALUATOR_REGISTRY.get_registered_metrics()

        metrics = []
        for m in registry_metrics:
            if m.metric_name == "per_turn_user_simulator_quality_v1":
                continue

            metrics.append(
                MetricInfo(
                    name=m.metric_name,
                    category=_METRIC_CATEGORIES.get(m.metric_name, "other"),
                    requires_eval_set=m.metric_name in METRICS_NEEDING_EXPECTED,
                    requires_llm=m.metric_name in METRICS_NEEDING_LLM,
                    requires_gcp=m.metric_name in METRICS_NEEDING_GCP,
                    requires_rubrics=m.metric_name in _METRICS_NEEDING_RUBRICS,
                    description=m.description or "No description available",
                    working=m.metric_name not in _METRICS_NEEDING_RUBRICS,
                )
            )

        return StandardResponse(data=metrics)

    except ImportError:
        fallback = [
            MetricInfo(
                name="tool_trajectory_avg_score",
                category="trajectory",
                requires_eval_set=True,
                requires_llm=False,
                requires_gcp=False,
                requires_rubrics=False,
                working=True,
                description="Compare tool call sequences against expected trajectory",
            ),
            MetricInfo(
                name="response_match_score",
                category="response",
                requires_eval_set=True,
                requires_llm=False,
                requires_gcp=False,
                requires_rubrics=False,
                working=True,
                description="Text similarity between actual and expected responses using ROUGE-1",
            ),
            MetricInfo(
                name="response_evaluation_score",
                category="response",
                requires_eval_set=True,
                requires_llm=False,
                requires_gcp=True,
                requires_rubrics=False,
                working=True,
                description="Semantic evaluation of response quality using Vertex AI",
            ),
            MetricInfo(
                name="final_response_match_v2",
                category="response",
                requires_eval_set=True,
                requires_llm=True,
                requires_gcp=False,
                requires_rubrics=False,
                working=True,
                description="LLM-based comparison of final responses",
            ),
            MetricInfo(
                name="hallucinations_v1",
                category="safety",
                requires_eval_set=False,
                requires_llm=True,
                requires_gcp=False,
                requires_rubrics=False,
                working=True,
                description="Detect hallucinated information in responses",
            ),
            MetricInfo(
                name="safety_v1",
                category="safety",
                requires_eval_set=False,
                requires_llm=False,
                requires_gcp=True,
                requires_rubrics=False,
                working=True,
                description="Safety and security assessment using Vertex AI",
            ),
            MetricInfo(
                name="rubric_based_final_response_quality_v1",
                category="quality",
                requires_eval_set=False,
                requires_llm=True,
                requires_gcp=False,
                requires_rubrics=True,
                working=False,
                description="Rubric-based quality assessment of responses (requires rubrics config)",
            ),
            MetricInfo(
                name="rubric_based_tool_use_quality_v1",
                category="quality",
                requires_eval_set=False,
                requires_llm=True,
                requires_gcp=False,
                requires_rubrics=True,
                working=False,
                description="Rubric-based assessment of tool usage quality (requires rubrics config)",
            ),
        ]
        return StandardResponse(data=fallback)


@router.post("/validate/eval-set", response_model=StandardResponse[EvalSetValidation])
async def validate_eval_set(
    eval_set_file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        eval_set_path = os.path.join(temp_dir, eval_set_file.filename or "eval_set.json")
        with open(eval_set_path, "wb") as f:  # noqa: ASYNC230
            content = await eval_set_file.read()
            f.write(content)

        try:
            eval_set = load_eval_set(eval_set_path)
            return StandardResponse(
                data=EvalSetValidation(
                    valid=True,
                    eval_set_id=eval_set.eval_set_id,
                    num_cases=len(eval_set.eval_cases),
                )
            )
        except Exception as exc:
            return StandardResponse(
                data=EvalSetValidation(
                    valid=False,
                    errors=[str(exc)],
                )
            )

    finally:
        shutil.rmtree(temp_dir)


def _session_name_from_filename(filename: str) -> str | None:
    """Extract a session name from a trace filename, stripping known prefixes."""
    base = re.sub(r"\.(jsonl?|json)$", "", filename, flags=re.IGNORECASE)
    for prefix in ("trace_", "agentevals_"):
        if base.startswith(prefix):
            return base[len(prefix) :]
    return None


def _serialize_invocation(inv) -> dict[str, Any]:
    """Serialize an ADK Invocation to a camelCase dict matching the frontend Invocation type."""
    inv_dict: dict[str, Any] = {
        "invocation_id": inv.invocation_id,
    }
    if inv.user_content is not None:
        inv_dict["user_content"] = inv.user_content.model_dump(exclude_none=True)
    if inv.final_response is not None:
        inv_dict["final_response"] = inv.final_response.model_dump(exclude_none=True)
    if inv.intermediate_data is not None:
        inv_dict["intermediate_data"] = inv.intermediate_data.model_dump(exclude_none=True)
    if inv.creation_timestamp is not None:
        inv_dict["creation_timestamp"] = inv.creation_timestamp
    return _camel_keys(inv_dict)


def _get_format_for_file(path: str, explicit_format: str) -> str:
    """Return the loader format for a single file, auto-detecting from extension."""
    if explicit_format:
        return explicit_format
    return "otlp-json" if path.lower().endswith(".jsonl") else "jaeger-json"


@router.post("/convert", response_model=StandardResponse[ConvertTracesData])
async def convert_trace_files(
    trace_files: list[UploadFile] = File(...),
    trace_format: str = Form(""),
):
    """Convert trace files to invocations and metadata without running evaluation."""
    temp_dir = tempfile.mkdtemp()
    try:
        saved_files: list[tuple[str, str]] = []  # (path, original_filename)
        for idx, trace_file in enumerate(trace_files):
            if not trace_file.filename:
                continue

            original = trace_file.filename
            lower = original.lower()
            if not (lower.endswith(".json") or lower.endswith(".jsonl")):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file extension for {original}. Only .json and .jsonl files are allowed.",
                )

            safe_name = f"{idx}_{os.path.basename(original)}"
            trace_path = os.path.join(temp_dir, safe_name)
            with open(trace_path, "wb") as f:  # noqa: ASYNC230
                content = await trace_file.read()

                if len(content) > 10 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File {original} exceeds 10MB limit",
                    )

                f.write(content)
            saved_files.append((trace_path, original))

        if not saved_files:
            raise HTTPException(status_code=400, detail="No valid trace files provided")

        all_traces = []
        trace_to_filename: dict[str, str] = {}
        load_warnings: list[str] = []
        for path, original in saved_files:
            fmt = _get_format_for_file(path, trace_format)
            loader = get_loader(fmt)
            try:
                traces = loader.load(path)
                for t in traces:
                    trace_to_filename[t.trace_id] = original
                all_traces.extend(traces)
            except Exception as exc:
                msg = f"Failed to load '{original}': {exc}"
                logger.warning(msg)
                load_warnings.append(msg)

        if not all_traces:
            detail = "No traces found in uploaded files"
            if load_warnings:
                detail += ". Errors: " + "; ".join(load_warnings)
            raise HTTPException(status_code=400, detail=detail)

        conversion_results = convert_traces(all_traces)
        trace_map = {t.trace_id: t for t in all_traces}

        entries: list[TraceConversionEntry] = []
        for conv_result in conversion_results:
            invocations = [_serialize_invocation(inv) for inv in conv_result.invocations]
            warnings = list(conv_result.warnings)

            trace = trace_map.get(conv_result.trace_id)
            meta = TraceConversionMetadata()
            if trace:
                meta_dict = extract_trace_metadata(trace)
                filename = trace_to_filename.get(conv_result.trace_id, "")
                session_name = _session_name_from_filename(filename)
                meta = TraceConversionMetadata(
                    agent_name=meta_dict.get("agent_name"),
                    model=meta_dict.get("model"),
                    start_time=meta_dict.get("start_time"),
                    user_input_preview=meta_dict.get("user_input_preview"),
                    final_output_preview=meta_dict.get("final_output_preview"),
                    session_name=session_name,
                )

            entries.append(
                TraceConversionEntry(
                    trace_id=conv_result.trace_id,
                    invocations=invocations,
                    warnings=warnings,
                    metadata=meta,
                )
            )

        return StandardResponse(data=ConvertTracesData(traces=entries))

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Trace conversion failed")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc!s}") from exc
    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate", response_model=StandardResponse[RunResult])
async def evaluate_traces(
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: UploadFile | None = File(None),
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
            raise HTTPException(status_code=400, detail=f"Invalid config JSON: {exc}") from exc

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
            with open(trace_path, "wb") as f:  # noqa: ASYNC230
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
                    detail="Invalid file extension for eval set. Only .json files are allowed.",
                )

            eval_set_path = os.path.join(temp_dir, eval_set_file.filename)
            with open(eval_set_path, "wb") as f:  # noqa: ASYNC230
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

        custom_evaluators: list[CustomEvaluatorDef] = []
        raw_custom = config_dict.get("customEvaluators", config_dict.get("customMetrics", []))
        if raw_custom:
            try:
                custom_evaluators = _parse_custom_evaluators(raw_custom)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid customEvaluators: {exc}") from exc

        eval_config = EvalRunConfig(
            trace_files=trace_paths,
            eval_set_file=eval_set_path,
            metrics=metrics,
            custom_evaluators=custom_evaluators,
            trace_format=trace_format,
            judge_model=config_dict.get("judgeModel"),
            threshold=threshold,
            trajectory_match_type=config_dict.get("trajectoryMatchType"),
        )

        logger.info(f"Evaluating {len(trace_paths)} trace file(s) with metrics: {metrics}")
        result = await run_evaluation(eval_config)

        result_dict = _camel_keys(result.model_dump(by_alias=True))
        return StandardResponse(data=result_dict)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Evaluation failed")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc!s}") from exc

    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate/stream")
async def evaluate_traces_stream(
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: UploadFile | None = File(None),
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
                with open(trace_path, "wb") as f:  # noqa: ASYNC230
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
                with open(eval_set_path, "wb") as f:  # noqa: ASYNC230
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

            custom_evaluators: list[CustomEvaluatorDef] = []
            raw_custom = config_dict.get("customEvaluators", config_dict.get("customMetrics", []))
            if raw_custom:
                try:
                    custom_evaluators = _parse_custom_evaluators(raw_custom)
                except Exception as exc:
                    yield f"data: {SSEErrorEvent(error=f'Invalid customEvaluators: {exc}').model_dump_json(by_alias=True)}\n\n"
                    return

            eval_config = EvalRunConfig(
                trace_files=trace_paths,
                eval_set_file=eval_set_path,
                metrics=metrics,
                custom_evaluators=custom_evaluators,
                trace_format=trace_format,
                judge_model=config_dict.get("judgeModel"),
                threshold=threshold,
                trajectory_match_type=config_dict.get("trajectoryMatchType"),
            )

            loader = get_loader(eval_config.trace_format)
            for trace_file_path in trace_paths:
                try:
                    traces = loader.load(trace_file_path)
                    for trace in traces:
                        extractor = get_extractor(trace)
                        perf_metrics = _camel_keys(extract_performance_metrics(trace, extractor))
                        trace_metadata = _camel_keys(extract_trace_metadata(trace, extractor))
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
