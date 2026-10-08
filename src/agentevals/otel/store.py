"""In memory store for live telemetry: traces, sessions, routing, completion and bounds.

Synchronous on purpose. Every public method applies its input as one unit with no suspension
point, so concurrent HTTP and gRPC exports cannot interleave inside a routing decision or a cap
check. The caller (``streaming.manager``) owns the event loop, timers and recompute; this module
only records what happened in :attr:`TelemetryStore.events`.

What the telemetry means is the :class:`RoutingPolicy`'s business (``genai.routing`` for GenAI);
this module holds only the mechanics. Routing (``otel_native_core_202610.md`` 16.2), for spans of
trace T with session key K from the policy:

* R1, membership first: a trace already in a session stays there. A session that learns a
  stronger key from its own trace (any key, for a provisional session) is promoted into the head
  session for that key. Spans reopen a complete session; logs never do.
* R2, a new trace with a key joins the head session for the key. A new SDK run id starts a new
  generation (``name-2``) at once. A complete head whose key kind splits on reruns starts a new
  generation for a rerun: a different ``service.instance.id`` when both sides carry one, otherwise
  a trace arriving once the head has been complete for the rerun window. Turns of one run that
  are further apart than the completion grace therefore stay together. Heads of other key kinds
  are always joined again.
* R3, a new trace without a key becomes a provisional session if the policy says it opens one,
  otherwise it is staged (not listed) until such a span or a key arrives for it.
"""

from __future__ import annotations

import hashlib
import heapq
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .identity import EVAL_SET_ID, METADATA_PREFIX, SESSION_RUN_ID, coerce_key
from .model import LogRecord, Span

SessionKey = tuple[str, str]

SERVICE_INSTANCE_ID = "service.instance.id"


class RoutingPolicy(Protocol):
    """What the store asks about the telemetry it routes."""

    rerun_kinds: frozenset[str]
    """Key kinds whose complete sessions split into ``name-2`` on a rerun instead of rejoining."""

    key_strength: Mapping[str, int]
    """Rank of key kinds, higher wins. A session whose key is outranked by a later batch of one of
    its traces is promoted into the head for the stronger key. Unlisted kinds rank lowest."""

    def session_key(self, items: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> SessionKey | None:
        """The key of spans or log records given as ``(attributes, resource attributes)``."""
        ...

    def opens_session(self, spans: Sequence[Span]) -> bool:
        """Whether a trace without a key is listed as a session, rather than staged."""
        ...

    def log_drop_reason(self, log: LogRecord) -> str | None:
        """Why a log record is accepted but not stored, or ``None`` to store it."""
        ...

    def is_own_record(self, log: LogRecord) -> bool:
        """A record agentevals emitted itself, refused so a pipeline loop cannot feed it back."""
        ...


REASON_TRACE_SPANS = "span(s) rejected: trace span limit reached"
REASON_SESSION_SPANS = "span(s) rejected: session span limit reached"
REASON_SESSION_TRACES = "span(s) rejected: session trace limit reached"
REASON_SPAN_MEMORY = "span(s) rejected: live store memory budget exhausted"
REASON_SPAN_SESSIONS = "span(s) rejected: session limit reached and no complete session to evict"
REASON_MERGE_REFUSED = "span(s) rejected: target session is full, merge refused"
REASON_TRACE_LOGS = "log record(s) rejected: trace log limit reached"
REASON_SESSION_LOGS = "log record(s) rejected: session log limit reached"
REASON_LOG_MEMORY = "log record(s) rejected: live store memory budget exhausted"
REASON_LOG_SESSIONS = "log record(s) rejected: session limit reached and no complete session to evict"
REASON_PENDING_FULL = "log record(s) rejected: pending log buffer full"
REASON_FEEDBACK = "log record(s) rejected: evaluation result emitted by agentevals"

# Refusals that may clear on their own (memory and session slots free up as sessions complete
# and expire), so an export refused only for these is answered as retryable overload. Per trace
# and per session caps are permanent and are reported through partial success instead.
TRANSIENT_REASONS = frozenset(
    {
        REASON_SPAN_MEMORY,
        REASON_SPAN_SESSIONS,
        REASON_LOG_MEMORY,
        REASON_LOG_SESSIONS,
        REASON_PENDING_FULL,
    }
)


@dataclass(frozen=True, slots=True)
class Limits:
    spans_per_trace: int = 10_000
    logs_per_trace: int = 5_000
    traces_per_session: int = 1_000
    spans_per_session: int = 50_000
    logs_per_session: int = 20_000
    max_sessions: int = 100
    max_bytes: int = 1024 * 1024 * 1024
    staged_traces: int = 1_000
    staged_ttl_seconds: float = 120.0
    pending_logs: int = 5_000
    pending_logs_per_trace: int = 1_000
    pending_log_bytes: int = 64 * 1024 * 1024
    pending_log_ttl_seconds: float = 300.0
    session_ttl_seconds: float = 2 * 3600.0
    completion_grace_seconds: float = 3.0
    idle_timeout_seconds: float = 30.0
    rerun_window_seconds: float = 30.0


@dataclass(slots=True)
class IngestResult:
    accepted: int = 0
    rejected: int = 0
    reasons: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)

    def reject(self, reason: str, n: int = 1) -> None:
        if n:
            self.rejected += n
            self.reasons[reason] += n

    def extend(self, other: IngestResult) -> None:
        self.accepted += other.accepted
        self.rejected += other.rejected
        self.reasons.update(other.reasons)
        self.dropped.update(other.dropped)

    @property
    def overloaded(self) -> bool:
        """Every item was refused, and only for transient capacity reasons."""
        return self.rejected > 0 and self.accepted == 0 and all(r in TRANSIENT_REASONS for r in self.reasons)


@dataclass(slots=True)
class LiveTrace:
    trace_id: str
    spans: dict[str, Span] = field(default_factory=dict)
    logs: list[LogRecord] = field(default_factory=list)
    log_keys: set[bytes] = field(default_factory=set)
    session_id: str | None = None
    version: int = 0
    nbytes: int = 0
    has_root: bool = False
    remote_roots: list[tuple[int, int]] = field(default_factory=list)
    min_start: int | None = None
    max_end: int | None = None
    last_span_at: float | None = None
    staged_at: float = 0.0
    complete: bool = False

    @property
    def has_spans(self) -> bool:
        return bool(self.spans)

    def note_span(self, span: Span) -> None:
        start, end = span.start_time_unix_nano, span.end_time_unix_nano
        self.min_start = start if self.min_start is None else min(self.min_start, start)
        self.max_end = end if self.max_end is None else max(self.max_end, end)
        if not span.parent_span_id:
            self.has_root = True
        elif span.has_remote_parent:
            self.remote_roots.append((start, end))

    @property
    def has_local_root(self) -> bool:
        """A span with no parent, or a span with a remote parent that encloses every span received
        for the trace. A remote parent span that does not is one process's share of a longer
        trace (a backend call, a harness subprocess) and ends before the trace does."""
        if self.has_root:
            return True
        return any(start <= self.min_start and end >= self.max_end for start, end in self.remote_roots)

    @property
    def start_time_unix_nano(self) -> int:
        return min((s.start_time_unix_nano for s in self.spans.values()), default=0)


@dataclass(slots=True)
class LiveSession:
    session_id: str
    key: SessionKey | None
    run_id: str | None = None
    instance_id: str | None = None
    eval_set_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_ids: list[str] = field(default_factory=list)
    is_complete: bool = False
    completed_at: datetime | None = None
    completed_mono: float | None = None
    completed_once: bool = False
    span_count: int = 0
    log_count: int = 0
    nbytes: int = 0
    last_activity: float = 0.0
    invocations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def provisional(self) -> bool:
        return self.key is None


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    trace_id: str
    version: int
    spans: tuple[Span, ...]
    logs: tuple[LogRecord, ...]
    complete: bool


def estimate_size(value: Any) -> int:
    """Rough decoded size in bytes, used for the memory budget. Iterative; never raises."""
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, (str, bytes, bytearray)):
            total += len(item) + 16
        elif isinstance(item, Mapping):
            total += 32
            for k, v in item.items():
                total += len(k) + 16 if isinstance(k, str) else 16
                stack.append(v)
        elif isinstance(item, (list, tuple)):
            total += 16 + 8 * len(item)
            stack.extend(item)
        else:
            total += 16
    return total


def span_size(span: Span) -> int:
    size = 256 + len(span.name) + estimate_size(span.attributes)
    for event in span.events:
        size += 64 + len(event.name) + estimate_size(event.attributes)
    for link in span.links:
        size += 96 + estimate_size(link.attributes)
    return size


def log_size(log: LogRecord) -> int:
    return 192 + estimate_size(log.body) + estimate_size(log.attributes)


def log_fingerprint(log: LogRecord) -> bytes:
    """Identity of a log record for retry deduplication. OTLP log records carry no id, and an
    exporter that retries after a lost response sends the same batch again."""
    material = (
        log.span_id,
        log.time_unix_nano,
        log.observed_time_unix_nano,
        log.event_name,
        log.severity_number,
        log.body,
        dict(log.attributes),
    )
    return hashlib.blake2b(repr(material).encode(), digest_size=16).digest()


def session_metadata(resource_attrs: Mapping[str, Any]) -> dict[str, Any]:
    """Resource attributes shown with the session; ``agentevals.metadata.*`` is unprefixed."""
    out: dict[str, Any] = {}
    for k, v in resource_attrs.items():
        if k.startswith(METADATA_PREFIX):
            out[k[len(METADATA_PREFIX) :]] = v
        elif not k.startswith("agentevals."):
            out[k] = v
    return out


class _PendingLogs:
    """Logs whose trace is not known yet, replayed when the trace appears. Bounded and expiring;
    a full buffer rejects the newcomer instead of evicting buffered records."""

    def __init__(self, limits: Limits):
        self._limits = limits
        self._by_trace: OrderedDict[str, list[tuple[float, LogRecord, int]]] = OrderedDict()
        self.count = 0
        self.nbytes = 0

    def add(self, trace_id: str, log: LogRecord, now: float) -> bool:
        size = log_size(log)
        bucket = self._by_trace.get(trace_id)
        if (
            self.count >= self._limits.pending_logs
            or self.nbytes + size > self._limits.pending_log_bytes
            or (bucket is not None and len(bucket) >= self._limits.pending_logs_per_trace)
        ):
            return False
        if bucket is None:
            bucket = self._by_trace[trace_id] = []
        bucket.append((now, log, size))
        self.count += 1
        self.nbytes += size
        return True

    def take(self, trace_id: str) -> list[LogRecord]:
        bucket = self._by_trace.pop(trace_id, None) or []
        self.count -= len(bucket)
        self.nbytes -= sum(size for _, _, size in bucket)
        return [log for _, log, _ in bucket]

    def expire(self, now: float) -> None:
        cutoff = now - self._limits.pending_log_ttl_seconds
        for trace_id in list(self._by_trace):
            bucket = self._by_trace[trace_id]
            kept = [entry for entry in bucket if entry[0] >= cutoff]
            if len(kept) != len(bucket):
                self.count -= len(bucket) - len(kept)
                self.nbytes -= sum(size for t, _, size in bucket if t < cutoff)
                if kept:
                    self._by_trace[trace_id] = kept
                else:
                    del self._by_trace[trace_id]


class TelemetryStore:
    def __init__(
        self,
        policy: RoutingPolicy,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.policy = policy
        self.limits = limits or Limits()
        self.clock = clock
        self.sessions: dict[str, LiveSession] = {}
        self.traces: dict[str, LiveTrace] = {}
        self.staged: OrderedDict[str, LiveTrace] = OrderedDict()
        self.pending = _PendingLogs(self.limits)
        self.heads: dict[SessionKey, str] = {}
        self.events: list[tuple] = []
        self.counters: Counter = Counter()
        self.nbytes = 0
        self._deadlines: list[tuple[float, str, str]] = []

    # ------------------------------------------------------------ lookups

    def session_of_trace(self, trace_id: str) -> LiveSession | None:
        trace = self.traces.get(trace_id)
        return self.sessions.get(trace.session_id) if trace and trace.session_id else None

    def primary_trace_id(self, session: LiveSession) -> str:
        """The earliest span bearing trace, else the first member."""
        with_spans = [self.traces[t] for t in session.trace_ids if self.traces[t].has_spans]
        if with_spans:
            return min(with_spans, key=lambda t: (t.start_time_unix_nano, t.trace_id)).trace_id
        return session.trace_ids[0] if session.trace_ids else ""

    def snapshot(self, session_id: str) -> list[TraceSnapshot]:
        session = self.sessions.get(session_id)
        if session is None:
            return []
        out = []
        for trace_id in session.trace_ids:
            trace = self.traces[trace_id]
            out.append(
                TraceSnapshot(trace_id, trace.version, tuple(trace.spans.values()), tuple(trace.logs), trace.complete)
            )
        return out

    # ------------------------------------------------------------ sessions

    def _unique_id(self, base: str) -> str:
        if base not in self.sessions:
            return base
        n = 2
        while f"{base}-{n}" in self.sessions:
            n += 1
        return f"{base}-{n}"

    def _new_session(
        self, base_id: str, key: SessionKey | None, run_id: str | None, resource: Mapping
    ) -> LiveSession | None:
        if len(self.sessions) >= self.limits.max_sessions and not self._evict_oldest_complete(exclude=None):
            return None
        session = LiveSession(
            session_id=self._unique_id(base_id),
            key=key,
            run_id=run_id,
            instance_id=coerce_key(resource.get(SERVICE_INSTANCE_ID)),
            eval_set_id=coerce_key(resource.get(EVAL_SET_ID)),
            metadata=session_metadata(resource),
            last_activity=self.clock(),
        )
        self.sessions[session.session_id] = session
        if key is not None:
            self.heads[key] = session.session_id
        self.events.append(("started", session.session_id))
        return session

    def _is_rerun(self, head: LiveSession, resource: Mapping) -> bool:
        if not head.is_complete or head.key is None or head.key[0] not in self.policy.rerun_kinds:
            return False
        instance = coerce_key(resource.get(SERVICE_INSTANCE_ID))
        if instance is not None and head.instance_id is not None:
            return instance != head.instance_id
        return self.clock() - (head.completed_mono or 0.0) >= self.limits.rerun_window_seconds

    def _outranks(self, key: SessionKey, other: SessionKey | None) -> bool:
        if other is None:
            return True
        strength = self.policy.key_strength
        return strength.get(key[0], 0) > strength.get(other[0], 0)

    def _head(self, key: SessionKey, run_id: str | None, resource: Mapping) -> LiveSession | None:
        head = self.sessions.get(self.heads.get(key, ""))
        if head is not None:
            new_run = run_id is not None and head.run_id != run_id
            if not new_run and not self._is_rerun(head, resource):
                return head
        return self._new_session(key[1], key, run_id, resource)

    def _remove_session(self, session_id: str, absorbed_by: str | None) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        for trace_id in session.trace_ids:
            trace = self.traces.get(trace_id)
            if trace is not None and trace.session_id == session_id:
                del self.traces[trace_id]
                self.nbytes -= trace.nbytes
        if session.key is not None and self.heads.get(session.key) == session_id:
            del self.heads[session.key]
        self.events.append(("removed", session_id, absorbed_by))

    def remove_session(self, session_id: str) -> None:
        self._remove_session(session_id, None)

    def _evict_oldest_complete(self, exclude: str | None) -> bool:
        candidates = [s for s in self.sessions.values() if s.is_complete and s.session_id != exclude]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda s: s.completed_mono or 0.0)
        self.counters["sessions evicted"] += 1
        self._remove_session(oldest.session_id, None)
        return True

    def _make_room(self, size: int, exclude: str | None) -> bool:
        while self.nbytes + size > self.limits.max_bytes:
            if self.staged:
                _, trace = self.staged.popitem(last=False)
                self.nbytes -= trace.nbytes
                self.counters["staged traces evicted"] += 1
            elif not self._evict_oldest_complete(exclude):
                return False
        return True

    def _attach(self, trace: LiveTrace, session: LiveSession) -> None:
        trace.session_id = session.session_id
        self.traces[trace.trace_id] = trace
        session.trace_ids.append(trace.trace_id)
        session.span_count += len(trace.spans)
        session.log_count += len(trace.logs)
        session.nbytes += trace.nbytes

    def _merge_into(self, source: LiveSession, target: LiveSession) -> bool:
        if (
            len(target.trace_ids) + len(source.trace_ids) > self.limits.traces_per_session
            or target.span_count + source.span_count > self.limits.spans_per_session
            or target.log_count + source.log_count > self.limits.logs_per_session
        ):
            return False
        moved = [self.traces[t] for t in source.trace_ids]
        source.trace_ids = []
        for trace in moved:
            self._attach(trace, target)
        self._remove_session(source.session_id, target.session_id)
        target.last_activity = self.clock()
        if target.is_complete and any(not t.complete for t in moved):
            self._reopen(target)
        self._dirty(target)
        return True

    def _reopen(self, session: LiveSession) -> None:
        session.is_complete = False
        session.completed_at = None
        session.completed_mono = None
        self.events.append(("reopened", session.session_id))

    def _dirty(self, session: LiveSession) -> None:
        self.events.append(("dirty", session.session_id))

    # ------------------------------------------------------------ spans

    def ingest_spans(self, spans: Sequence[Span]) -> IngestResult:
        """Apply spans of one trace sharing one resource (one routing unit)."""
        result = IngestResult()
        if not spans:
            return result
        now = self.clock()
        trace_id = spans[0].trace_id
        resource = spans[0].resource.attributes
        key = self.policy.session_key((s.attributes, resource) for s in spans)
        run_id = coerce_key(resource.get(SESSION_RUN_ID))

        trace = self.traces.get(trace_id)
        session = self.sessions.get(trace.session_id) if trace and trace.session_id else None
        if session is not None:
            if key is not None and self._outranks(key, session.key):
                target = self._head(key, run_id, resource)
                if target is None:
                    result.reject(REASON_SPAN_SESSIONS, len(spans))
                    return result
                if target is not session:
                    if not self._merge_into(session, target):
                        result.reject(REASON_MERGE_REFUSED, len(spans))
                        return result
                    session = target
            elif key is not None and key != session.key and not self._outranks(session.key, key):
                self.counters["session key conflicts"] += 1
        else:
            staged = self.staged.pop(trace_id, None)
            if staged is not None:
                self.nbytes -= staged.nbytes
            if key is not None:
                session = self._head(key, run_id, resource)
            elif self.policy.opens_session(spans):
                session = self._new_session(f"otlp-{trace_id[:12]}", None, None, resource)
            else:
                return self._stage(staged or LiveTrace(trace_id=trace_id), spans, now, result)
            if session is None:
                if staged is not None:
                    self._restage(staged)
                result.reject(REASON_SPAN_SESSIONS, len(spans))
                return result
            if len(session.trace_ids) >= self.limits.traces_per_session:
                if staged is not None:
                    self._restage(staged)
                result.reject(REASON_SESSION_TRACES, len(spans))
                return result
            trace = staged or LiveTrace(trace_id=trace_id)
            if staged is not None:
                self.nbytes += staged.nbytes
            self._attach(trace, session)
            for log in self.pending.take(trace_id):
                self._add_log(trace, session, log, IngestResult())

        added: list[Span] = []
        for span in spans:
            if span.span_id in trace.spans:
                result.accepted += 1
                continue
            if len(trace.spans) >= self.limits.spans_per_trace:
                result.reject(REASON_TRACE_SPANS)
                continue
            if session.span_count >= self.limits.spans_per_session:
                result.reject(REASON_SESSION_SPANS)
                continue
            size = span_size(span)
            if not self._make_room(size, exclude=session.session_id):
                result.reject(REASON_SPAN_MEMORY)
                continue
            trace.spans[span.span_id] = span
            trace.nbytes += size
            session.nbytes += size
            self.nbytes += size
            session.span_count += 1
            trace.note_span(span)
            result.accepted += 1
            added.append(span)
        if added:
            if session.is_complete:
                self._reopen(session)
            trace.version += 1
            trace.last_span_at = now
            trace.complete = False
            session.last_activity = now
            self._schedule_trace(trace)
            self.events.append(("spans", session.session_id, added))
            self._dirty(session)
        return result

    def _stage(self, trace: LiveTrace, spans: Sequence[Span], now: float, result: IngestResult) -> IngestResult:
        for span in spans:
            if span.span_id in trace.spans:
                result.accepted += 1
                continue
            if len(trace.spans) >= self.limits.spans_per_trace:
                result.reject(REASON_TRACE_SPANS)
                continue
            size = span_size(span)
            if not self._make_room(size + trace.nbytes, exclude=None):
                result.reject(REASON_SPAN_MEMORY)
                continue
            trace.spans[span.span_id] = span
            trace.nbytes += size
            trace.note_span(span)
            result.accepted += 1
        trace.staged_at = now
        trace.last_span_at = now
        trace.version += 1
        self._restage(trace)
        return result

    def _restage(self, trace: LiveTrace) -> None:
        self.staged[trace.trace_id] = trace
        self.staged.move_to_end(trace.trace_id)
        self.nbytes += trace.nbytes
        while len(self.staged) > self.limits.staged_traces:
            _, dropped = self.staged.popitem(last=False)
            self.nbytes -= dropped.nbytes
            self.counters["staged traces evicted"] += 1

    # ------------------------------------------------------------ logs

    def _add_log(self, trace: LiveTrace, session: LiveSession | None, log: LogRecord, result: IngestResult) -> bool:
        key = log_fingerprint(log)
        if key in trace.log_keys:
            result.accepted += 1
            return False
        if len(trace.logs) >= self.limits.logs_per_trace:
            result.reject(REASON_TRACE_LOGS)
            return False
        if session is not None and session.log_count >= self.limits.logs_per_session:
            result.reject(REASON_SESSION_LOGS)
            return False
        size = log_size(log)
        if not self._make_room(size, exclude=session.session_id if session else None):
            result.reject(REASON_LOG_MEMORY)
            return False
        trace.logs.append(log)
        trace.log_keys.add(key)
        trace.nbytes += size
        self.nbytes += size
        trace.version += 1
        if session is not None:
            session.log_count += 1
            session.nbytes += size
        result.accepted += 1
        return True

    def ingest_logs(self, logs: Sequence[LogRecord]) -> IngestResult:
        """Apply log records of one trace (or of no trace) sharing one resource."""
        result = IngestResult()
        kept: list[LogRecord] = []
        for log in logs:
            if self.policy.is_own_record(log):
                result.reject(REASON_FEEDBACK)
            elif reason := self.policy.log_drop_reason(log):
                result.accepted += 1
                result.dropped[reason] += 1
            elif not log.trace_id:
                result.accepted += 1
                result.dropped["no trace context"] += 1
            else:
                kept.append(log)
        self.counters.update(result.dropped)
        if not kept:
            return result

        now = self.clock()
        trace_id = kept[0].trace_id
        trace = self.traces.get(trace_id)
        session = self.sessions.get(trace.session_id) if trace and trace.session_id else None
        if trace is None and trace_id in self.staged:
            trace = self.staged[trace_id]
            for log in kept:
                self._add_log(trace, None, log, result)
            return result
        if trace is None:
            resource = kept[0].resource.attributes
            key = self.policy.session_key((log.attributes, resource) for log in kept)
            if key is None:
                for log in kept:
                    if self.pending.add(trace_id, log, now):
                        result.accepted += 1
                    else:
                        result.reject(REASON_PENDING_FULL)
                return result
            session = self._head(key, coerce_key(resource.get(SESSION_RUN_ID)), resource)
            if session is None:
                result.reject(REASON_LOG_SESSIONS, len(kept))
                return result
            trace = LiveTrace(trace_id=trace_id)
            self._attach(trace, session)
            self._schedule_session(session, now)

        before = result.accepted
        for log in kept:
            self._add_log(trace, session, log, result)
        if result.accepted > before and session is not None:
            if not session.is_complete:
                session.last_activity = now
                if not any(self.traces[t].has_spans for t in session.trace_ids):
                    self._schedule_session(session, now)
            self._dirty(session)
        return result

    # ------------------------------------------------------------ completion

    def _schedule_trace(self, trace: LiveTrace) -> None:
        delay = self.limits.completion_grace_seconds if trace.has_local_root else self.limits.idle_timeout_seconds
        heapq.heappush(self._deadlines, ((trace.last_span_at or 0.0) + delay, "trace", trace.trace_id))

    def _schedule_session(self, session: LiveSession, now: float) -> None:
        heapq.heappush(self._deadlines, (now + self.limits.idle_timeout_seconds, "session", session.session_id))

    def next_deadline(self) -> float | None:
        return self._deadlines[0][0] if self._deadlines else None

    def _trace_due(self, trace: LiveTrace) -> float:
        delay = self.limits.completion_grace_seconds if trace.has_local_root else self.limits.idle_timeout_seconds
        return (trace.last_span_at or 0.0) + delay

    def tick(self) -> list[str]:
        """Complete what is due. Returns the ids of sessions that completed in this call."""
        now = self.clock()
        touched: set[str] = set()
        while self._deadlines and self._deadlines[0][0] <= now:
            _, kind, ident = heapq.heappop(self._deadlines)
            if kind == "trace":
                trace = self.traces.get(ident)
                if trace is None or trace.complete or trace.session_id is None:
                    continue
                due = self._trace_due(trace)
                if due > now:
                    heapq.heappush(self._deadlines, (due, "trace", ident))
                    continue
                trace.complete = True
                touched.add(trace.session_id)
            else:
                session = self.sessions.get(ident)
                if session is None or session.is_complete:
                    continue
                if any(self.traces[t].has_spans for t in session.trace_ids):
                    continue
                due = session.last_activity + self.limits.idle_timeout_seconds
                if due > now:
                    heapq.heappush(self._deadlines, (due, "session", ident))
                    continue
                touched.add(ident)

        completed = []
        for session_id in touched:
            session = self.sessions.get(session_id)
            if session is None or session.is_complete:
                continue
            bearing = [self.traces[t] for t in session.trace_ids if self.traces[t].has_spans]
            if all(t.complete for t in bearing):
                session.is_complete = True
                session.completed_at = datetime.now(UTC)
                session.completed_mono = now
                completed.append(session_id)
        return completed

    def expire(self) -> None:
        """Drop expired staged traces, pending logs and sessions past their TTL."""
        now = self.clock()
        self.pending.expire(now)
        cutoff = now - self.limits.staged_ttl_seconds
        while self.staged:
            trace_id, trace = next(iter(self.staged.items()))
            if trace.staged_at >= cutoff:
                break
            del self.staged[trace_id]
            self.nbytes -= trace.nbytes
        session_cutoff = now - self.limits.session_ttl_seconds
        for session in list(self.sessions.values()):
            if session.is_complete and (session.completed_mono or now) < session_cutoff:
                self._remove_session(session.session_id, None)

    # ------------------------------------------------------------ debug load

    def load_session(
        self,
        session_id: str,
        spans: Iterable[Span],
        logs: Iterable[LogRecord],
        *,
        eval_set_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LiveSession | None:
        """Insert a complete session as is, bypassing key routing (bug report replay)."""
        if len(self.sessions) >= self.limits.max_sessions and not self._evict_oldest_complete(exclude=None):
            return None
        now = self.clock()
        session = LiveSession(
            session_id=self._unique_id(session_id),
            key=None,
            eval_set_id=eval_set_id,
            metadata=dict(metadata or {}),
            last_activity=now,
        )
        by_trace: dict[str, LiveTrace] = {}
        for span in spans:
            trace = by_trace.setdefault(span.trace_id, LiveTrace(trace_id=span.trace_id))
            if span.span_id in trace.spans or len(trace.spans) >= self.limits.spans_per_trace:
                continue
            trace.spans[span.span_id] = span
            trace.nbytes += span_size(span)
            trace.note_span(span)
        for log in logs:
            if not log.trace_id:
                continue
            trace = by_trace.setdefault(log.trace_id, LiveTrace(trace_id=log.trace_id))
            if len(trace.logs) < self.limits.logs_per_trace:
                trace.logs.append(log)
                trace.nbytes += log_size(log)
        size = sum(t.nbytes for t in by_trace.values())
        if not self._make_room(size, exclude=None):
            return None
        self.sessions[session.session_id] = session
        for trace in by_trace.values():
            if trace.trace_id in self.traces:
                continue
            trace.complete = True
            trace.last_span_at = now
            trace.version = 1
            self._attach(trace, session)
            self.nbytes += trace.nbytes
        session.is_complete = True
        session.completed_once = True
        session.completed_at = datetime.now(UTC)
        session.completed_mono = now
        self.events.append(("started", session.session_id))
        return session
