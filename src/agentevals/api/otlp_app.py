"""Minimal FastAPI app for the OTLP HTTP receiver on port 4318.

Shares the StreamingTraceManager with the main app (port 8001).
Mounts only the /v1/traces and /v1/logs endpoints.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .otlp_routes import otlp_router, set_trace_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .app import get_trace_manager

    mgr = get_trace_manager()
    if mgr:
        set_trace_manager(mgr)
    yield


otlp_app = FastAPI(title="agentevals OTLP receiver", lifespan=lifespan)
otlp_app.include_router(otlp_router)
