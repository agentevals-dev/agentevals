"""Jaeger JSON reader (legacy import format).

Jaeger stores OTLP fields as span tags when it ingests OTLP: span kind, status, and the
instrumentation scope. They are mapped back to envelope fields here, so the attributes hold
only what the producer set. ``processes`` become resources (``service.name`` plus process
tags) and span ``logs`` become span events.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from types import MappingProxyType
from typing import Any

from ..otel.decode import SPAN_ID_BYTES, TRACE_ID_BYTES, DecodeResult, normalize_id
from ..otel.model import (
    EMPTY_ATTRS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    Resource,
    Scope,
    Span,
    SpanEvent,
    SpanLink,
    build_traces,
)
from .base import Trace, TraceLoader
from .compat import to_legacy_traces

logger = logging.getLogger(__name__)

_KINDS = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
_STATUS = {"OK": STATUS_OK, "ERROR": STATUS_ERROR, "UNSET": STATUS_UNSET}
_SCOPE_NAME_TAGS = ("otel.scope.name", "otel.library.name")
_SCOPE_VERSION_TAGS = ("otel.scope.version", "otel.library.version")
_FIELD_TAGS = frozenset(
    {"span.kind", "otel.status_code", "otel.status_description", *_SCOPE_NAME_TAGS, *_SCOPE_VERSION_TAGS}
)


def _tag_value(tag: dict) -> Any:
    value = tag.get("value")
    kind = tag.get("type")
    if kind == "binary" and isinstance(value, str):
        try:
            return base64.b64decode(value)
        except (binascii.Error, ValueError):
            return value
    if kind == "int64" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if kind == "float64" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _tags(raw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag in raw or []:
        if isinstance(tag, dict) and isinstance(tag.get("key"), str):
            out[tag["key"]] = _tag_value(tag)
    return out


def decode_jaeger_document(data: Any) -> DecodeResult:
    result = DecodeResult()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Invalid Jaeger JSON format: expected top-level 'data' key")
    for trace_data in data["data"]:
        if not isinstance(trace_data, dict):
            continue
        resources: dict[str, Resource] = {}
        for pid, proc in (trace_data.get("processes") or {}).items():
            if not isinstance(proc, dict):
                continue
            attrs = _tags(proc.get("tags"))
            if isinstance(proc.get("serviceName"), str):
                attrs = {"service.name": proc["serviceName"], **attrs}
            resources[pid] = Resource(attributes=MappingProxyType(attrs) if attrs else EMPTY_ATTRS)
        scopes: dict[tuple[str, str | None], Scope] = {}
        for raw in trace_data.get("spans") or []:
            if not isinstance(raw, dict):
                continue
            span = _span(raw, resources, scopes, result)
            if span is not None:
                result.spans.append(span)
    return result


def _span(raw: dict, resources: dict[str, Resource], scopes: dict, result: DecodeResult) -> Span | None:
    trace_id = normalize_id(raw.get("traceID"), TRACE_ID_BYTES, False, result, "trace id")
    span_id = normalize_id(raw.get("spanID"), SPAN_ID_BYTES, False, result, "span id")
    if trace_id is None or span_id is None:
        result.reject_span("span(s) rejected: invalid trace or span id")
        return None
    tags = _tags(raw.get("tags"))
    parent_id = None
    links = []
    for ref in raw.get("references") or []:
        if not isinstance(ref, dict):
            continue
        ref_span = normalize_id(ref.get("spanID"), SPAN_ID_BYTES, False, result, "parent span id")
        if ref_span is None:
            continue
        if ref.get("refType") == "CHILD_OF" and parent_id is None:
            parent_id = ref_span
        else:
            ref_trace = normalize_id(ref.get("traceID"), TRACE_ID_BYTES, False, result, "link trace id") or trace_id
            links.append(SpanLink(trace_id=ref_trace, span_id=ref_span))

    status = _STATUS.get(str(tags.get("otel.status_code", "")).upper(), STATUS_UNSET)
    if status == STATUS_UNSET and tags.get("error") is True:
        status = STATUS_ERROR
    scope_name = next((tags[k] for k in _SCOPE_NAME_TAGS if isinstance(tags.get(k), str)), "")
    scope_version = next((tags[k] for k in _SCOPE_VERSION_TAGS if isinstance(tags.get(k), str)), None)
    scope_key = (scope_name, scope_version)
    if scope_key not in scopes:
        scopes[scope_key] = Scope(name=scope_name, version=scope_version)

    events = []
    for log in raw.get("logs") or []:
        if not isinstance(log, dict):
            continue
        fields = _tags(log.get("fields"))
        name = fields.pop("event", "log")
        events.append(
            SpanEvent(
                name=str(name),
                time_unix_nano=int(log.get("timestamp") or 0) * 1000,
                attributes=MappingProxyType(fields),
            )
        )

    start_us = int(raw.get("startTime") or 0)
    duration_us = int(raw.get("duration") or 0)
    status_message = tags.get("otel.status_description")
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_id,
        name=raw.get("operationName") if isinstance(raw.get("operationName"), str) else "",
        kind=_KINDS.get(str(tags.get("span.kind", "")).lower(), 0),
        start_time_unix_nano=start_us * 1000,
        end_time_unix_nano=(start_us + duration_us) * 1000,
        attributes=MappingProxyType({k: v for k, v in tags.items() if k not in _FIELD_TAGS}),
        status_code=status,
        status_message=status_message if isinstance(status_message, str) else None,
        events=tuple(events),
        links=tuple(links),
        resource=resources.get(raw.get("processID"), Resource()),
        scope=scopes[scope_key],
    )


class JaegerJsonLoader(TraceLoader):
    """Legacy entry point returning ``loader.base`` traces for the current extraction."""

    def format_name(self) -> str:
        return "jaeger-json"

    def load(self, source: str) -> list[Trace]:
        with open(source, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or "data" not in raw:
            raise ValueError(f"Invalid Jaeger JSON format: expected top-level 'data' key in {source}")
        result = decode_jaeger_document(raw)
        traces, _ = build_traces(result.spans)
        logger.info("Loaded %d trace(s) from %s", len(traces), source)
        return to_legacy_traces(traces)
