"""OTLP gRPC receiver services for traces and logs.

Receives standard OTLP/gRPC Export requests on port 4317 and forwards them
into the same StreamingTraceManager pipeline used by OTLP/HTTP routes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

from .otlp_processing import fix_protobuf_id_fields, process_logs, process_traces

if TYPE_CHECKING:
    from grpc import aio

    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

GRPC_SHUTDOWN_GRACE_SECONDS = 5
DEFAULT_GRPC_MAX_CONCURRENT_RPCS = 32
DEFAULT_GRPC_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class OtlpTraceService(trace_service_pb2_grpc.TraceServiceServicer):
    """OTLP TraceService gRPC implementation."""

    def __init__(self, manager: StreamingTraceManager):
        self._manager = manager

    async def Export(self, request, context):  # noqa: N802 (gRPC method name)
        body = MessageToDict(request, preserving_proto_field_name=False)
        fix_protobuf_id_fields(body)
        await process_traces(body, self._manager)
        return trace_service_pb2.ExportTraceServiceResponse()


class OtlpLogsService(logs_service_pb2_grpc.LogsServiceServicer):
    """OTLP LogsService gRPC implementation."""

    def __init__(self, manager: StreamingTraceManager):
        self._manager = manager

    async def Export(self, request, context):  # noqa: N802 (gRPC method name)
        body = MessageToDict(request, preserving_proto_field_name=False)
        fix_protobuf_id_fields(body)
        await process_logs(body, self._manager)
        return logs_service_pb2.ExportLogsServiceResponse()


def create_otlp_grpc_server(
    host: str,
    port: int,
    manager: StreamingTraceManager,
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
