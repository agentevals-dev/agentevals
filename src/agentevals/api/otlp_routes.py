"""OTLP HTTP routes for /v1/traces and /v1/logs.

Route handlers are intentionally thin and delegate decode/process logic to
`otlp_processing.py` so protocol handling can be reused by gRPC receivers and
tested independently from HTTP routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response

from .dependencies import require_trace_manager
from .otlp_processing import (
    decode_protobuf_logs,
    decode_protobuf_traces,
    process_logs,
    process_traces,
)

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

otlp_router = APIRouter()


@otlp_router.post("/v1/traces")
async def receive_traces(
    request: Request,
    manager: StreamingTraceManager = Depends(require_trace_manager),
) -> Response:
    """OTLP HTTP trace receiver (ExportTraceServiceRequest)."""
    content_type = request.headers.get("content-type", "")

    if "application/x-protobuf" in content_type:
        raw = await request.body()
        body = decode_protobuf_traces(raw)
    else:
        body = await request.json()

    await process_traces(body, manager)
    return Response(
        status_code=200,
        content='{"partialSuccess":{}}',
        media_type="application/json",
    )


@otlp_router.post("/v1/logs")
async def receive_logs(
    request: Request,
    manager: StreamingTraceManager = Depends(require_trace_manager),
) -> Response:
    """OTLP HTTP log receiver (ExportLogsServiceRequest)."""
    content_type = request.headers.get("content-type", "")

    if "application/x-protobuf" in content_type:
        raw = await request.body()
        body = decode_protobuf_logs(raw)
    else:
        body = await request.json()

    await process_logs(body, manager)
    return Response(
        status_code=200,
        content='{"partialSuccess":{}}',
        media_type="application/json",
    )
