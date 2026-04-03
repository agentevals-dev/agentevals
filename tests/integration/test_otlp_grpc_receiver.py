"""Integration tests for OTLP gRPC receiver."""

from __future__ import annotations

import socket

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from agentevals.api.otlp_grpc import create_otlp_grpc_server

from .conftest import wait_for_session_complete

pytestmark = pytest.mark.integration


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _create_server_with_retry(trace_manager, attempts: int = 20):
    last_error: RuntimeError | None = None
    for _ in range(attempts):
        port = _find_free_port()
        try:
            server = create_otlp_grpc_server(
                host="localhost",
                port=port,
                manager=trace_manager,
            )
            return port, server
        except RuntimeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


class TestOtlpGrpcReceiver:
    async def test_grpc_trace_and_logs_create_session(self, trace_manager):
        grpc = pytest.importorskip("grpc")

        port, server = _create_server_with_retry(trace_manager)
        session_name = "grpc-integration"

        trace_id_hex = "0102030405060708090a0b0c0d0e0f10"
        span_id_hex = "1112131415161718"
        trace_id = bytes.fromhex(trace_id_hex)
        span_id = bytes.fromhex(span_id_hex)

        await server.start()

        try:
            async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
                trace_stub = trace_service_pb2_grpc.TraceServiceStub(channel)
                logs_stub = logs_service_pb2_grpc.LogsServiceStub(channel)

                trace_req = ExportTraceServiceRequest(
                    resource_spans=[
                        ResourceSpans(
                            resource=Resource(
                                attributes=[
                                    KeyValue(
                                        key="agentevals.session_name",
                                        value=AnyValue(string_value=session_name),
                                    ),
                                ]
                            ),
                            scope_spans=[
                                ScopeSpans(
                                    scope=InstrumentationScope(name="grpc-test", version="0.1"),
                                    spans=[
                                        Span(
                                            trace_id=trace_id,
                                            span_id=span_id,
                                            name="grpc-root",
                                            kind=Span.SPAN_KIND_CLIENT,
                                            start_time_unix_nano=1_000_000_000,
                                            end_time_unix_nano=2_000_000_000,
                                        )
                                    ],
                                )
                            ],
                        )
                    ]
                )
                await trace_stub.Export(trace_req)

                logs_req = ExportLogsServiceRequest(
                    resource_logs=[
                        ResourceLogs(
                            scope_logs=[
                                ScopeLogs(
                                    log_records=[
                                        LogRecord(
                                            event_name="gen_ai.user.message",
                                            observed_time_unix_nano=1_500_000_000,
                                            trace_id=trace_id,
                                            span_id=span_id,
                                            body=AnyValue(string_value='{"content":"hello over grpc"}'),
                                        )
                                    ]
                                )
                            ]
                        )
                    ]
                )
                await logs_stub.Export(logs_req)

            await wait_for_session_complete(trace_manager, session_name, timeout=2.0)
            session = trace_manager.sessions[session_name]

            assert session.is_complete
            assert session.source == "otlp"
            assert session.trace_ids == {trace_id_hex}
            assert len(session.spans) == 1
            assert len(session.logs) >= 1
            assert session.logs[0]["event_name"] == "gen_ai.user.message"
        finally:
            await server.stop(grace=1)
