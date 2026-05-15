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
from ..run.sinks import log_registered_sinks
from ..run.worker import AsyncRunWorker
from ..storage import StorageSettings, build_repos
from ..storage.postgres.migrator import Migrator, discover_migrations
from ..utils.log_buffer import log_buffer
from .debug_routes import debug_router
from .routes import router
from .runs_routes import runs_router

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    val = raw.strip().lower()
    if val in _TRUE_VALUES:
        return True
    if val in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of true/false/1/0/yes/no/on/off (got: {raw!r})")


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
            migrator = Migrator(
                dsn=storage_settings.database_url or "",
                schema=storage_settings.schema_name,
                lock_timeout_s=storage_settings.migrate_lock_timeout_s,
            )
            if _env_bool("AGENTEVALS_AUTO_MIGRATE", default=True):
                logger.info("Applying any pending migrations to schema '%s'", storage_settings.schema_name)
                await migrator.up()
            else:
                logger.info(
                    "AGENTEVALS_AUTO_MIGRATE is disabled; verifying schema '%s' is up to date",
                    storage_settings.schema_name,
                )
                status = await migrator.status()
                if status.dirty:
                    raise RuntimeError(
                        f"schema_migrations is dirty at version {status.version}. "
                        "Resolve manually and run 'agentevals migrate force <version>', "
                        "or set AGENTEVALS_AUTO_MIGRATE=true to retry on startup."
                    )
                current = status.version
                pending = [m.version for m in discover_migrations() if current is None or m.version > current]
                if pending:
                    raise RuntimeError(
                        f"Database schema is behind: pending migrations {pending}. "
                        "Run 'agentevals migrate up' to apply them, "
                        "or set AGENTEVALS_AUTO_MIGRATE=true to apply on startup."
                    )

            repos = await build_repos(storage_settings)
            app.state.storage_settings = storage_settings
            app.state.repos = repos
            app.state.run_service = RunService(repos.runs, repos.results)

            worker = AsyncRunWorker(runs=repos.runs, results=repos.results, settings=storage_settings)
            await worker.start()
            app.state.run_worker = worker
            log_registered_sinks()

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
        openapi_tags=[
            {
                "name": "runs",
                "description": (
                    "**Preview.** Async run pipeline backed by Postgres. The shape of "
                    "submission, responses, and persisted run / result data may change "
                    "incompatibly in upcoming releases. Operators evaluating this "
                    "surface should plan to recreate persisted data when upgrading "
                    "agentevals between minor versions."
                ),
            },
        ],
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
