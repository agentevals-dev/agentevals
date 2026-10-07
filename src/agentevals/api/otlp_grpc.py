"""OTLP gRPC receiver services for traces and logs.

Receives standard OTLP/gRPC Export requests on port 4317 and forwards them
into the same live store pipeline used by the OTLP/HTTP routes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

from ..otel.decode import DecodeResult, decode_logs_proto, decode_traces_proto
from .otlp_http import PARSE_THREAD_THRESHOLD
from .otlp_processing import (
    build_logs_response,
    build_traces_response,
    ingest_logs,
    ingest_traces,
)

if TYPE_CHECKING:
    from grpc import aio

    from ..streaming.manager import LiveManager

logger = logging.getLogger(__name__)

GRPC_SHUTDOWN_GRACE_SECONDS = 5
DEFAULT_GRPC_MAX_CONCURRENT_RPCS = 32
DEFAULT_GRPC_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


async def _decode(request: Any, decode: Callable[[Any], DecodeResult]) -> DecodeResult:
    if request.ByteSize() < PARSE_THREAD_THRESHOLD:
        return decode(request)
    return await asyncio.to_thread(decode, request)


async def _abort_overloaded(context: Any, signal: str) -> None:
    import grpc

    await context.abort(grpc.StatusCode.UNAVAILABLE, f"Live store is at capacity; {signal} not accepted, retry later")


class OtlpTraceService(trace_service_pb2_grpc.TraceServiceServicer):
    """OTLP TraceService gRPC implementation."""

    def __init__(self, manager: LiveManager):
        self._manager = manager

    async def Export(self, request, context):  # noqa: N802 (gRPC method name)
        result = await ingest_traces(await _decode(request, decode_traces_proto), self._manager)
        if result.overloaded:
            await _abort_overloaded(context, "spans")
        return build_traces_response(result)


class OtlpLogsService(logs_service_pb2_grpc.LogsServiceServicer):
    """OTLP LogsService gRPC implementation."""

    def __init__(self, manager: LiveManager):
        self._manager = manager

    async def Export(self, request, context):  # noqa: N802 (gRPC method name)
        result = await ingest_logs(await _decode(request, decode_logs_proto), self._manager)
        if result.overloaded:
            await _abort_overloaded(context, "log records")
        return build_logs_response(result)


def create_otlp_grpc_server(
    host: str,
    port: int,
    manager: LiveManager,
    *,
    max_concurrent_rpcs: int = DEFAULT_GRPC_MAX_CONCURRENT_RPCS,
    max_message_bytes: int = DEFAULT_GRPC_MAX_MESSAGE_BYTES,
) -> aio.Server:
    """Create an OTLP gRPC server bound to host:port."""
    try:
        import grpc
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("OTLP gRPC receiver requires grpcio. Install with: pip install grpcio") from exc

    server = grpc.aio.server(
        compression=grpc.Compression.Gzip,
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        options=[
            ("grpc.max_receive_message_length", max_message_bytes),
            ("grpc.max_send_message_length", max_message_bytes),
        ],
    )
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(OtlpTraceService(manager), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(OtlpLogsService(manager), server)

    listen_addr = f"{host}:{port}"
    bound_port = server.add_insecure_port(listen_addr)
    if bound_port == 0:
        raise RuntimeError(f"Failed to bind OTLP gRPC receiver to {listen_addr}")

    logger.info(
        "OTLP gRPC receiver configured at %s (gzip enabled, max_concurrent_rpcs=%d, max_msg=%d)",
        listen_addr,
        max_concurrent_rpcs,
        max_message_bytes,
    )
    return server


async def stop_otlp_grpc_server(server: aio.Server, *, force: bool = False) -> None:
    """Stop the OTLP gRPC server with graceful or forced semantics."""
    grace = 0 if force else GRPC_SHUTDOWN_GRACE_SECONDS
    await server.stop(grace=grace)
