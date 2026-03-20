from __future__ import annotations

import glob
import importlib.metadata
import io
import json
import logging
import os
import platform
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi import File as FastAPIFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentevals import __version__

from ..utils.log_buffer import log_buffer
from .models import DebugLoadData, SessionInfo, StandardResponse, WSSessionCompleteEvent, WSSessionStartedEvent

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

debug_router = APIRouter()

_trace_manager: StreamingTraceManager | None = None


def set_trace_manager(manager: StreamingTraceManager) -> None:
    global _trace_manager
    _trace_manager = manager


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


def _collect_sessions() -> list[dict]:
    if not _trace_manager:
        return []

    sessions_data = []
    for session in _trace_manager.sessions.values():
        sessions_data.append(
            {
                "session_id": session.session_id,
                "trace_id": session.trace_id,
                "eval_set_id": session.eval_set_id,
                "started_at": session.started_at.isoformat(),
                "is_complete": session.is_complete,
                "span_count": len(session.spans),
                "log_count": len(session.logs),
                "metadata": session.metadata,
                "spans": session.spans,
                "logs": session.logs,
            }
        )
    return sessions_data


def _collect_temp_files(session_ids: set[str] | None = None) -> dict[str, str]:
    """Collect temp files, filtering JSONL files to current sessions only."""
    tmp_dir = tempfile.gettempdir()
    files = {}
    for pattern in ["agentevals_*.jsonl", "eval_set_*.json"]:
        for path in glob.glob(os.path.join(tmp_dir, pattern)):
            basename = os.path.basename(path)
            # Filter JSONL files to only include current sessions
            if session_ids is not None and basename.endswith(".jsonl"):
                # Extract session ID from filename: agentevals_{session_id}.jsonl
                sid = basename.removeprefix("agentevals_").removesuffix(".jsonl")
                if sid not in session_ids:
                    continue
            try:
                with open(path) as f:
                    files[basename] = f.read()
            except OSError:
                logger.debug("Could not read temp file %s", path)
    return files


@debug_router.post("/bundle")
async def create_debug_bundle(diagnostics: FrontendDiagnostics):
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

        sessions = _collect_sessions()
        for s in sessions:
            sid = s["session_id"]
            zf.writestr(
                f"{prefix}/sessions/{sid}/spans.json",
                json.dumps(s["spans"], indent=2),
            )
            zf.writestr(
                f"{prefix}/sessions/{sid}/logs.json",
                json.dumps(s["logs"], indent=2),
            )
            session_meta = {k: v for k, v in s.items() if k not in ("spans", "logs")}
            zf.writestr(
                f"{prefix}/sessions/{sid}/session_meta.json",
                json.dumps(session_meta, indent=2),
            )

        zf.writestr(f"{prefix}/backend_logs.txt", log_buffer.get_text())

        current_session_ids = {s["session_id"] for s in sessions}
        temp_files = _collect_temp_files(session_ids=current_session_ids)
        for filename, content in temp_files.items():
            zf.writestr(f"{prefix}/temp_files/{filename}", content)

        zf.writestr(
            f"{prefix}/frontend_state.json",
            json.dumps(diagnostics.app_state, indent=2),
        )
        zf.writestr(
            f"{prefix}/console_logs.json",
            json.dumps(diagnostics.console_logs, indent=2),
        )
        zf.writestr(
            f"{prefix}/network_errors.json",
            json.dumps(diagnostics.network_errors, indent=2),
        )

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="bug-report-{timestamp}.zip"'},
    )


@debug_router.post("/load", response_model=StandardResponse[DebugLoadData])
async def load_debug_bundle(file: UploadFile = FastAPIFile(...)):
    if not _trace_manager:
        raise HTTPException(
            status_code=400,
            detail="Live mode is not enabled. Start with: agentevals serve --dev",
        )

    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid ZIP file") from exc

    session_dirs: dict[str, list[str]] = {}
    for name in zf.namelist():
        parts = name.split("/")
        if len(parts) >= 4 and parts[-3] == "sessions":
            sid = parts[-2]
            session_dirs.setdefault(sid, []).append(name)

    if not session_dirs:
        raise HTTPException(status_code=400, detail="No sessions found in ZIP")

    from ..streaming.session import TraceSession

    loaded = []
    for sid, files in session_dirs.items():
        meta_file = next((f for f in files if f.endswith("session_meta.json")), None)
        spans_file = next((f for f in files if f.endswith("spans.json")), None)
        logs_file = next((f for f in files if f.endswith("logs.json")), None)

        if not spans_file:
            continue

        meta = json.loads(zf.read(meta_file)) if meta_file else {}
        spans = json.loads(zf.read(spans_file))
        logs = json.loads(zf.read(logs_file)) if logs_file else []

        session = TraceSession(
            session_id=meta.get("session_id", sid),
            trace_id=meta.get("trace_id", sid),
            eval_set_id=meta.get("eval_set_id"),
            spans=spans,
            logs=logs,
            is_complete=True,
            metadata=meta.get("metadata", {}),
        )

        _trace_manager.sessions[session.session_id] = session

        await _trace_manager.broadcast_to_ui(
            WSSessionStartedEvent(
                session=SessionInfo(
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                    eval_set_id=session.eval_set_id,
                    span_count=len(session.spans),
                    is_complete=False,
                    started_at=session.started_at.isoformat(),
                    metadata=session.metadata,
                ),
            ).model_dump(by_alias=True)
        )

        invocations_data = await _trace_manager._extract_invocations(session)
        await _trace_manager._save_spans_to_temp_file(session)

        await _trace_manager.broadcast_to_ui(
            WSSessionCompleteEvent(
                session_id=session.session_id,
                invocations=invocations_data,
            ).model_dump(by_alias=True)
        )

        loaded.append(session.session_id)
        logger.info("Loaded session from bug report: %s", session.session_id)

    return StandardResponse(data=DebugLoadData(loaded_sessions=loaded, count=len(loaded)))
