"""Lossless envelope model for OpenTelemetry data.

These types mirror OTLP closely enough that a document decoded into them and encoded back
round-trips. Ids and attributes are never rewritten. Anything derived (the span tree, joined
logs, cycle breaking) lives on :class:`Trace` as indexes, so spans themselves stay immutable
and a trace can be snapshotted by copying its dicts.

Attribute values are decoded OTLP ``AnyValue``: ``str``, ``bool``, ``int``, ``float``,
``bytes``, ``list`` or ``dict`` (nested to the decoder's depth limit).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

EMPTY_ATTRS: Mapping[str, Any] = MappingProxyType({})

(
    SPAN_KIND_UNSPECIFIED,
    SPAN_KIND_INTERNAL,
    SPAN_KIND_SERVER,
    SPAN_KIND_CLIENT,
    SPAN_KIND_PRODUCER,
    SPAN_KIND_CONSUMER,
) = range(6)
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

# W3C trace flags bits carried on OTLP spans (proto field `flags`): bit 8 says the
# parent-is-remote bit is known, bit 9 says the parent is remote.
FLAG_HAS_IS_REMOTE = 0x100
FLAG_IS_REMOTE = 0x200

MAX_JSON_PARSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SpanRef:
    trace_id: str
    span_id: str


@dataclass(frozen=True, slots=True)
class Resource:
    attributes: Mapping[str, Any] = EMPTY_ATTRS
    schema_url: str | None = None


@dataclass(frozen=True, slots=True)
class Scope:
    name: str = ""
    version: str | None = None
    schema_url: str | None = None
    attributes: Mapping[str, Any] = EMPTY_ATTRS


EMPTY_RESOURCE = Resource()
EMPTY_SCOPE = Scope()


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    time_unix_nano: int
    attributes: Mapping[str, Any] = EMPTY_ATTRS


@dataclass(frozen=True, slots=True)
class SpanLink:
    trace_id: str
    span_id: str
    trace_state: str | None = None
    attributes: Mapping[str, Any] = EMPTY_ATTRS
    flags: int | None = None


@dataclass(frozen=True, slots=True)
class LogRecord:
    time_unix_nano: int | None
    observed_time_unix_nano: int | None
    event_name: str | None
    severity_number: int | None
    severity_text: str | None
    body: Any
    attributes: Mapping[str, Any]
    trace_id: str | None
    span_id: str | None
    flags: int | None
    resource: Resource
    scope: Scope

    @property
    def ref(self) -> SpanRef | None:
        if self.trace_id and self.span_id:
            return SpanRef(self.trace_id, self.span_id)
        return None


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_time_unix_nano: int
    end_time_unix_nano: int
    attributes: Mapping[str, Any]
    status_code: int = STATUS_UNSET
    status_message: str | None = None
    events: tuple[SpanEvent, ...] = ()
    links: tuple[SpanLink, ...] = ()
    trace_state: str | None = None
    flags: int | None = None
    resource: Resource = EMPTY_RESOURCE
    scope: Scope = EMPTY_SCOPE

    @property
    def ref(self) -> SpanRef:
        return SpanRef(self.trace_id, self.span_id)

    @property
    def has_remote_parent(self) -> bool:
        return self.flags is not None and self.flags & (FLAG_HAS_IS_REMOTE | FLAG_IS_REMOTE) == (
            FLAG_HAS_IS_REMOTE | FLAG_IS_REMOTE
        )

    @property
    def duration_unix_nano(self) -> int:
        return max(0, self.end_time_unix_nano - self.start_time_unix_nano)


@dataclass(slots=True)
class Trace:
    """Spans of one trace plus derived indexes.

    ``parents`` holds the effective parent used for the tree: the raw ``parent_span_id``
    unless that parent is absent from the trace or the link was broken to resolve a cycle.
    """

    trace_id: str
    spans: dict[str, Span] = field(default_factory=dict)
    parents: dict[str, str | None] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    logs: dict[str, list[LogRecord]] = field(default_factory=dict)
    orphan_logs: list[LogRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def root_spans(self) -> list[Span]:
        return [self.spans[s] for s in self.roots]

    def children_of(self, span: Span | str) -> list[Span]:
        span_id = span if isinstance(span, str) else span.span_id
        return [self.spans[c] for c in self.children.get(span_id, ())]

    def parent_of(self, span: Span | str) -> Span | None:
        span_id = span if isinstance(span, str) else span.span_id
        parent = self.parents.get(span_id)
        return self.spans.get(parent) if parent else None

    def logs_of(self, span: Span | str) -> list[LogRecord]:
        span_id = span if isinstance(span, str) else span.span_id
        return self.logs.get(span_id, [])

    def ancestors(self, span: Span | str) -> list[Span]:
        """Nearest first. Terminates because the effective parent graph is acyclic."""
        out = []
        current = self.parent_of(span)
        while current is not None:
            out.append(current)
            current = self.parent_of(current)
        return out

    def walk(self, start: Span | str | None = None) -> list[Span]:
        """Depth first, children in start-time order. Iterative."""
        if start is None:
            stack = list(reversed(self.roots))
        else:
            stack = [start if isinstance(start, str) else start.span_id]
        out = []
        while stack:
            span_id = stack.pop()
            out.append(self.spans[span_id])
            stack.extend(reversed(self.children.get(span_id, ())))
        return out

    @property
    def start_time_unix_nano(self) -> int:
        return min((s.start_time_unix_nano for s in self.spans.values()), default=0)


def attr_str(attrs: Mapping[str, Any], key: str) -> str | None:
    value = attrs.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    return None


def attr_int(attrs: Mapping[str, Any], key: str) -> int | None:
    value = attrs.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def attr_float(attrs: Mapping[str, Any], key: str) -> float | None:
    value = attrs.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_json_value(value: Any) -> Any:
    """Structured values pass through; strings are parsed as JSON within a size cap.

    Returns ``None`` for anything that cannot be parsed. Never raises.
    """
    if value is None or isinstance(value, (dict, list, bool, int, float)):
        return value
    if isinstance(value, bytes):
        return None
    if not isinstance(value, str) or len(value) > MAX_JSON_PARSE_BYTES:
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def attr_json(attrs: Mapping[str, Any], key: str) -> Any:
    return parse_json_value(attrs.get(key))


def _order_key(span: Span) -> tuple[int, str]:
    return (span.start_time_unix_nano, span.span_id)


def _link_tree(trace: Trace) -> None:
    """Derive effective parents, children and roots. Never mutates spans.

    A parent outside the trace leaves the span a root. A self parent becomes a root. In a
    cycle, the member that started first (ties: lowest span id) becomes a root, so the result
    does not depend on arrival order. Each break is recorded in ``trace.warnings``.
    """
    spans = trace.spans
    parents: dict[str, str | None] = {}
    for span_id, span in spans.items():
        parent = span.parent_span_id
        if parent == span_id:
            trace.warnings.append(f"span {span_id} names itself as parent; treated as a root")
            parent = None
        parents[span_id] = parent if parent in spans else None

    state: dict[str, int] = {}
    for start in spans:
        if state.get(start):
            continue
        path: list[str] = []
        on_path: dict[str, int] = {}
        node: str | None = start
        while node is not None and not state.get(node):
            if node in on_path:
                cycle = path[on_path[node] :]
                root = min(cycle, key=lambda s: _order_key(spans[s]))
                parents[root] = None
                trace.warnings.append(f"parent cycle through {len(cycle)} spans broken at {root}")
                break
            on_path[node] = len(path)
            path.append(node)
            node = parents[node]
        for visited in path:
            state[visited] = 1

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for span_id, parent in parents.items():
        if parent is None:
            roots.append(span_id)
        else:
            children.setdefault(parent, []).append(span_id)
    for kids in children.values():
        kids.sort(key=lambda s: _order_key(spans[s]))
    roots.sort(key=lambda s: _order_key(spans[s]))
    trace.parents, trace.children, trace.roots = parents, children, roots


def build_traces(spans: Iterable[Span], logs: Iterable[LogRecord] = ()) -> tuple[list[Trace], list[LogRecord]]:
    """Group spans into traces and join logs to spans by (trace_id, span_id).

    Returns the traces in first-seen order and the logs that belong to no trace in this batch
    (no trace context, or a trace with no spans here). For a duplicated span id the first copy
    wins, which is what an exporter retry needs.
    """
    traces: dict[str, Trace] = {}
    for span in spans:
        trace = traces.get(span.trace_id)
        if trace is None:
            trace = traces[span.trace_id] = Trace(trace_id=span.trace_id)
        if span.span_id in trace.spans:
            trace.warnings.append(f"duplicate span {span.span_id} ignored")
            continue
        trace.spans[span.span_id] = span
    for trace in traces.values():
        _link_tree(trace)

    unattributed: list[LogRecord] = []
    for log in logs:
        trace = traces.get(log.trace_id) if log.trace_id else None
        if trace is None:
            unattributed.append(log)
        elif log.span_id and log.span_id in trace.spans:
            trace.logs.setdefault(log.span_id, []).append(log)
        else:
            trace.orphan_logs.append(log)
    return list(traces.values()), unattributed
