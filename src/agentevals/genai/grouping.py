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

from ..otel.model import Trace
from .semconv import CONVERSATION_ID, SESSION_ID

SESSION_NAME = "agentevals.session_name"
MAX_KEY_LENGTH = 256

KeyKind = Literal["name", "conversation", "session_id", "trace"]


def coerce_key(value: Any) -> str | None:
    """A usable identity value: scalar, printable, at most 256 characters; otherwise ``None``."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_KEY_LENGTH or not text.isprintable():
        return None
    return text


def _first(attr_maps: Iterable[Mapping[str, Any]], key: str) -> str | None:
    for attrs in attr_maps:
        value = coerce_key(attrs.get(key))
        if value:
            return value
    return None


def conversation_key(trace: Trace) -> tuple[KeyKind, str]:
    spans = list(trace.spans.values())
    resources = [s.resource.attributes for s in spans]
    name = _first(resources, SESSION_NAME)
    if name:
        return "name", name
    for key, kind in ((CONVERSATION_ID, "conversation"), (SESSION_ID, "session_id")):
        value = _first((s.attributes for s in spans), key) or _first(resources, key)
        if value:
            return kind, value
    return "trace", trace.trace_id


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
