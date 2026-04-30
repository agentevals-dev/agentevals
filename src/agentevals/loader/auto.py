"""Format auto-detection and unified trace-loading entrypoint.

The public surface of this module is intentionally tiny:

- :func:`load_traces` — load traces from a file. Auto-detects the on-disk
  shape (Jaeger native JSON, OTLP JSON document, OTLP JSONL, Tempo v1
  ``batches``, Tempo v2 ``trace`` wrapper) and dispatches to the right
  underlying loader. Callers don't need to know what tracing system
  exported the file.
- :func:`detect_format` — peek at a file and return its format name, or
  ``None`` if the shape isn't recognized. Useful for surfacing detection
  results to a user without committing to loading.

The format-specific :class:`JaegerJsonLoader` and :class:`OtlpJsonLoader`
classes remain for callers that already know which format they have
(streaming OTLP receivers, in-memory dict ingest, tests).
"""

from __future__ import annotations

import json
import logging

from .base import Trace, TraceLoader
from .jaeger import JaegerJsonLoader
from .otlp import OtlpJsonLoader

logger = logging.getLogger(__name__)

JAEGER_JSON = "jaeger-json"
OTLP_JSON = "otlp-json"

_LOADERS: dict[str, type[TraceLoader]] = {
    JAEGER_JSON: JaegerJsonLoader,
    OTLP_JSON: OtlpJsonLoader,
}


def detect_format(path: str) -> str | None:
    """Return the format name for ``path`` (``"jaeger-json"`` or ``"otlp-json"``).

    Order:
    1. The file must exist and be readable. Missing/unreadable files always
       return ``None`` regardless of extension.
    2. ``.jsonl`` extension implies OTLP (one span per line).
    3. Otherwise read and inspect top-level keys:
       - ``resourceSpans`` / ``batches`` / wrapped ``trace.{...}``  → OTLP
       - ``data``                                                    → Jaeger
    Returns ``None`` when the file is missing, unreadable, not valid JSON,
    or the shape isn't recognized; callers that still want to attempt a
    load can fall back to a default.
    """
    try:
        with open(path) as f:
            if path.lower().endswith(".jsonl"):
                return OTLP_JSON
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    if "resourceSpans" in data or "batches" in data:
        return OTLP_JSON

    inner = data.get("trace")
    if isinstance(inner, dict) and ("resourceSpans" in inner or "batches" in inner):
        return OTLP_JSON

    if "data" in data:
        return JAEGER_JSON

    return None


def get_loader_for_format(format_name: str) -> TraceLoader:
    """Return a loader instance for an explicit format name."""
    if format_name not in _LOADERS:
        raise ValueError(f"Unknown trace format {format_name!r}. Supported: {sorted(_LOADERS)}")
    return _LOADERS[format_name]()


def load_traces(path: str, *, format: str | None = None) -> list[Trace]:
    """Load traces from a file with auto-detection.

    Args:
        path: Path to a Jaeger or OTLP JSON file (any extension).
        format: Optional explicit format override. When omitted, detection
            runs on file contents and extension. Useful for non-standard
            exports where sniffing fails.

    Raises:
        ValueError: If ``format`` is unknown or detection fails on a file
            whose shape can't be matched and no default is sensible.
    """
    if format is not None:
        return get_loader_for_format(format).load(path)

    detected = detect_format(path)
    if detected is None:
        raise ValueError(
            f"Could not detect trace format for {path!r}. "
            'Expected Jaeger JSON ({"data": [...]}) or OTLP JSON '
            '({"resourceSpans": [...]} / {"batches": [...]}). '
            "Pass an explicit format= override if needed."
        )
    return get_loader_for_format(detected).load(path)
