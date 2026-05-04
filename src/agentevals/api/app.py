"""FastAPI application for agentevals REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agentevals import __version__

from ..run.service import RunService
from ..run.worker import AsyncRunWorker
from ..storage import StorageSettings, build_repos
from ..storage.postgres.migrator import Migrator
from ..utils.log_buffer import log_buffer
from .debug_routes import debug_router
from .routes import router
from .runs_routes import runs_router

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass


def _build_lifespan():
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
        mgr = getattr(app.state, "trace_manager", None)
        if mgr:
            mgr.start_cleanup_task()

        storage_settings: StorageSettings | None = None
        worker: AsyncRunWorker | None = None
        try:
            storage_settings = StorageSettings.from_env()
        except Exception as exc:
            logger.error("Storage configuration invalid; /api/runs will not be available: %s", exc)

        if storage_settings is not None and storage_settings.backend == "postgres":
            logger.info("Applying any pending migrations to schema '%s'", storage_settings.schema_name)
            migrator = Migrator(
                dsn=storage_settings.database_url or "",
                schema=storage_settings.schema_name,
                lock_timeout_s=storage_settings.migrate_lock_timeout_s,
            )
            await migrator.up()

            repos = await build_repos(storage_settings)
            app.state.storage_settings = storage_settings
            app.state.repos = repos
            app.state.run_service = RunService(repos.runs, repos.results)

            worker = AsyncRunWorker(runs=repos.runs, results=repos.results, settings=storage_settings)
            await worker.start()
            app.state.run_worker = worker

        yield

        if worker is not None:
            await worker.stop()
        repos = getattr(app.state, "repos", None)
        if repos is not None:
            await repos.close()
        if mgr:
            await mgr.shutdown()
        ae_logger.removeHandler(log_buffer)

    return lifespan


def create_app(
    *,
    trace_manager: StreamingTraceManager | None = None,
    enable_streaming: bool = False,
) -> FastAPI:
    """Create the main agentevals API app."""
    app = FastAPI(
        title="agentevals API",
        version=__version__,
        description="REST API for evaluating agent traces using ADK's scoring framework",
        lifespan=_build_lifespan(),
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
    app.include_router(runs_router, prefix="/api")

    if trace_manager is not None:
        app.state.trace_manager = trace_manager

    if enable_streaming:
        if trace_manager is None:
            raise ValueError("enable_streaming requires a trace_manager")

        from .streaming_routes import streaming_router

        app.include_router(streaming_router, prefix="/api/streaming")

        @app.websocket("/ws/traces")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.app.state.trace_manager.handle_connection(websocket)

        @app.get("/stream/ui-updates")
        async def ui_updates_stream(request: Request):
            mgr = request.app.state.trace_manager

            async def event_generator():
                queue = mgr.register_sse_client()
                try:
                    while True:
                        event = await queue.get()
                        if event is None:
                            break
                        yield f"data: {json.dumps(event)}\n\n"
                except asyncio.CancelledError:
                    pass
                finally:
                    mgr.unregister_sse_client(queue)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

    static_dir = Path(__file__).parent.parent / "_static"
    has_ui = static_dir.is_dir() and (static_dir / "index.html").exists()

    if has_ui and not os.getenv("AGENTEVALS_HEADLESS"):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="ui-assets")

        @app.get("/")
        async def root():
            return FileResponse(static_dir / "index.html")

        @app.get("/{path:path}")
        async def spa_fallback(path: str):
            file_path = static_dir / path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
