"""Trace loader implementations.

Most callers should use :func:`load_traces` from
:mod:`agentevals.loader.auto`, which auto-detects the on-disk format
(Jaeger or OTLP, including Tempo's ``batches`` / wrapper variants) and
dispatches to the right underlying loader.
"""

from .auto import (
    JAEGER_JSON,
    OTLP_JSON,
    detect_format,
    get_loader_for_format,
    load_traces,
)
from .base import TraceLoader
from .jaeger import JaegerJsonLoader
from .otlp import OtlpJsonLoader

__all__ = [
    "JAEGER_JSON",
    "OTLP_JSON",
    "JaegerJsonLoader",
    "OtlpJsonLoader",
    "TraceLoader",
    "detect_format",
    "get_loader_for_format",
    "load_traces",
]
