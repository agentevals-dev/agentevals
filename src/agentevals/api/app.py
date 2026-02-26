"""FastAPI application for agentevals REST API."""

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .routes import router
from .streaming_routes import streaming_router
from ..streaming.ws_server import StreamingTraceManager

# Load environment variables from .env file if it exists
# This makes GOOGLE_API_KEY available for LLM-based evaluators
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

app = FastAPI(
    title="agentevals API",
    version="0.1.0",
    description="REST API for evaluating agent traces using ADK's scoring framework",
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
app.include_router(streaming_router, prefix="/api/streaming")

trace_manager = StreamingTraceManager()

from .streaming_routes import set_trace_manager
set_trace_manager(trace_manager)


@app.on_event("startup")
async def configure_logging():
    """Configure logging and start background tasks."""
    logging.getLogger("agentevals").setLevel(logging.INFO)
    trace_manager.start_cleanup_task()


@app.on_event("shutdown")
async def shutdown_cleanup():
    """Stop background tasks on shutdown."""
    await trace_manager.stop_cleanup_task()


@app.websocket("/ws/traces")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for agents to stream OTel spans."""
    await trace_manager.handle_connection(websocket)


@app.get("/stream/ui-updates")
async def ui_updates_stream():
    """SSE endpoint for UI to receive real-time updates."""

    async def event_generator():
        queue = trace_manager.register_sse_client()

        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            trace_manager.unregister_sse_client(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
