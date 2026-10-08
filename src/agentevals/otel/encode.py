"""Encode envelopes back to OTLP/JSON.

Used for session export and debug bundles. Spans and logs are grouped under the resource and
scope objects they were decoded with, and every id is written as decoded: the raw
``parentSpanId`` is kept even when the derived tree broke that link.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from typing import Any

from .model import LogRecord, Resource, Scope, Span, Trace


def any_value_to_json(value: Any) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (bytes, bytearray)):
        return {"bytesValue": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [any_value_to_json(v) for v in value]}}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": [{"key": str(k), "value": any_value_to_json(v)} for k, v in value.items()]}}
    return {}


def attributes_to_json(attrs: Mapping[str, Any]) -> list[dict]:
    return [{"key": k, "value": any_value_to_json(v)} for k, v in attrs.items()]


def _dropped(out: dict, **counts: int) -> dict:
    """Write non zero ``dropped*Count`` fields, the producer's only sign that it truncated something."""
    for name, value in counts.items():
        if value:
            out[name] = value
    return out


def _resource_json(resource: Resource) -> dict:
    return _dropped(
        {"attributes": attributes_to_json(resource.attributes)},
        droppedAttributesCount=resource.dropped_attributes_count,
    )


def _scope_json(scope: Scope) -> dict:
    out: dict[str, Any] = {"name": scope.name}
    if scope.version:
        out["version"] = scope.version
    if scope.attributes:
        out["attributes"] = attributes_to_json(scope.attributes)
    return _dropped(out, droppedAttributesCount=scope.dropped_attributes_count)


def span_to_json(span: Span) -> dict:
    out: dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": str(span.start_time_unix_nano),
        "endTimeUnixNano": str(span.end_time_unix_nano),
        "attributes": attributes_to_json(span.attributes),
    }
    if span.parent_span_id:
        out["parentSpanId"] = span.parent_span_id
    if span.trace_state:
        out["traceState"] = span.trace_state
    if span.flags is not None:
        out["flags"] = span.flags
    if span.events:
        out["events"] = [
            _dropped(
                {"name": e.name, "timeUnixNano": str(e.time_unix_nano), "attributes": attributes_to_json(e.attributes)},
                droppedAttributesCount=e.dropped_attributes_count,
            )
            for e in span.events
        ]
    if span.links:
        links = []
        for link in span.links:
            item: dict[str, Any] = {
                "traceId": link.trace_id,
                "spanId": link.span_id,
                "attributes": attributes_to_json(link.attributes),
            }
            if link.trace_state:
                item["traceState"] = link.trace_state
            if link.flags is not None:
                item["flags"] = link.flags
            links.append(_dropped(item, droppedAttributesCount=link.dropped_attributes_count))
        out["links"] = links
    if span.status_code or span.status_message:
        status: dict[str, Any] = {"code": span.status_code}
        if span.status_message:
            status["message"] = span.status_message
        out["status"] = status
    return _dropped(
        out,
        droppedAttributesCount=span.dropped_attributes_count,
        droppedEventsCount=span.dropped_events_count,
        droppedLinksCount=span.dropped_links_count,
    )


def log_to_json(log: LogRecord) -> dict:
    out: dict[str, Any] = {"attributes": attributes_to_json(log.attributes)}
    if log.time_unix_nano is not None:
        out["timeUnixNano"] = str(log.time_unix_nano)
    if log.observed_time_unix_nano is not None:
        out["observedTimeUnixNano"] = str(log.observed_time_unix_nano)
    if log.severity_number is not None:
        out["severityNumber"] = log.severity_number
    if log.severity_text:
        out["severityText"] = log.severity_text
    if log.event_name:
        out["eventName"] = log.event_name
    if log.body is not None:
        out["body"] = any_value_to_json(log.body)
    if log.trace_id:
        out["traceId"] = log.trace_id
    if log.span_id:
        out["spanId"] = log.span_id
    if log.flags is not None:
        out["flags"] = log.flags
    return _dropped(out, droppedAttributesCount=log.dropped_attributes_count)


def _group(items: Iterable[Any]) -> list[tuple[Resource, list[tuple[Scope, list[Any]]]]]:
    """Group by the resource and scope objects items were decoded with, in first-seen order."""
    resources: dict[int, tuple[Resource, dict[int, tuple[Scope, list[Any]]]]] = {}
    for item in items:
        res_entry = resources.setdefault(id(item.resource), (item.resource, {}))
        scope_entry = res_entry[1].setdefault(id(item.scope), (item.scope, []))
        scope_entry[1].append(item)
    return [(res, list(scopes.values())) for res, scopes in resources.values()]


def encode_document(spans: Iterable[Span] = (), logs: Iterable[LogRecord] = ()) -> dict:
    """Build one OTLP/JSON document holding ``resourceSpans`` and ``resourceLogs``."""
    resource_spans = []
    for resource, scopes in _group(spans):
        entry: dict[str, Any] = {
            "resource": _resource_json(resource),
            "scopeSpans": [],
        }
        if resource.schema_url:
            entry["schemaUrl"] = resource.schema_url
        for scope, items in scopes:
            scope_entry: dict[str, Any] = {"scope": _scope_json(scope), "spans": [span_to_json(s) for s in items]}
            if scope.schema_url:
                scope_entry["schemaUrl"] = scope.schema_url
            entry["scopeSpans"].append(scope_entry)
        resource_spans.append(entry)

    resource_logs = []
    for resource, scopes in _group(logs):
        entry = {"resource": _resource_json(resource), "scopeLogs": []}
        if resource.schema_url:
            entry["schemaUrl"] = resource.schema_url
        for scope, items in scopes:
            scope_entry = {"scope": _scope_json(scope), "logRecords": [log_to_json(r) for r in items]}
            if scope.schema_url:
                scope_entry["schemaUrl"] = scope.schema_url
            entry["scopeLogs"].append(scope_entry)
        resource_logs.append(entry)

    doc: dict[str, Any] = {"resourceSpans": resource_spans}
    if resource_logs:
        doc["resourceLogs"] = resource_logs
    return doc


def encode_traces(traces: Iterable[Trace], extra_logs: Iterable[LogRecord] = ()) -> dict:
    """Encode traces with every joined and orphan log they hold."""
    traces = list(traces)
    spans = [s for t in traces for s in t.spans.values()]
    logs = [r for t in traces for recs in t.logs.values() for r in recs]
    logs += [r for t in traces for r in t.orphan_logs]
    logs += list(extra_logs)
    logs.sort(key=lambda r: r.time_unix_nano or r.observed_time_unix_nano or 0)
    return encode_document(spans, logs)
