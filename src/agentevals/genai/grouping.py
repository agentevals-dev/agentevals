"""Group traces into conversations by identity attributes.

The key is the first value present, in this order:

1. resource ``agentevals.session_name``
2. ``gen_ai.conversation.id`` on a span, then on the resource
3. ``session.id`` on a span, then on the resource
4. the trace id

Values are caller chosen, so they are coerced to text and capped before use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from ..otel.identity import SESSION_NAME, coerce_key
from ..otel.model import Span, Trace
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


def group_traces(traces: Iterable[Trace], by_conversation: bool) -> list[tuple[str, list[Trace]]]:
    """Evaluation groups in first-seen order. Without conversation grouping each trace stands alone."""
    groups: dict[str, list[Trace]] = {}
    for trace in traces:
        key = conversation_key(trace)[1] if by_conversation else trace.trace_id
        groups.setdefault(key, []).append(trace)
    for members in groups.values():
        members.sort(key=lambda t: t.start_time_unix_nano)
    return list(groups.items())
