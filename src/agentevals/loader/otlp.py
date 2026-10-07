"""OTLP/JSON file reader.

Accepted shapes:

1. An OTLP export document: ``{"resourceSpans": [...], "resourceLogs": [...]}``.
2. Tempo v1 ``{"batches": [...]}`` with ``instrumentationLibrarySpans``, and the Tempo v2
   ``{"trace": {...}}`` wrapper.
3. JSON lines where each line is an export document, which is what the Collector ``file``
   exporter writes (traces and logs may be mixed).
4. JSON lines where each line is a bare OTLP span, or a single bare span or list of spans.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..otel.decode import DecodeResult, decode_bare_spans_json, decode_json_document, is_otlp_document
from ..otel.model import Trace, build_traces

logger = logging.getLogger(__name__)

_NOT_JSON = object()


def _looks_like_span(obj: Any) -> bool:
    return isinstance(obj, dict) and ("spanId" in obj or "traceId" in obj)


def decode_otlp_text(content: str) -> DecodeResult:
    """Decode OTLP/JSON file content leniently. Raises ``ValueError`` for content that is not OTLP."""
    content = content.strip()
    if not content:
        return DecodeResult()
    try:
        data = json.loads(content)
    except ValueError:
        data = _NOT_JSON
    except RecursionError as exc:
        raise ValueError("JSON nested too deeply") from exc

    if data is not _NOT_JSON:
        if is_otlp_document(data):
            return decode_json_document(data, strict=False)
        if _looks_like_span(data):
            return decode_bare_spans_json([data], strict=False)
        if isinstance(data, list) and all(_looks_like_span(item) for item in data):
            return decode_bare_spans_json(data, strict=False)
        raise ValueError("not an OTLP JSON document: expected resourceSpans, resourceLogs or batches")

    result = DecodeResult()
    bare: list[dict] = []
    for number, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"line {number}: invalid JSON") from exc
        if is_otlp_document(obj):
            result.extend(decode_json_document(obj, strict=False))
        elif _looks_like_span(obj):
            bare.append(obj)
        else:
            raise ValueError(f"line {number}: expected an OTLP export document or a span")
    if bare:
        result.extend(decode_bare_spans_json(bare, strict=False))
    return result


class OtlpJsonLoader:
    def format_name(self) -> str:
        return "otlp-json"

    def load(self, source: str) -> list[Trace]:
        with open(source, encoding="utf-8") as f:
            result = decode_otlp_text(f.read())
        traces, _ = build_traces(result.spans, result.logs)
        logger.info("Loaded %d trace(s) from %s", len(traces), source)
        return traces

    def load_from_dict(self, data: dict) -> list[Trace]:
        if not isinstance(data, dict) or not is_otlp_document(data):
            raise ValueError("Expected OTLP JSON with 'resourceSpans' or 'batches' key (or wrapped under 'trace')")
        result = decode_json_document(data, strict=False)
        traces, _ = build_traces(result.spans, result.logs)
        return traces
