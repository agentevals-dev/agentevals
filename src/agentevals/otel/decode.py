"""Decode OTLP traces and logs into envelopes.

Two inputs, one result shape:

* OTLP/JSON documents (dicts), including legacy and vendor variants: protobuf field names
  (``resource_spans``, ``trace_id``), ``batches`` and
  ``instrumentationLibrarySpans`` (Tempo v1), the ``{"trace": {...}}`` wrapper (Tempo v2),
  attributes given as flat or nested dicts (ClickHouse JSON columns), enum names instead of
  numbers, and bare spans with no resource envelope (one span per line).
* Protobuf ``Export{Trace,Logs}ServiceRequest`` messages, read field by field. ``MessageToDict``
  is never used: it base64 encodes ids and costs a full extra copy.

Decoders never raise for bad data. Items that cannot be represented are counted in
:class:`DecodeResult` so receivers can report them through ``partialSuccess``.

Id handling depends on ``strict``. Receivers decode strictly: a trace id must be 16 bytes and a
span id 8 bytes (32 and 16 hex characters in JSON) and not all zero, otherwise the item is
rejected. File loaders decode leniently: well formed hex is lowercased, base64 of the right
length is converted to hex, and any other non empty id is kept as is with a warning, so old
exports (64 bit Jaeger ids, base64 ids) still load.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .model import (
    EMPTY_ATTRS,
    EMPTY_RESOURCE,
    EMPTY_SCOPE,
    LogRecord,
    Resource,
    Scope,
    Span,
    SpanEvent,
    SpanLink,
)

logger = logging.getLogger(__name__)

MAX_ANY_VALUE_DEPTH = 64
TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8

EVENT_NAME_ATTRIBUTES = ("otel.event.name", "event.name")

_HEX = frozenset("0123456789abcdef")

_SPAN_KINDS = {
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
_STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


@dataclass(slots=True)
class DecodeResult:
    spans: list[Span] = field(default_factory=list)
    logs: list[LogRecord] = field(default_factory=list)
    rejected_spans: int = 0
    rejected_logs: int = 0
    reasons: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)

    def reject_span(self, reason: str) -> None:
        self.rejected_spans += 1
        self.reasons[reason] += 1

    def reject_log(self, reason: str) -> None:
        self.rejected_logs += 1
        self.reasons[reason] += 1

    def warn(self, message: str) -> None:
        self.warnings[message] += 1

    def extend(self, other: DecodeResult) -> None:
        self.spans.extend(other.spans)
        self.logs.extend(other.logs)
        self.rejected_spans += other.rejected_spans
        self.rejected_logs += other.rejected_logs
        self.reasons.update(other.reasons)
        self.warnings.update(other.warnings)

    @property
    def error_message(self) -> str:
        return "; ".join(f"{n} {reason}" for reason, n in self.reasons.items())


def _frozen(d: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(d) if d else EMPTY_ATTRS


# ---------------------------------------------------------------- ids


def _is_hex(s: str) -> bool:
    return bool(s) and all(c in _HEX for c in s)


def normalize_id(raw: Any, nbytes: int, strict: bool, result: DecodeResult, what: str) -> str | None:
    """Return the id as lowercase hex, or ``None`` when it must be treated as absent or invalid."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (bytes, bytearray)):
        valid = len(raw) == nbytes and any(raw)
        if not raw or (strict and not valid):
            return None
        return bytes(raw).hex()
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if len(lowered) == 2 * nbytes and _is_hex(lowered):
        if set(lowered) == {"0"}:
            return None
        return lowered
    if strict:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) == nbytes and any(decoded):
        return decoded.hex()
    result.warn(f"non standard {what} kept as is")
    return lowered or None


# ---------------------------------------------------------------- scalars


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return 0
    return 0


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    n = _int(value)
    return n or None


def _enum(value: Any, names: Mapping[str, int]) -> int:
    if isinstance(value, str) and value in names:
        return names[value]
    return _int(value)


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


# ---------------------------------------------------------------- AnyValue (JSON)


def any_value_from_json(obj: Any, result: DecodeResult, depth: int = 0) -> Any:
    """Decode an OTLP/JSON ``AnyValue``. Objects without a union field decode to ``None``."""
    if not isinstance(obj, Mapping):
        return None
    if depth > MAX_ANY_VALUE_DEPTH:
        result.warn("value nested deeper than the decoder limit was dropped")
        return None
    if "stringValue" in obj:
        v = obj["stringValue"]
        return v if isinstance(v, str) else str(v)
    if "boolValue" in obj:
        return bool(obj["boolValue"])
    if "intValue" in obj:
        return _int(obj["intValue"])
    if "doubleValue" in obj:
        try:
            return float(obj["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "bytesValue" in obj:
        raw = obj["bytesValue"]
        if isinstance(raw, str):
            try:
                return base64.b64decode(raw, validate=False)
            except (binascii.Error, ValueError):
                return raw.encode()
        return None
    if "arrayValue" in obj:
        arr = obj["arrayValue"] or {}
        values = arr.get("values", []) if isinstance(arr, Mapping) else []
        return [any_value_from_json(v, result, depth + 1) for v in values if isinstance(v, Mapping)]
    if "kvlistValue" in obj:
        kv = obj["kvlistValue"] or {}
        values = kv.get("values", []) if isinstance(kv, Mapping) else []
        out = {}
        for item in values:
            if isinstance(item, Mapping) and isinstance(item.get("key"), str):
                out[item["key"]] = any_value_from_json(item.get("value", {}), result, depth + 1)
        return out
    return None


def _has_any_value(obj: Any) -> bool:
    return isinstance(obj, Mapping) and any(
        k in obj
        for k in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue", "arrayValue", "kvlistValue")
    )


def _flatten_dict(d: Mapping, prefix: str, out: dict, depth: int) -> None:
    for key, value in d.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and depth < MAX_ANY_VALUE_DEPTH:
            _flatten_dict(value, full, out, depth + 1)
        else:
            out[full] = value


def attributes_from_json(attrs: Any, result: DecodeResult) -> Mapping[str, Any]:
    """OTLP ``[{key, value}]`` lists, or dicts (flat or nested, flattened to dotted keys).

    List entries whose value carries no ``AnyValue`` field are skipped, as OTLP readers do.
    """
    out: dict[str, Any] = {}
    if isinstance(attrs, Mapping):
        _flatten_dict(attrs, "", out, 0)
        return _frozen(out)
    if not isinstance(attrs, list):
        return EMPTY_ATTRS
    for item in attrs:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not _has_any_value(value):
            continue
        out[key] = any_value_from_json(value, result)
    return _frozen(out)


# ---------------------------------------------------------------- JSON documents


def _resource_from_json(obj: Any, schema_url: Any, result: DecodeResult) -> Resource:
    if not isinstance(obj, Mapping) and not schema_url:
        return EMPTY_RESOURCE
    obj = obj if isinstance(obj, Mapping) else {}
    attrs = attributes_from_json(obj.get("attributes", []), result)
    return Resource(
        attributes=attrs,
        schema_url=_opt_str(schema_url),
        dropped_attributes_count=_int(obj.get("droppedAttributesCount")),
    )


def _scope_from_json(obj: Any, schema_url: Any, result: DecodeResult) -> Scope:
    if not isinstance(obj, Mapping):
        obj = {}
    name = obj.get("name") if isinstance(obj.get("name"), str) else ""
    if not name and not obj and not schema_url:
        return EMPTY_SCOPE
    return Scope(
        name=name,
        version=_opt_str(obj.get("version")),
        schema_url=_opt_str(schema_url),
        attributes=attributes_from_json(obj.get("attributes", []), result),
        dropped_attributes_count=_int(obj.get("droppedAttributesCount")),
    )


def span_from_json(
    data: Any,
    resource: Resource,
    scope: Scope,
    strict: bool,
    result: DecodeResult,
) -> Span | None:
    if not isinstance(data, Mapping):
        result.reject_span("span(s) rejected: not an object")
        return None
    trace_id = normalize_id(data.get("traceId"), TRACE_ID_BYTES, strict, result, "trace id")
    span_id = normalize_id(data.get("spanId"), SPAN_ID_BYTES, strict, result, "span id")
    if trace_id is None or span_id is None:
        if data.get("traceId") in (None, "") or data.get("spanId") in (None, ""):
            result.reject_span("span(s) rejected: no traceId or spanId field")
        else:
            result.reject_span("span(s) rejected: invalid trace or span id")
        return None
    parent = data.get("parentSpanId")
    parent_id = normalize_id(parent, SPAN_ID_BYTES, strict, result, "parent span id") if parent else None
    if parent and parent_id is None:
        result.warn("invalid parent span id ignored")

    status = data.get("status") if isinstance(data.get("status"), Mapping) else {}
    events = []
    for ev in data.get("events") or []:
        if not isinstance(ev, Mapping):
            continue
        events.append(
            SpanEvent(
                name=ev.get("name") if isinstance(ev.get("name"), str) else "",
                time_unix_nano=_int(ev.get("timeUnixNano")),
                attributes=attributes_from_json(ev.get("attributes", []), result),
                dropped_attributes_count=_int(ev.get("droppedAttributesCount")),
            )
        )
    links = []
    for ln in data.get("links") or []:
        if not isinstance(ln, Mapping):
            continue
        lt = normalize_id(ln.get("traceId"), TRACE_ID_BYTES, strict, result, "link trace id")
        ls = normalize_id(ln.get("spanId"), SPAN_ID_BYTES, strict, result, "link span id")
        if lt is None or ls is None:
            result.warn("link with invalid ids dropped")
            continue
        links.append(
            SpanLink(
                trace_id=lt,
                span_id=ls,
                trace_state=_opt_str(ln.get("traceState")),
                attributes=attributes_from_json(ln.get("attributes", []), result),
                flags=_opt_int(ln.get("flags")),
                dropped_attributes_count=_int(ln.get("droppedAttributesCount")),
            )
        )
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_id,
        name=data.get("name") if isinstance(data.get("name"), str) else "",
        kind=_enum(data.get("kind"), _SPAN_KINDS),
        start_time_unix_nano=_int(data.get("startTimeUnixNano")),
        end_time_unix_nano=_int(data.get("endTimeUnixNano")),
        attributes=attributes_from_json(data.get("attributes", []), result),
        status_code=_enum(status.get("code"), _STATUS_CODES),
        status_message=_opt_str(status.get("message")),
        events=tuple(events),
        links=tuple(links),
        trace_state=_opt_str(data.get("traceState")),
        flags=_opt_int(data.get("flags")),
        resource=resource,
        scope=scope,
        dropped_attributes_count=_int(data.get("droppedAttributesCount")),
        dropped_events_count=_int(data.get("droppedEventsCount")),
        dropped_links_count=_int(data.get("droppedLinksCount")),
    )


def resolve_event_name(event_name: Any, attributes: Mapping[str, Any]) -> str | None:
    """OTLP ``eventName`` first, then the ``otel.event.name`` and legacy ``event.name`` attributes."""
    if isinstance(event_name, str) and event_name:
        return event_name
    for key in EVENT_NAME_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def log_from_json(data: Any, resource: Resource, scope: Scope, strict: bool, result: DecodeResult) -> LogRecord | None:
    if not isinstance(data, Mapping):
        result.reject_log("log record(s) rejected: not an object")
        return None
    raw_trace, raw_span = data.get("traceId"), data.get("spanId")
    trace_id = normalize_id(raw_trace, TRACE_ID_BYTES, strict, result, "trace id")
    span_id = normalize_id(raw_span, SPAN_ID_BYTES, strict, result, "span id")
    if (raw_trace and trace_id is None) or (raw_span and span_id is None):
        if strict:
            result.reject_log("log record(s) rejected: invalid trace or span id")
            return None
    attributes = attributes_from_json(data.get("attributes", []), result)
    body = data.get("body")
    return LogRecord(
        time_unix_nano=_opt_int(data.get("timeUnixNano")),
        observed_time_unix_nano=_opt_int(data.get("observedTimeUnixNano")),
        event_name=resolve_event_name(data.get("eventName"), attributes),
        severity_number=_severity(data.get("severityNumber")),
        severity_text=_opt_str(data.get("severityText")),
        body=any_value_from_json(body, result) if _has_any_value(body) else None,
        attributes=attributes,
        trace_id=trace_id,
        span_id=span_id if trace_id else None,
        flags=_opt_int(data.get("flags")),
        resource=resource,
        scope=scope,
        dropped_attributes_count=_int(data.get("droppedAttributesCount")),
    )


_SEVERITY_NAMES = {
    f"SEVERITY_NUMBER_{name}{suffix}": base + i
    for base, name in ((1, "TRACE"), (5, "DEBUG"), (9, "INFO"), (13, "WARN"), (17, "ERROR"), (21, "FATAL"))
    for i, suffix in enumerate(("", "2", "3", "4"))
}


def _severity(value: Any) -> int | None:
    if isinstance(value, str) and not value.isdigit():
        return _SEVERITY_NAMES.get(value)
    return _opt_int(value)


_PROTO_NAME_ROOTS = ("resource_spans", "resource_logs")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def proto_names_to_json(data: Any) -> Any:
    """Rename protobuf field names (``resource_spans``, ``trace_id``) to their OTLP/JSON spelling, in place.

    protojson with proto names (Go ``UseProtoNames``) writes them and the Collector reads both.
    Only documents rooted at ``resource_spans`` / ``resource_logs`` are touched. Keys of dict valued
    ``attributes`` are attribute names and are kept as they are. Iterative, so nesting depth is no
    concern.
    """
    if not isinstance(data, dict) or not any(k in data for k in _PROTO_NAME_ROOTS):
        return data
    stack: list[Any] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in [k for k in item if isinstance(k, str) and "_" in k]:
                item[_camel(key)] = item.pop(key)
            for key, value in item.items():
                if isinstance(value, (dict, list)) and not (key == "attributes" and isinstance(value, dict)):
                    stack.append(value)
        else:
            stack.extend(v for v in item if isinstance(v, (dict, list)))
    return data


def is_otlp_document(data: Any) -> bool:
    if not isinstance(data, Mapping):
        return False
    if any(k in data for k in ("resourceSpans", "resourceLogs", "batches", *_PROTO_NAME_ROOTS)):
        return True
    inner = data.get("trace")
    return isinstance(inner, Mapping) and ("resourceSpans" in inner or "batches" in inner)


def decode_json_document(data: Any, *, strict: bool) -> DecodeResult:
    """Decode an OTLP/JSON export document (traces, logs, or both)."""
    result = DecodeResult()
    if not isinstance(data, Mapping):
        return result
    data = proto_names_to_json(data)
    inner = data.get("trace")
    if isinstance(inner, Mapping) and ("resourceSpans" in inner or "batches" in inner):
        data = inner

    resource_spans = data.get("resourceSpans") or data.get("batches") or []
    for rs in resource_spans if isinstance(resource_spans, list) else []:
        if not isinstance(rs, Mapping):
            continue
        resource = _resource_from_json(rs.get("resource"), rs.get("schemaUrl"), result)
        scope_spans = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for ss in scope_spans if isinstance(scope_spans, list) else []:
            if not isinstance(ss, Mapping):
                continue
            scope = _scope_from_json(ss.get("scope") or ss.get("instrumentationLibrary"), ss.get("schemaUrl"), result)
            for sp in ss.get("spans") or []:
                span = span_from_json(sp, resource, scope, strict, result)
                if span is not None:
                    result.spans.append(span)

    resource_logs = data.get("resourceLogs") or []
    for rl in resource_logs if isinstance(resource_logs, list) else []:
        if not isinstance(rl, Mapping):
            continue
        resource = _resource_from_json(rl.get("resource"), rl.get("schemaUrl"), result)
        scope_logs = rl.get("scopeLogs") or rl.get("instrumentationLibraryLogs") or []
        for sl in scope_logs if isinstance(scope_logs, list) else []:
            if not isinstance(sl, Mapping):
                continue
            scope = _scope_from_json(sl.get("scope") or sl.get("instrumentationLibrary"), sl.get("schemaUrl"), result)
            for lr in sl.get("logRecords") or []:
                log = log_from_json(lr, resource, scope, strict, result)
                if log is not None:
                    result.logs.append(log)
    return result


def decode_bare_spans_json(items: Iterable[Any], *, strict: bool) -> DecodeResult:
    """Spans without a resource envelope (one OTLP span object per line)."""
    result = DecodeResult()
    for item in items:
        span = span_from_json(item, EMPTY_RESOURCE, EMPTY_SCOPE, strict, result)
        if span is not None:
            result.spans.append(span)
    return result


# ---------------------------------------------------------------- protobuf


def any_value_from_proto(av: Any, result: DecodeResult, depth: int = 0) -> Any:
    if depth > MAX_ANY_VALUE_DEPTH:
        result.warn("value nested deeper than the decoder limit was dropped")
        return None
    which = av.WhichOneof("value")
    if which is None:
        return None
    if which == "string_value":
        return av.string_value
    if which == "bool_value":
        return av.bool_value
    if which == "int_value":
        return av.int_value
    if which == "double_value":
        return av.double_value
    if which == "bytes_value":
        return bytes(av.bytes_value)
    if which == "array_value":
        return [any_value_from_proto(v, result, depth + 1) for v in av.array_value.values]
    if which == "kvlist_value":
        return {kv.key: any_value_from_proto(kv.value, result, depth + 1) for kv in av.kvlist_value.values}
    return None


def attributes_from_proto(kvs: Iterable[Any], result: DecodeResult) -> Mapping[str, Any]:
    out = {}
    for kv in kvs:
        if kv.value.WhichOneof("value") is None:
            continue
        out[kv.key] = any_value_from_proto(kv.value, result)
    return _frozen(out)


def _resource_from_proto(rs: Any, result: DecodeResult) -> Resource:
    attrs = attributes_from_proto(rs.resource.attributes, result)
    schema_url = rs.schema_url or None
    dropped = rs.resource.dropped_attributes_count
    if not attrs and not schema_url and not dropped:
        return EMPTY_RESOURCE
    return Resource(attributes=attrs, schema_url=schema_url, dropped_attributes_count=dropped)


def _scope_from_proto(ss: Any, result: DecodeResult) -> Scope:
    sc = ss.scope
    attrs = attributes_from_proto(sc.attributes, result)
    if not sc.name and not sc.version and not attrs and not ss.schema_url and not sc.dropped_attributes_count:
        return EMPTY_SCOPE
    return Scope(
        name=sc.name,
        version=sc.version or None,
        schema_url=ss.schema_url or None,
        attributes=attrs,
        dropped_attributes_count=sc.dropped_attributes_count,
    )


def decode_traces_proto(request: Any) -> DecodeResult:
    """Decode an ``ExportTraceServiceRequest``. Always strict: protobuf only arrives on receivers."""
    result = DecodeResult()
    for rs in request.resource_spans:
        resource = _resource_from_proto(rs, result)
        for ss in rs.scope_spans:
            scope = _scope_from_proto(ss, result)
            for sp in ss.spans:
                trace_id = normalize_id(sp.trace_id, TRACE_ID_BYTES, True, result, "trace id")
                span_id = normalize_id(sp.span_id, SPAN_ID_BYTES, True, result, "span id")
                if trace_id is None or span_id is None:
                    result.reject_span("span(s) rejected: invalid trace or span id")
                    continue
                parent_id = (
                    normalize_id(sp.parent_span_id, SPAN_ID_BYTES, True, result, "parent span id")
                    if sp.parent_span_id
                    else None
                )
                if sp.parent_span_id and parent_id is None:
                    result.warn("invalid parent span id ignored")
                links = []
                for ln in sp.links:
                    lt = normalize_id(ln.trace_id, TRACE_ID_BYTES, True, result, "link trace id")
                    ls = normalize_id(ln.span_id, SPAN_ID_BYTES, True, result, "link span id")
                    if lt is None or ls is None:
                        result.warn("link with invalid ids dropped")
                        continue
                    links.append(
                        SpanLink(
                            lt,
                            ls,
                            ln.trace_state or None,
                            attributes_from_proto(ln.attributes, result),
                            ln.flags or None,
                            ln.dropped_attributes_count,
                        )
                    )
                result.spans.append(
                    Span(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_id,
                        name=sp.name,
                        kind=int(sp.kind),
                        start_time_unix_nano=sp.start_time_unix_nano,
                        end_time_unix_nano=sp.end_time_unix_nano,
                        attributes=attributes_from_proto(sp.attributes, result),
                        status_code=int(sp.status.code),
                        status_message=sp.status.message or None,
                        events=tuple(
                            SpanEvent(
                                ev.name,
                                ev.time_unix_nano,
                                attributes_from_proto(ev.attributes, result),
                                ev.dropped_attributes_count,
                            )
                            for ev in sp.events
                        ),
                        links=tuple(links),
                        trace_state=sp.trace_state or None,
                        flags=sp.flags or None,
                        resource=resource,
                        scope=scope,
                        dropped_attributes_count=sp.dropped_attributes_count,
                        dropped_events_count=sp.dropped_events_count,
                        dropped_links_count=sp.dropped_links_count,
                    )
                )
    return result


def decode_logs_proto(request: Any) -> DecodeResult:
    """Decode an ``ExportLogsServiceRequest``. Always strict."""
    result = DecodeResult()
    for rl in request.resource_logs:
        resource = _resource_from_proto(rl, result)
        for sl in rl.scope_logs:
            scope = _scope_from_proto(sl, result)
            for lr in sl.log_records:
                trace_id = normalize_id(lr.trace_id, TRACE_ID_BYTES, True, result, "trace id") if lr.trace_id else None
                span_id = normalize_id(lr.span_id, SPAN_ID_BYTES, True, result, "span id") if lr.span_id else None
                if (lr.trace_id and trace_id is None) or (lr.span_id and span_id is None):
                    result.reject_log("log record(s) rejected: invalid trace or span id")
                    continue
                attributes = attributes_from_proto(lr.attributes, result)
                result.logs.append(
                    LogRecord(
                        time_unix_nano=lr.time_unix_nano or None,
                        observed_time_unix_nano=lr.observed_time_unix_nano or None,
                        event_name=resolve_event_name(getattr(lr, "event_name", ""), attributes),
                        severity_number=int(lr.severity_number) or None,
                        severity_text=lr.severity_text or None,
                        body=any_value_from_proto(lr.body, result) if lr.HasField("body") else None,
                        attributes=attributes,
                        trace_id=trace_id,
                        span_id=span_id if trace_id else None,
                        flags=lr.flags or None,
                        resource=resource,
                        scope=scope,
                        dropped_attributes_count=lr.dropped_attributes_count,
                    )
                )
    return result
