"""The routing policy the live store uses for GenAI telemetry.

The store (``otel.store``) knows nothing about GenAI. It asks this policy for a session key, whether
a trace without a key is worth a session, which log records to keep, and which records are
agentevals' own output coming back through a pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..otel.identity import EMITTER_SCOPE
from ..otel.model import LogRecord, Span
from .extract import IGNORED, NON_GENAI, classify
from .grouping import KeyKind, identity_key
from .overlay import overlay
from .semconv import EVENT_EVALUATION_RESULT

SERVICE_INSTANCE_ID = "service.instance.id"


class GenAIRoutingPolicy:
    """Sessions keyed by ``agentevals.session_name``, ``gen_ai.conversation.id`` or ``session.id``.

    Only ``agentevals.session_name`` sessions split into ``name-2`` on a rerun; conversation and
    ``session.id`` keys always rejoin. ``key_strength`` follows the precedence of
    :func:`~agentevals.genai.grouping.identity_key`: a session keyed by ``session.id`` moves to the
    conversation once a span of its trace carries ``gen_ai.conversation.id``, which happens when a
    subprocess (such as a coding agent harness) exports before the span that owns the turn.
    ``emitter_instance_id`` is the ``service.instance.id`` this process emits evaluation results
    with, when it emits them.
    """

    rerun_kinds: frozenset[str] = frozenset({"name"})
    key_strength: Mapping[str, int] = MappingProxyType({"session_id": 1, "conversation": 2, "name": 3})

    def __init__(self, emitter_instance_id: str | None = None):
        self.emitter_instance_id = emitter_instance_id

    def session_key(self, items: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> tuple[KeyKind, str] | None:
        return identity_key(items)

    def opens_session(self, spans: Sequence[Span]) -> bool:
        return any(classify(overlay(s)) not in (NON_GENAI, IGNORED) for s in spans)

    def log_drop_reason(self, log: LogRecord) -> str | None:
        if not log.event_name or not log.event_name.startswith("gen_ai."):
            return "not a gen_ai event"
        return None

    def is_own_record(self, log: LogRecord) -> bool:
        """An evaluation result agentevals emitted. Only evaluation results qualify: a producer may
        share this process's ``service.instance.id`` (one ``OTEL_RESOURCE_ATTRIBUTES`` for both),
        and its own telemetry must still be kept."""
        if log.event_name != EVENT_EVALUATION_RESULT:
            return False
        if log.scope.name == EMITTER_SCOPE:
            return True
        instance = log.resource.attributes.get(SERVICE_INSTANCE_ID)
        return self.emitter_instance_id is not None and instance == self.emitter_instance_id
