"""agentevals' own telemetry: identity, the env switches, and the opt in API server traces.

agentevals is a telemetry sink, so it exports nothing of its own unless asked to. Each signal it
emits has a dedicated provider, never the global one, so an application that embeds agentevals
keeps its own providers. Exporters read only the standard ``OTEL_EXPORTER_OTLP_*`` variables.

API server traces come from the upstream gRPC instrumentation. They are on with
``AGENTEVALS_API_TRACES=true``; ``OTEL_SDK_DISABLED=true`` or ``OTEL_TRACES_EXPORTER=none`` win.
``OTEL_TRACES_EXPORTER=console`` prints them instead of exporting. The live store drops these spans
(``genai.routing``), so pointing the exporter at agentevals itself does not create sessions.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

API_TRACES_ENV = "AGENTEVALS_API_TRACES"

PROCESS_INSTANCE_ID = str(uuid.uuid4())

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_true(name: str) -> bool:
    """Lenient read for standard OTel switches, which other tools also set."""
    return os.environ.get(name, "").strip().lower() in _TRUE


def env_flag(name: str) -> bool | None:
    """Strict read for agentevals switches: ``None`` when unset, ``ValueError`` when unrecognized."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be one of true/false/1/0/yes/no/on/off (got: {raw!r})")


def package_version() -> str:
    from . import __version__

    return __version__


def build_resource() -> Any:
    """Service identity as one unit; ``OTEL_RESOURCE_ATTRIBUTES`` and ``OTEL_SERVICE_NAME`` win."""
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    ours = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or "agentevals",
            "service.version": package_version(),
            "service.instance.id": PROCESS_INSTANCE_ID,
        }
    )
    return ours.merge(OTELResourceDetector().detect())


def otlp_protocol(signal: str) -> str:
    """``http/protobuf`` or ``grpc``, from ``OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL``, then the general one."""
    raw = (
        os.environ.get(f"OTEL_EXPORTER_OTLP_{signal}_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or ""
    ).strip()
    if raw in ("", "http/protobuf"):
        return "http/protobuf"
    if raw == "grpc":
        return "grpc"
    logger.warning("OTLP protocol %r is not supported for %s; using http/protobuf", raw, signal.lower())
    return "http/protobuf"


def _traces_exporter_setting() -> str:
    return os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower()


def api_traces_enabled() -> bool:
    if env_true("OTEL_SDK_DISABLED") or _traces_exporter_setting() == "none":
        return False
    return bool(env_flag(API_TRACES_ENV))


def api_tracer_provider() -> Any | None:
    """A dedicated provider for the API server's spans, or ``None`` when they are off."""
    if not api_traces_enabled():
        return None
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    if _traces_exporter_setting() == "console":
        exporter = ConsoleSpanExporter()
    elif otlp_protocol("TRACES") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    provider = TracerProvider(resource=build_resource())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider
