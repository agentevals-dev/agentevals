"""Minimal FastAPI app for the OTLP HTTP receiver on port 4318.

Mounts only the /v1/traces and /v1/logs endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from .otlp_http import register_otlp_exception_handlers
from .otlp_routes import otlp_router

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager


def create_otlp_app(*, trace_manager: StreamingTraceManager | None = None) -> FastAPI:
    """Create the OTLP HTTP receiver app."""
    app = FastAPI(title="agentevals OTLP receiver")
    if trace_manager is not None:
        app.state.trace_manager = trace_manager
    app.include_router(otlp_router)
    # Scoped to this app: `require_trace_manager` is shared with the main API,
    # which must keep its own error body shape.
    register_otlp_exception_handlers(app)
    return app


otlp_app = create_otlp_app()
