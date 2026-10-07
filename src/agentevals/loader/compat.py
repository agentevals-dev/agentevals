"""Temporary bridge from envelope traces to the legacy ``loader.base`` objects.

The legacy extraction (``converter``, ``genai_converter``, ``extraction``, ``trace_metrics``)
reads a flat ``tags`` dict. This module rebuilds that view from envelopes with the rules the
old OTLP loader applied, except that resource attributes now rank below span attributes and
the span tree comes from :func:`agentevals.otel.model.build_traces`, which breaks parent cycles.

Delete together with ``loader/base.py`` once evaluation runs on the canonical GenAI model.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from ..otel import model as otel
from ..trace_attrs import (
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_SCHEMA_URL,
    OTEL_SCOPE,
    OTEL_SCOPE_VERSION,
    SPEC_CONTAINER_ATTRS,
)
from .base import Span, Trace

_PROMOTED_EVENT_KEYS = (OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_OUTPUT_MESSAGES)


def _plain(value: Any) -> Any:
    """Legacy tags carried bytes as base64 text, as OTLP/JSON spells them."""
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _put(tags: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (list, Mapping)) and key not in SPEC_CONTAINER_ATTRS:
        return
    tags[key] = _plain(value)


def legacy_tags(span: otel.Span) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for key, value in span.resource.attributes.items():
        _put(tags, key, value)
    for key, value in span.attributes.items():
        _put(tags, key, value)
    if span.scope.name:
        tags[OTEL_SCOPE] = span.scope.name
    if span.scope.version:
        tags[OTEL_SCOPE_VERSION] = span.scope.version
    schema_url = span.scope.schema_url or span.resource.schema_url
    if schema_url:
        tags[OTEL_SCHEMA_URL] = schema_url
    for event in span.events:
        for key in _PROMOTED_EVENT_KEYS:
            if key in event.attributes and key not in tags:
                _put(tags, key, event.attributes[key])
    return tags


def to_legacy_trace(trace: otel.Trace) -> Trace:
    legacy: dict[str, Span] = {}
    for span_id, span in trace.spans.items():
        legacy[span_id] = Span(
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            operation_name=span.name,
            start_time=span.start_time_unix_nano // 1000,
            duration=(span.end_time_unix_nano - span.start_time_unix_nano) // 1000,
            tags=legacy_tags(span),
        )
    for parent_id, child_ids in trace.children.items():
        legacy[parent_id].children = [legacy[c] for c in child_ids]
    return Trace(
        trace_id=trace.trace_id,
        root_spans=[legacy[r] for r in trace.roots],
        all_spans=list(legacy.values()),
    )


def to_legacy_traces(traces: list[otel.Trace]) -> list[Trace]:
    return [to_legacy_trace(t) for t in traces]
