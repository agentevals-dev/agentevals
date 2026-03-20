"""FastAPI application for agentevals REST API."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agentevals import __version__

from ..utils.log_buffer import log_buffer
from .debug_routes import debug_router
from .debug_routes import set_trace_manager as set_debug_trace_manager
from .routes import router

try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_level_str = os.getenv("AGENTEVALS_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s:%(name)s:%(message)s",
        force=True,
    )
    ae_logger = logging.getLogger("agentevals")
    ae_logger.setLevel(log_level)
    if log_buffer not in ae_logger.handlers:
        log_buffer.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        ae_logger.addHandler(log_buffer)
    if _trace_manager:
        _trace_manager.start_cleanup_task()
    yield
    if _trace_manager:
        await _trace_manager.shutdown()
    ae_logger.removeHandler(log_buffer)


app = FastAPI(
    title="agentevals API",
    version=__version__,
    description="REST API for evaluating agent traces using ADK's scoring framework",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(debug_router, prefix="/api/debug")

_live_mode = os.getenv("AGENTEVALS_LIVE") == "1"
_trace_manager = None

if _live_mode:
    from fastapi import WebSocket

    from ..streaming.ws_server import StreamingTraceManager
    from .streaming_routes import set_trace_manager, streaming_router

    app.include_router(streaming_router, prefix="/api/streaming")
    _trace_manager = StreamingTraceManager()
    set_trace_manager(_trace_manager)
    set_debug_trace_manager(_trace_manager)

    @app.websocket("/ws/traces")
    async def websocket_endpoint(websocket: WebSocket):
        await _trace_manager.handle_connection(websocket)

    @app.get("/stream/ui-updates")
    async def ui_updates_stream():
        async def event_generator():
            queue = _trace_manager.register_sse_client()
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                _trace_manager.unregister_sse_client(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )


def get_trace_manager():
    return _trace_manager


_static_dir = Path(__file__).parent.parent / "_static"
_has_ui = _static_dir.is_dir() and (_static_dir / "index.html").exists()

if _has_ui and not os.getenv("AGENTEVALS_HEADLESS"):
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="ui-assets")

    @app.get("/")
    async def root():
        return FileResponse(_static_dir / "index.html")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        file_path = _static_dir / path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(_static_dir / "index.html")
