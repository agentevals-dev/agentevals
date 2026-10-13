"""The API's native gRPC server: the v1alpha1 services, ``grpc.health.v1``, optional reflection and
opt in tracing with the upstream gRPC instrumentation.

A browser cannot speak native gRPC (no HTTP/2 trailers from ``fetch``), so this listener is out of
reach of web pages. Every caller that can reach it is trusted: expose it only to trusted callers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ...telemetry import api_tracer_provider, env_flag
from ..otlp_grpc import listen_address
from .policy import check_policies
from .services import ApiContext, Ports, servicers

logger = logging.getLogger(__name__)

REFLECTION_ENV = "AGENTEVALS_GRPC_REFLECTION"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_CONCURRENT_RPCS = 64
SHUTDOWN_GRACE_SECONDS = 5

SERVER_OPTIONS = [
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    # Watch streams stay open for hours; pings keep idle connections through load balancers.
    ("grpc.keepalive_time_ms", 60_000),
    ("grpc.keepalive_timeout_ms", 20_000),
    ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
]


@dataclass(frozen=True, slots=True)
class ApiGrpcSettings:
    host: str
    port: int
    ports: Ports


@dataclass(slots=True)
class ApiServer:
    server: grpc.aio.Server
    health: health.aio.HealthServicer
    tracer_provider: Any | None
    port: int


async def start_api_server(context: ApiContext, host: str, port: int) -> ApiServer:
    tracer_provider = api_tracer_provider()
    interceptors = []
    if tracer_provider is not None:
        from opentelemetry.instrumentation.grpc import aio_server_interceptor, filters

        noise = filters.any_of(filters.health_check(), filters.service_prefix("grpc.reflection."))
        interceptors.append(aio_server_interceptor(tracer_provider=tracer_provider, filter_=filters.negate(noise)))

    server = grpc.aio.server(
        interceptors=interceptors, options=SERVER_OPTIONS, maximum_concurrent_rpcs=MAX_CONCURRENT_RPCS
    )
    names = []
    for servicer, register, service in servicers(context):
        check_policies(servicer, service)
        register(servicer, server)
        names.append(service.full_name)

    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    for name in ["", *names]:
        await health_servicer.set(name, health_pb2.HealthCheckResponse.SERVING)

    if env_flag(REFLECTION_ENV):
        from grpc_reflection.v1alpha import reflection

        reflection.enable_server_reflection([*names, health.SERVICE_NAME, reflection.SERVICE_NAME], server)

    address = listen_address(host, port)
    bound = server.add_insecure_port(address)
    if bound == 0:
        raise RuntimeError(f"Failed to bind the API gRPC server to {address}")
    await server.start()
    logger.info(
        "API gRPC server on %s (reflection %s, tracing %s)",
        listen_address(host, bound),
        "on" if env_flag(REFLECTION_ENV) else "off",
        "on" if tracer_provider is not None else "off",
    )
    return ApiServer(server=server, health=health_servicer, tracer_provider=tracer_provider, port=bound)


async def stop_api_server(api: ApiServer) -> None:
    await api.health.enter_graceful_shutdown()
    await api.server.stop(grace=SHUTDOWN_GRACE_SECONDS)
    if api.tracer_provider is not None:
        api.tracer_provider.shutdown()
