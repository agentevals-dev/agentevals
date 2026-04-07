"""FastAPI dependency functions for shared services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager


def get_trace_manager(request: Request) -> StreamingTraceManager | None:
    """Return the StreamingTraceManager or None if live mode is off."""
    return getattr(request.app.state, "trace_manager", None)


def get_trace_manager_from_app(app: Any) -> StreamingTraceManager | None:
    """Return the StreamingTraceManager from an app object or None."""
    return getattr(app.state, "trace_manager", None)


def require_trace_manager(request: Request) -> StreamingTraceManager:
    """Return the StreamingTraceManager, raising 503 if live mode is off."""
    mgr = get_trace_manager_from_app(request.app)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Live mode not enabled")
    return mgr
