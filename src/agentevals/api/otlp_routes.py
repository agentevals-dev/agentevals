"""OTLP HTTP routes for /v1/traces and /v1/logs.

Route handlers are intentionally thin: `otlp_http.py` owns protocol handling
(content negotiation, compression, `google.rpc.Status` error bodies) and
`otlp_processing.py` owns decoding and ingestion, so both can be reused by the
gRPC receiver and tested independently from HTTP routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response

from .dependencies import require_trace_manager
from .otlp_http import build_export_response, read_export_request
from .otlp_processing import (
    build_logs_response,
    build_traces_response,
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
    body, media_type = await read_export_request(request, decode_protobuf_traces, "resourceSpans")
    result = await process_traces(body, manager)
    return build_export_response(request, build_traces_response(result), media_type)


@otlp_router.post("/v1/logs")
async def receive_logs(
    request: Request,
    manager: StreamingTraceManager = Depends(require_trace_manager),
) -> Response:
    """OTLP HTTP log receiver (ExportLogsServiceRequest)."""
    body, media_type = await read_export_request(request, decode_protobuf_logs, "resourceLogs")
    result = await process_logs(body, manager)
    return build_export_response(request, build_logs_response(result), media_type)
