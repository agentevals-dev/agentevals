"""Format detection and the file loading entry points.

* :func:`load_telemetry` reads a file into envelope traces (``agentevals.otel.model``) plus the
  log records that belong to no trace in the file.
* :func:`load_traces` returns just the traces.
* :func:`detect_format` sniffs content, so a ``.jsonl`` file holding Collector exports, bare
  spans or anything else is classified by what it contains.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from ..otel.decode import decode_bare_spans_json, decode_json_document, is_otlp_document
from ..otel.model import LogRecord, Trace, build_traces
from .jaeger import JaegerJsonLoader, decode_jaeger_document
from .otlp import OtlpJsonLoader, decode_otlp_text

logger = logging.getLogger(__name__)

JAEGER_JSON = "jaeger-json"
OTLP_JSON = "otlp-json"


class TraceLoader(Protocol):
    def format_name(self) -> str: ...

    def load(self, source: str) -> list[Trace]: ...


_LOADERS: dict[str, type[TraceLoader]] = {
    JAEGER_JSON: JaegerJsonLoader,
    OTLP_JSON: OtlpJsonLoader,
}


@dataclass
class TelemetryFile:
    format: str
    traces: list[Trace]
    unattributed_logs: list[LogRecord] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)


_UNPARSED = object()


def _looks_like_span(obj: object) -> bool:
    return isinstance(obj, dict) and ("spanId" in obj or "traceId" in obj)


def _classify(content: str) -> tuple[str | None, object]:
    """Return the format and the parsed document (``_UNPARSED`` for JSON lines)."""
    text = content.strip()
    if not text:
        return None, _UNPARSED
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        first = text.split("\n", 1)[0].strip()
        try:
            head = json.loads(first)
        except (ValueError, RecursionError):
            return None, _UNPARSED
        ok = isinstance(head, dict) and (is_otlp_document(head) or _looks_like_span(head))
        return (OTLP_JSON if ok else None), _UNPARSED
    if isinstance(data, dict):
        if is_otlp_document(data) or _looks_like_span(data):
            return OTLP_JSON, data
        if "data" in data:
            return JAEGER_JSON, data
    if isinstance(data, list) and data and all(_looks_like_span(i) for i in data):
        return OTLP_JSON, data
    return None, data


def detect_format(path: str) -> str | None:
    """Return ``"otlp-json"``, ``"jaeger-json"``, or ``None`` when unreadable or unrecognized."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    return _classify(content)[0]


def get_loader_for_format(format_name: str) -> TraceLoader:
    if format_name not in _LOADERS:
        raise ValueError(f"Unknown trace format {format_name!r}. Supported: {sorted(_LOADERS)}")
    return _LOADERS[format_name]()


def load_telemetry(path: str, *, format: str | None = None) -> TelemetryFile:
    """Read a trace file into envelopes. Raises ``ValueError`` when the content is not recognized."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    detected, data = _classify(content)
    fmt = format or detected
    if fmt not in _LOADERS:
        if format is not None:
            raise ValueError(f"Unknown trace format {format!r}. Supported: {sorted(_LOADERS)}")
        raise ValueError(
            f"Could not detect trace format for {path!r}. "
            'Expected Jaeger JSON ({"data": [...]}) or OTLP JSON '
            '({"resourceSpans": [...]} / {"batches": [...]}). '
            "Pass an explicit format= override if needed."
        )
    if data is _UNPARSED:
        if fmt == JAEGER_JSON:
            raise ValueError(f"Invalid Jaeger JSON in {path!r}")
        result = decode_otlp_text(content)
    else:
        result = _decode_obj(data, fmt)
    traces, unattributed = build_traces(result.spans, result.logs)
    if result.reasons:
        logger.warning("Skipped items in %s: %s", path, result.error_message)
    return TelemetryFile(format=fmt, traces=traces, unattributed_logs=unattributed, warnings=result.warnings)


def _decode_obj(data: object, fmt: str):
    if fmt == JAEGER_JSON:
        return decode_jaeger_document(data)
    if isinstance(data, dict) and is_otlp_document(data):
        return decode_json_document(data, strict=False)
    if isinstance(data, list) or _looks_like_span(data):
        return decode_bare_spans_json(data if isinstance(data, list) else [data], strict=False)
    raise ValueError("not an OTLP JSON document: expected resourceSpans, resourceLogs or batches")


def load_traces_from_obj(data: object, *, format: str | None = None) -> list[Trace]:
    """Traces from an already parsed JSON document (Jaeger or OTLP)."""
    if format is not None and format not in _LOADERS:
        raise ValueError(f"Unknown trace format {format!r}. Supported: {sorted(_LOADERS)}")
    fmt = format or (
        JAEGER_JSON if isinstance(data, dict) and "data" in data and not is_otlp_document(data) else OTLP_JSON
    )
    result = _decode_obj(data, fmt)
    traces, _ = build_traces(result.spans, result.logs)
    return traces


def load_traces(path: str, *, format: str | None = None) -> list[Trace]:
    return load_telemetry(path, format=format).traces
