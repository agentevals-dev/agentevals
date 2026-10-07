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
from .otlp_http import build_export_response, overload_error, read_export_request
from .otlp_processing import (
    build_logs_response,
    build_traces_response,
    decode_logs_json,
    decode_logs_protobuf,
    decode_traces_json,
    decode_traces_protobuf,
    ingest_logs,
    ingest_traces,
)

if TYPE_CHECKING:
    from ..streaming.manager import LiveManager

otlp_router = APIRouter()


@otlp_router.post("/v1/traces")
async def receive_traces(
    request: Request,
    manager: LiveManager = Depends(require_trace_manager),
) -> Response:
    """OTLP HTTP trace receiver (ExportTraceServiceRequest)."""
    decoded, media_type = await read_export_request(
        request, decode_traces_json, decode_traces_protobuf, "resourceSpans"
    )
    result = await ingest_traces(decoded, manager)
    if result.overloaded:
        raise overload_error("spans")
    return build_export_response(build_traces_response(result), media_type)


@otlp_router.post("/v1/logs")
async def receive_logs(
    request: Request,
    manager: LiveManager = Depends(require_trace_manager),
) -> Response:
    """OTLP HTTP log receiver (ExportLogsServiceRequest)."""
    decoded, media_type = await read_export_request(request, decode_logs_json, decode_logs_protobuf, "resourceLogs")
    result = await ingest_logs(decoded, manager)
    if result.overloaded:
        raise overload_error("log records")
    return build_export_response(build_logs_response(result), media_type)
