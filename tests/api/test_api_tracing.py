"""Opt in traces of the API server, through the upstream gRPC instrumentation."""

from __future__ import annotations

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from agentevals.api.otlp_processing import decode_traces_protobuf, ingest_traces
from agentevals.api.rpc.server import start_api_server, stop_api_server
from agentevals.api.rpc.services import ApiContext
from agentevals.api.v1alpha1 import system_pb2, system_pb2_grpc
from agentevals.otel.identity import GRPC_INSTRUMENTATION_SCOPE
from agentevals.otel.store import DROPPED_OWN_SPANS
from agentevals.streaming.manager import LiveManager
from agentevals.telemetry import api_tracer_provider, api_traces_enabled


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("AGENTEVALS_API_TRACES", "OTEL_SDK_DISABLED", "OTEL_TRACES_EXPORTER", "AGENTEVALS_GRPC_REFLECTION"):
        monkeypatch.delenv(name, raising=False)


class TestSwitches:
    @pytest.mark.parametrize(
        ("env", "enabled"),
        [
            ({}, False),
            ({"AGENTEVALS_API_TRACES": "true"}, True),
            ({"AGENTEVALS_API_TRACES": "false"}, False),
            ({"AGENTEVALS_API_TRACES": "true", "OTEL_SDK_DISABLED": "true"}, False),
            ({"AGENTEVALS_API_TRACES": "true", "OTEL_TRACES_EXPORTER": "none"}, False),
            ({"AGENTEVALS_API_TRACES": "true", "OTEL_TRACES_EXPORTER": "console"}, True),
        ],
    )
    def test_enabled(self, monkeypatch, env, enabled):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert api_traces_enabled() is enabled

    def test_an_unrecognized_value_is_an_error(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_API_TRACES", "sometimes")
        with pytest.raises(ValueError, match="AGENTEVALS_API_TRACES"):
            api_traces_enabled()

    def test_no_provider_when_off(self):
        assert api_tracer_provider() is None

    def test_a_dedicated_provider_when_on(self, monkeypatch):
        from opentelemetry import trace

        monkeypatch.setenv("AGENTEVALS_API_TRACES", "true")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
        provider = api_tracer_provider()
        try:
            assert isinstance(provider, TracerProvider)
            assert provider is not trace.get_tracer_provider()
            assert provider.resource.attributes["service.name"] == "agentevals"
        finally:
            provider.shutdown()


async def test_server_spans_are_recorded_and_never_become_sessions(monkeypatch):
    exporter = InMemorySpanExporter()
    # A session name on the resource would open a session for any span the store kept.
    provider = TracerProvider(resource=Resource.create({"agentevals.session_name": "api-self"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("agentevals.api.rpc.server.api_tracer_provider", lambda: provider)

    manager = LiveManager()
    api = await start_api_server(ApiContext(manager=manager), "127.0.0.1", 0)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{api.port}") as channel:
            await system_pb2_grpc.SystemServiceStub(channel).GetVersion(system_pb2.GetVersionRequest())
            await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
    finally:
        await stop_api_server(api)

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["/agentevals.api.v1alpha1.SystemService/GetVersion"]
    [own] = spans
    assert own.kind == SpanKind.SERVER
    assert own.instrumentation_scope.name == GRPC_INSTRUMENTATION_SCOPE

    result = await ingest_traces(decode_traces_protobuf(encode_spans(spans).SerializeToString()), manager)
    assert (result.accepted, result.rejected) == (1, 0)
    assert manager.store.counters[DROPPED_OWN_SPANS] == 1
    assert manager.sessions == {}
