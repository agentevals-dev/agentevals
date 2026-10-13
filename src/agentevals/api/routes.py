"""API routes for agentevals."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic.alias_generators import to_camel

from agentevals import __version__

from ..adk_bridge import invocation_to_dict, to_adk_invocations
from ..builtin_metrics import metric_catalog
from ..config import EvalParams, EvalRunConfig
from ..genai.extract import extract_conversation
from ..loader import load_traces
from ..loader.otlp import OtlpJsonLoader
from ..resolvers import (
    reset_resolved_credentials,
    resolve_credential_refs,
    set_resolved_credentials,
)
from ..run.service import RunService
from ..runner import (
    RunResult,
    evaluation_groups,
    load_eval_set,
    load_eval_set_from_dict,
    run_evaluation,
)
from ..trace_metrics import extract_trace_metadata
from .evaluation import (
    CredentialResolutionError,
    Evaluation,
    EvaluationEvent,
    GroupResult,
    Performance,
    Progress,
    evaluate,
    persist_evaluation,
    stream_evaluation,
)
from .models import (
    ApiKeyStatus,
    ConfigData,
    ConvertTracesData,
    EvalSetValidation,
    EvaluateJsonRequest,
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


@contextmanager
def _scoped_credentials(resolved: dict[str, str] | None):
    """Scope an already-resolved ``logical-name -> secret value`` map to the current task.

    Mirrors the async worker's set/reset (``run/worker.py``) so the synchronous evaluate
    paths populate the same credential ContextVar that judge graders read. A falsy map is a
    no-op, keeping callers byte-for-byte backward compatible. For streaming endpoints, enter
    this BEFORE ``asyncio.create_task`` so the eval task inherits the populated context (a
    child task snapshots its parent's context at creation time). Resolution is done by the
    caller so its failures surface as request errors rather than scoping concerns.
    """
    token = set_resolved_credentials(resolved) if resolved else None
    try:
        yield
    finally:
        if token is not None:
            reset_resolved_credentials(token)


async def _resolve_credentials(refs: dict[str, dict[str, Any]] | None) -> dict[str, str] | None:
    """Resolve credentialRefs to secret values, mapping bad references to a 400.

    Resolver ``ValueError``s (missing/unknown ``kind``, missing locator fields, an unset
    env var) are request/input errors, so surface them as 400s instead of letting them
    bubble up as 500s. Infrastructure failures from custom resolvers raise other exception
    types and are left to propagate as 5xx.
    """
    if not refs:
        return None
    try:
        return await resolve_credential_refs(refs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Could not resolve credentialRefs: {exc}") from exc


def _parse_credential_refs_form(raw: str | None) -> dict[str, dict[str, Any]] | None:
    """Parse and validate the multipart ``credential_refs`` form field (a JSON object string).

    Empty/absent is treated as no credentials. Raises ``ValueError`` (which
    ``json.JSONDecodeError`` subclasses) on malformed JSON or a non-object shape, so callers
    map both to the same error they use for a bad ``config``. The JSON request endpoints get
    this shape check for free from the ``EvaluateJsonRequest`` model.
    """
    if not raw:
        return None
    refs = json.loads(raw)
    if not isinstance(refs, dict) or not all(isinstance(ref, dict) for ref in refs.values()):
        raise ValueError("credentialRefs must be a JSON object mapping each logical name to a reference object")
    return refs


_PAYLOAD_KEYS = frozenset({"args", "arguments", "response", "result", "output"})


def _camel_keys(obj: Any) -> Any:
    """Convert response structure keys to camelCase.

    Values under payload keys (tool arguments, tool results) are user data and pass through
    unchanged, so a tool argument named ``order_id`` stays ``order_id``.
    """
    if isinstance(obj, dict):
        return {to_camel(k): (v if k in _PAYLOAD_KEYS else _camel_keys(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camel_keys(item) for item in obj]
    return obj


def _safe_upload_path(temp_dir: str, filename: str, idx: int = 0) -> str:
    """Build a save path inside ``temp_dir`` from a client-controlled filename.

    ``basename`` drops any directory components, and the ``idx`` prefix keeps
    same-named uploads from clobbering each other.
    """
    return os.path.join(temp_dir, f"{idx}_{os.path.basename(filename)}")


def _load_eval_set_dict(path: str | None) -> dict | None:
    """Read the uploaded eval set file back into a dict for persistence.

    The on-disk file gets cleaned up with the temp dir; capturing the dict
    here lets us store it on the run row so a future ``GET /api/runs/{id}``
    can show what was evaluated against without re-uploading the file.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("could not re-read eval_set file at %s for persistence", path)
        return None


def _run_service(request: Request) -> RunService | None:
    return getattr(request.app.state, "run_service", None)


def _config_json(config: str) -> dict[str, Any]:
    try:
        return json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config JSON: {exc}") from exc


def _credential_refs_form(raw: str | None) -> dict[str, dict[str, Any]] | None:
    try:
        return _parse_credential_refs_form(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid credentialRefs: {exc}") from exc


async def _save_upload(upload: UploadFile, path: str, what: str) -> None:
    content = await upload.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"{what} exceeds 10MB limit")
    with open(path, "wb") as f:  # noqa: ASYNC230
        f.write(content)


async def _save_trace_uploads(trace_files: list[UploadFile], temp_dir: str) -> list[str]:
    paths = []
    for idx, trace_file in enumerate(trace_files):
        if not trace_file.filename:
            continue
        if not trace_file.filename.lower().endswith((".json", ".jsonl")):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file extension for {trace_file.filename}. Only .json and .jsonl files are allowed.",
            )
        path = _safe_upload_path(temp_dir, trace_file.filename, idx)
        await _save_upload(trace_file, path, f"File {trace_file.filename}")
        paths.append(path)
    if not paths:
        raise HTTPException(status_code=400, detail="No valid trace files provided")
    return paths


async def _save_eval_set_upload(eval_set_file: UploadFile | None, temp_dir: str) -> str | None:
    if not eval_set_file or not eval_set_file.filename:
        return None
    if not eval_set_file.filename.endswith(".json"):
        raise HTTPException(
            status_code=400, detail="Invalid file extension for eval set. Only .json files are allowed."
        )
    path = os.path.join(temp_dir, f"evalset_{os.path.basename(eval_set_file.filename)}")
    await _save_upload(eval_set_file, path, "Eval set file")
    return path


def _eval_run_config(config_dict: dict[str, Any], trace_paths: list[str], eval_set_path: str | None) -> EvalRunConfig:
    try:
        return EvalRunConfig.model_validate(
            {
                **config_dict,
                "traceFiles": trace_paths,
                "evalSetFile": eval_set_path,
                "traceFormat": config_dict.get("trace_format"),
            }
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc


def _sse(event: Any) -> str:
    return f"data: {event.model_dump_json(by_alias=True)}\n\n"


def _sse_error(message: str) -> str:
    return _sse(SSEErrorEvent(error=message))


def _sse_event(event: EvaluationEvent) -> str:
    if isinstance(event, Performance):
        payload = SSEPerformanceMetricsEvent(
            trace_id=event.trace_id,
            performance_metrics=_camel_keys(event.performance_metrics),
            trace_metadata=_camel_keys(event.trace_metadata),
        )
        return f"event: performance_metrics\n{_sse(payload)}"
    if isinstance(event, Progress):
        return _sse(SSEProgressEvent(message=event.message))
    if isinstance(event, GroupResult):
        return _sse(
            SSETraceProgressEvent(
                trace_progress=SSETraceProgress(
                    trace_id=event.result.trace_id,
                    partial_result=_camel_keys(event.result.model_dump(by_alias=True)),
                )
            )
        )
    return _sse(SSEDoneEvent(result=_camel_keys(event.result.model_dump(by_alias=True))))


async def _sse_evaluation(evaluation: Evaluation, request: Request) -> AsyncIterator[str]:
    try:
        async for event in stream_evaluation(evaluation, run_service=_run_service(request)):
            yield _sse_event(event)
    except CredentialResolutionError as exc:
        yield _sse_error(f"Could not resolve credentialRefs: {exc}")
    except Exception as exc:
        logger.exception("Evaluation stream failed")
        yield _sse_error(str(exc))


def _event_stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


router = APIRouter()

_MAX_JSON_BODY_BYTES = 50 * 1024 * 1024  # 50 MB (multipart endpoints allow 10 MB per file)
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("/health", response_model=StandardResponse[HealthData])
async def health_check():
    return StandardResponse(data=HealthData(status="ok", version=__version__))


@router.get("/config", response_model=StandardResponse[ConfigData])
async def get_config(request: Request):
    return StandardResponse(
        data=ConfigData(
            api_keys=ApiKeyStatus(
                google=bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
                openai=bool(os.environ.get("OPENAI_API_KEY")),
            ),
            otlp=getattr(request.app.state, "otlp_ports", None),
        )
    )


@router.get("/metrics", response_model=StandardResponse[list[MetricInfo]])
async def list_metrics():
    return StandardResponse(data=[MetricInfo(**dataclasses.asdict(m)) for m in metric_catalog()])


@router.post("/validate/eval-set", response_model=StandardResponse[EvalSetValidation])
async def validate_eval_set(
    eval_set_file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        eval_set_path = os.path.join(temp_dir, f"evalset_{os.path.basename(eval_set_file.filename or 'eval_set.json')}")
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
    """An ADK invocation as the camelCase dict the frontend ``Invocation`` type expects."""
    return _camel_keys(invocation_to_dict(inv))


@router.post("/convert", response_model=StandardResponse[ConvertTracesData])
async def convert_trace_files(
    trace_files: list[UploadFile] = File(...),
    trace_format: str | None = Form(None),
    group_by: str = Form("trace"),
):
    """Convert trace files to invocations and metadata without running evaluation.

    One entry per trace by default. ``group_by`` (``conversation`` or ``auto``, with the evaluation
    meaning) returns one entry per evaluation group instead, keyed by the group's earliest trace
    and listing its members in ``traceIds``, so a client can show what an evaluation will score.
    """
    if group_by not in ("trace", "conversation", "auto"):
        raise HTTPException(status_code=400, detail="group_by must be one of trace, conversation, auto")
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

            trace_path = _safe_upload_path(temp_dir, original, idx)
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
            try:
                traces = load_traces(path, format=trace_format or None)
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

        entries: list[TraceConversionEntry] = []
        for key, members in evaluation_groups(all_traces, group_by):
            first = members[0]
            conversation = extract_conversation(members, key)
            invocations = [_serialize_invocation(inv) for inv in to_adk_invocations(conversation.turns)]
            warnings = [w for t in members for w in t.warnings] + [
                w.reason for t in conversation.turns for w in t.warnings
            ]
            meta_dict = extract_trace_metadata(members, conversation)
            meta = TraceConversionMetadata(
                agent_name=meta_dict.get("agent_name"),
                model=meta_dict.get("model"),
                schema_version=meta_dict.get("schema_version"),
                start_time=meta_dict.get("start_time"),
                user_input_preview=meta_dict.get("user_input_preview"),
                final_output_preview=meta_dict.get("final_output_preview"),
                session_name=_session_name_from_filename(trace_to_filename.get(first.trace_id, "")),
            )
            entries.append(
                TraceConversionEntry(
                    trace_id=first.trace_id,
                    trace_ids=[t.trace_id for t in members],
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
    request: Request,
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: UploadFile | None = File(None),
    credential_refs: str | None = Form(None),
):
    """Evaluate trace files with the given evaluator configuration (a JSON string).

    ``credential_refs`` is an optional JSON object mapping logical credential names to secret
    references, resolved so LLM as judge graders can authenticate.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        config_dict = _config_json(config)
        cred_refs = _credential_refs_form(credential_refs)
        trace_paths = await _save_trace_uploads(trace_files, temp_dir)
        eval_set_path = await _save_eval_set_upload(eval_set_file, temp_dir)
        eval_config = _eval_run_config(config_dict, trace_paths, eval_set_path)

        logger.info(
            "Evaluating %d trace file(s) with evaluators: %s",
            len(trace_paths),
            [e.name for e in eval_config.evaluators],
        )
        resolved_creds = await _resolve_credentials(cred_refs)
        with _scoped_credentials(resolved_creds):
            result = await run_evaluation(eval_config)

        run_id = await persist_evaluation(
            _run_service(request),
            Evaluation(
                traces=[],
                params=eval_config,
                eval_set_dict=_load_eval_set_dict(eval_set_path),
                trace_format=eval_config.trace_format,
                upload_filenames=[tf.filename for tf in trace_files if tf.filename],
            ),
            result,
        )
        if run_id:
            result.run_id = run_id
        return StandardResponse(data=_camel_keys(result.model_dump(by_alias=True)))

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Evaluation failed")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc!s}") from exc
    finally:
        shutil.rmtree(temp_dir)


@router.post("/evaluate/stream")
async def evaluate_traces_stream(
    request: Request,
    trace_files: list[UploadFile] = File(...),
    config: str = Form(...),
    eval_set_file: UploadFile | None = File(None),
    credential_refs: str | None = Form(None),
):
    """Evaluate traces with real-time progress via SSE."""
    temp_dir = tempfile.mkdtemp()
    upload_filenames = [tf.filename for tf in trace_files if tf.filename]

    async def events():
        try:
            try:
                config_dict = _config_json(config)
                cred_refs = _credential_refs_form(credential_refs)
                trace_paths = await _save_trace_uploads(trace_files, temp_dir)
                eval_set_path = await _save_eval_set_upload(eval_set_file, temp_dir)
                eval_config = _eval_run_config(config_dict, trace_paths, eval_set_path)
            except HTTPException as exc:
                yield _sse_error(exc.detail)
                return

            traces = []
            load_errors: list[str] = []
            for path in trace_paths:
                try:
                    traces.extend(load_traces(path, format=eval_config.trace_format))
                except Exception as exc:
                    load_errors.append(f"Failed to load trace file '{os.path.basename(path)}': {exc}")
            eval_set = None
            if eval_set_path:
                try:
                    eval_set = load_eval_set(eval_set_path)
                except Exception as exc:
                    load_errors.append(f"Failed to load eval set: {exc}")

            evaluation = Evaluation(
                traces=traces,
                params=eval_config,
                eval_set=eval_set,
                credential_refs=cred_refs,
                load_errors=load_errors,
                eval_set_dict=_load_eval_set_dict(eval_set_path),
                trace_format=eval_config.trace_format,
                upload_filenames=upload_filenames,
            )
            async for frame in _sse_evaluation(evaluation, request):
                yield frame
        finally:
            shutil.rmtree(temp_dir)

    return _event_stream(events())


def _parse_json_request(request: EvaluateJsonRequest):
    """Parse traces and eval set from an EvaluateJsonRequest.

    Returns (traces, eval_set).  Raises HTTPException on invalid input.
    """
    try:
        traces = OtlpJsonLoader().load_from_dict(request.traces)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not traces:
        raise HTTPException(status_code=400, detail="No traces found in OTLP JSON")

    eval_set = None
    if request.eval_set:
        try:
            eval_set = load_eval_set_from_dict(request.eval_set)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid eval set: {exc}") from exc

    return traces, eval_set


def _check_json_body_size(raw_request: Request):
    content_length = int(raw_request.headers.get("content-length", 0))
    if content_length > _MAX_JSON_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds {_MAX_JSON_BODY_BYTES // (1024 * 1024)}MB limit",
        )


def _json_evaluation(request: EvaluateJsonRequest, traces: list, eval_set) -> Evaluation:
    return Evaluation(
        traces=traces,
        params=request.config,
        eval_set=eval_set,
        credential_refs=request.credential_refs,
        eval_set_dict=request.eval_set,
    )


@router.post("/evaluate/json", response_model=StandardResponse[RunResult])
async def evaluate_traces_json(request: EvaluateJsonRequest, raw_request: Request):
    """Evaluate OTLP JSON traces passed in the request body."""
    _check_json_body_size(raw_request)
    traces, eval_set = _parse_json_request(request)
    try:
        result = await evaluate(_json_evaluation(request, traces, eval_set), run_service=_run_service(raw_request))
        return StandardResponse(data=_camel_keys(result.model_dump(by_alias=True)))
    except CredentialResolutionError as exc:
        raise HTTPException(status_code=400, detail=f"Could not resolve credentialRefs: {exc}") from exc
    except Exception as exc:
        logger.exception("JSON evaluation failed")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc!s}") from exc


@router.post("/evaluate/json/stream")
async def evaluate_traces_json_stream(request: EvaluateJsonRequest, raw_request: Request):
    """Evaluate OTLP JSON traces with real-time progress via SSE."""
    _check_json_body_size(raw_request)

    async def events():
        try:
            traces, eval_set = _parse_json_request(request)
        except HTTPException as exc:
            yield _sse_error(exc.detail)
            return
        async for frame in _sse_evaluation(_json_evaluation(request, traces, eval_set), raw_request):
            yield frame

    return _event_stream(events())
