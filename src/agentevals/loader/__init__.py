"""Trace loader implementations.

Most callers should use :func:`load_traces` from
:mod:`agentevals.loader.auto`, which auto-detects the on-disk format
(Jaeger or OTLP, including Tempo's ``batches`` / wrapper variants) and
dispatches to the right underlying loader.
"""

from ..otel.model import Span, Trace
from .auto import (
    JAEGER_JSON,
    OTLP_JSON,
    TelemetryFile,
    TraceLoader,
    detect_format,
    get_loader_for_format,
    load_telemetry,
    load_traces,
    load_traces_from_obj,
)
from .jaeger import JaegerJsonLoader
from .otlp import OtlpJsonLoader

__all__ = [
    "TelemetryFile",
    "load_telemetry",
    "JAEGER_JSON",
    "OTLP_JSON",
    "JaegerJsonLoader",
    "OtlpJsonLoader",
    "Span",
    "Trace",
    "TraceLoader",
    "detect_format",
    "get_loader_for_format",
    "load_traces",
    "load_traces_from_obj",
]
