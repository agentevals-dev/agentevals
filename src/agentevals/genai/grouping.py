"""Group traces into conversations by identity attributes.

The key is the first value present, in this order:

1. resource ``agentevals.session_name``
2. ``gen_ai.conversation.id`` on a span, then on the resource
3. ``session.id`` on a span, then on the resource
4. the trace id

Values are caller chosen, so they are coerced to text and capped before use.

In every mode, a trace whose agent span resumes a turn started in another trace of the batch (see
:func:`.extract.resumes`) joins that trace's group, so a paused and resumed turn is evaluated whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from ..otel.identity import SESSION_NAME, coerce_key
from ..otel.model import Span, SpanRef, Trace
from .extract import resume_links, resumes
from .overlay import SpanView, overlay
from .semconv import CONVERSATION_ID, SESSION_ID

KeyKind = Literal["name", "conversation", "session_id", "trace"]


def _first(attr_maps: Iterable[Mapping[str, Any]], key: str) -> str | None:
    for attrs in attr_maps:
        value = coerce_key(attrs.get(key))
        if value:
            return value
    return None


def identity_key(items: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> tuple[KeyKind, str] | None:
    """The identity key of a set of spans or log records, given as ``(attributes, resource attributes)``."""
    items = list(items)
    resources = [r for _, r in items]
    name = _first(resources, SESSION_NAME)
    if name:
        return "name", name
    for key, kind in ((CONVERSATION_ID, "conversation"), (SESSION_ID, "session_id")):
        value = _first((a for a, _ in items), key) or _first(resources, key)
        if value:
            return kind, value
    return None


def by_start(spans: Iterable[Span]) -> list[Span]:
    """Spans in start order, so the outermost span's identity wins over a delegate's whatever order
    the spans arrived in (an A2A sub agent can carry its own ``gen_ai.conversation.id``)."""
    return sorted(spans, key=lambda s: (s.start_time_unix_nano, s.span_id))


def conversation_key(trace: Trace) -> tuple[KeyKind, str]:
    key = identity_key((s.attributes, s.resource.attributes) for s in by_start(trace.spans.values()))
    return key or ("trace", trace.trace_id)


def has_session_name(traces: Iterable[Trace]) -> bool:
    return any(conversation_key(t)[0] == "name" for t in traces)


def _resume_edges(traces: list[Trace]) -> Iterable[tuple[str, str]]:
    """``(resumed trace id, origin trace id)`` for every resume link between two traces of the batch."""
    by_id = {t.trace_id: t for t in traces}
    origins: dict[SpanRef, SpanView | None] = {}
    for trace in traces:
        for span in trace.spans.values():
            if not span.links:
                continue
            view = overlay(span, trace.logs_of(span))
            for ref in dict.fromkeys(resume_links(view)):
                if ref.trace_id == trace.trace_id:
                    continue
                if ref not in origins:
                    origin_trace = by_id.get(ref.trace_id)
                    origin = origin_trace.spans.get(ref.span_id) if origin_trace else None
                    origins[ref] = overlay(origin, origin_trace.logs_of(origin)) if origin else None
                if origins[ref] is not None and resumes(view, origins[ref]):
                    yield trace.trace_id, ref.trace_id


def group_traces(traces: Iterable[Trace], by_conversation: bool) -> list[tuple[str, list[Trace]]]:
    """Evaluation groups in first-seen order. Without conversation grouping each trace stands alone,
    unless it resumes a turn of another trace."""
    traces = list(traces)
    key_of = {t.trace_id: conversation_key(t)[1] if by_conversation else t.trace_id for t in traces}
    parent: dict[str, str] = {}

    def root(key: str) -> str:
        while key in parent:
            parent[key] = parent.get(parent[key], parent[key])
            key = parent[key]
        return key

    for resumed, origin in _resume_edges(traces):
        a, b = root(key_of[resumed]), root(key_of[origin])
        if a != b:
            parent[a] = b

    groups: dict[str, list[Trace]] = {}
    for trace in traces:
        groups.setdefault(root(key_of[trace.trace_id]), []).append(trace)
    for members in groups.values():
        members.sort(key=lambda t: t.start_time_unix_nano)
    return list(groups.items())
