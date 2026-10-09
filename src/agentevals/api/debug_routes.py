from __future__ import annotations

import dataclasses
import importlib.metadata
import io
import json
import logging
import os
import platform
import sys
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentevals import __version__

from ..otel.decode import decode_bare_spans_json, decode_json_document
from ..otel.identity import coerce_key
from ..otel.model import EMPTY_SCOPE, LogRecord, Resource, Scope, Span
from ..utils.log_buffer import log_buffer
from .dependencies import get_trace_manager, require_trace_manager
from .models import DebugLoadData, StandardResponse

if TYPE_CHECKING:
    from ..streaming.manager import LiveManager

logger = logging.getLogger(__name__)

debug_router = APIRouter()

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_ENTRY_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_SESSIONS = 200
_SESSION_FILES = ("otlp.json", "spans.json", "logs.json", "session_meta.json")


class FrontendDiagnostics(BaseModel):
    user_description: str = ""
    browser_info: dict = {}
    console_logs: list[dict] = []
    app_state: dict = {}
    network_errors: list[dict] = []


def _get_package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _collect_environment() -> dict:
    packages = [
        "fastapi",
        "uvicorn",
        "google-adk",
        "google-genai",
        "opentelemetry-sdk",
        "opentelemetry-api",
        "pydantic",
    ]
    return {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "agentevals_version": __version__,
        "python_version": sys.version,
        "os": platform.system(),
        "os_version": platform.release(),
        "machine": platform.machine(),
        "packages": {p: _get_package_version(p) for p in packages},
        "config": {
            "log_level": os.getenv("AGENTEVALS_LOG_LEVEL", "INFO"),
            "live_mode": os.getenv("AGENTEVALS_LIVE") == "1",
        },
        "api_keys": {
            "google": bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
            "openai": bool(os.getenv("OPENAI_API_KEY")),
        },
    }


@debug_router.post("/bundle")
async def create_debug_bundle(
    diagnostics: FrontendDiagnostics,
    manager: LiveManager | None = Depends(get_trace_manager),
):
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    prefix = f"bug-report-{timestamp}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        env = _collect_environment()
        metadata = {
            **env,
            "user_description": diagnostics.user_description,
            "browser_info": diagnostics.browser_info,
        }
        zf.writestr(f"{prefix}/metadata.json", json.dumps(metadata, indent=2))

        # Directories are numbered: session ids are chosen by the producer and must never
        # become archive paths.
        sessions = list(manager.sessions.values()) if manager else []
        for index, session in enumerate(sessions, start=1):
            base = f"{prefix}/sessions/{index:04d}"
            zf.writestr(f"{base}/otlp.json", json.dumps(manager.session_document(session)))
            session_meta = {
                "session_id": session.session_id,
                "trace_id": manager.store.primary_trace_id(session),
                "trace_ids": session.trace_ids,
                "eval_set_id": session.eval_set_id,
                "started_at": session.started_at.isoformat(),
                "is_complete": session.is_complete,
                "span_count": session.span_count,
                "log_count": session.log_count,
                "metadata": session.metadata,
            }
            zf.writestr(f"{base}/session_meta.json", json.dumps(session_meta, indent=2, default=str))

        zf.writestr(f"{prefix}/backend_logs.txt", log_buffer.get_text())
        zf.writestr(f"{prefix}/frontend_state.json", json.dumps(diagnostics.app_state, indent=2))
        zf.writestr(f"{prefix}/console_logs.json", json.dumps(diagnostics.console_logs, indent=2))
        zf.writestr(f"{prefix}/network_errors.json", json.dumps(diagnostics.network_errors, indent=2))

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="bug-report-{timestamp}.zip"'},
    )


async def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail=f"Bundle exceeds {MAX_BUNDLE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _session_entries(zf: zipfile.ZipFile) -> dict[str, dict[str, zipfile.ZipInfo]]:
    dirs: dict[str, dict[str, zipfile.ZipInfo]] = {}
    total = 0
    for info in zf.infolist():
        parts = info.filename.split("/")
        if len(parts) < 4 or parts[-3] != "sessions" or parts[-1] not in _SESSION_FILES:
            continue
        if info.file_size > MAX_ENTRY_BYTES:
            raise HTTPException(status_code=413, detail=f"Bundle entry {info.filename!r} is too large")
        total += info.file_size
        if total > MAX_TOTAL_ENTRY_BYTES:
            raise HTTPException(status_code=413, detail="Bundle content is too large")
        dirs.setdefault("/".join(parts[:-1]), {})[parts[-1]] = info
        if len(dirs) > MAX_BUNDLE_SESSIONS:
            raise HTTPException(status_code=413, detail=f"Bundle holds more than {MAX_BUNDLE_SESSIONS} sessions")
    return dirs


def _read_json(zf: zipfile.ZipFile, info: zipfile.ZipInfo | None, default: Any) -> Any:
    if info is None:
        return default
    try:
        return json.loads(zf.read(info))
    except (ValueError, RecursionError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"Unreadable bundle entry {info.filename!r}") from exc


def _legacy_session(spans: Any, logs: Any, metadata: Mapping[str, Any]) -> tuple[list[Span], list[LogRecord]]:
    """Read a bundle written before the store: bare spans with the scope flattened into
    ``otel.scope.*`` attributes, ``service.name`` in the session metadata, and internal log
    dicts that carry only their span id."""
    service_name = metadata.get("service.name")
    resource = Resource(attributes={"service.name": service_name} if isinstance(service_name, str) else {})
    decoded = decode_bare_spans_json(spans if isinstance(spans, list) else [], strict=False)
    scopes: dict[tuple[str, str | None], Scope] = {}
    out_spans = []
    for span in decoded.spans:
        name, version = span.attributes.get("otel.scope.name"), span.attributes.get("otel.scope.version")
        key = (name if isinstance(name, str) else "", version if isinstance(version, str) else None)
        scope = scopes.setdefault(key, Scope(name=key[0], version=key[1]))
        out_spans.append(dataclasses.replace(span, resource=resource, scope=scope))

    trace_of_span = {s.span_id: s.trace_id for s in out_spans}
    trace_ids = {s.trace_id for s in out_spans}
    only_trace = next(iter(trace_ids)) if len(trace_ids) == 1 else None
    out_logs = []
    for entry in logs if isinstance(logs, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("event_name"), str):
            continue
        span_id = entry.get("span_id") if isinstance(entry.get("span_id"), str) else None
        trace_id = trace_of_span.get(span_id or "") or only_trace
        if trace_id is None:
            continue
        try:
            time_ns = int(entry.get("timestamp") or 0) or None
        except (TypeError, ValueError):
            time_ns = None
        attributes = entry.get("attributes")
        out_logs.append(
            LogRecord(
                time_unix_nano=time_ns,
                observed_time_unix_nano=None,
                event_name=entry["event_name"],
                severity_number=None,
                severity_text=None,
                body=entry.get("body"),
                attributes=attributes if isinstance(attributes, dict) else {},
                trace_id=trace_id,
                span_id=span_id,
                flags=None,
                resource=resource,
                scope=EMPTY_SCOPE,
            )
        )
    return out_spans, out_logs


@debug_router.post("/load", response_model=StandardResponse[DebugLoadData])
async def load_debug_bundle(
    file: UploadFile = FastAPIFile(...),
    manager: LiveManager = Depends(require_trace_manager),
):
    content = await _read_upload(file)
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid ZIP file") from exc

    session_dirs = _session_entries(zf)
    if not any("otlp.json" in f or "spans.json" in f for f in session_dirs.values()):
        raise HTTPException(status_code=400, detail="No sessions found in ZIP")

    loaded = []
    for directory, files in session_dirs.items():
        meta = _read_json(zf, files.get("session_meta.json"), {})
        meta = meta if isinstance(meta, dict) else {}
        metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        if "otlp.json" in files:
            decoded = decode_json_document(_read_json(zf, files["otlp.json"], {}), strict=False)
            spans, logs = decoded.spans, decoded.logs
        elif "spans.json" in files:
            spans, logs = _legacy_session(
                _read_json(zf, files["spans.json"], []), _read_json(zf, files.get("logs.json"), []), metadata
            )
        else:
            continue

        session_id = coerce_key(meta.get("session_id")) or coerce_key(directory.rsplit("/", 1)[-1]) or "bundle"
        session = manager.load_session(
            session_id,
            spans,
            logs,
            eval_set_id=coerce_key(meta.get("eval_set_id")),
            metadata=metadata,
        )
        if session is None:
            raise HTTPException(status_code=503, detail="Live store is at capacity")
        loaded.append(session.session_id)
        logger.info("Loaded session from bug report: %s", session.session_id)

    return StandardResponse(data=DebugLoadData(loaded_sessions=loaded, count=len(loaded)))
