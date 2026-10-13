"""Live session manager: ingest into the store, completion ticker, recompute and the watchers.

The store is synchronous; this module owns the event loop side. Exports are applied one routing
unit at a time, yielding the loop on a time budget so a large export never holds it. Conversations
are recomputed on frozen snapshots in a worker thread, with turns memoized per trace version.

Each recompute builds the session's view (``api.views``) in the same thread and publishes it to
every watcher: the gRPC ``WatchSessions`` stream and the UI's SSE stream. A watcher keeps only the
latest pending event per session, so it never holds more than the session limit and a slow reader
is never dropped: it skips intermediate states.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from google.protobuf import json_format

from ..api.models import SessionInfo
from ..api.v1alpha1 import sessions_pb2
from ..api.views import SessionFacts, session_view
from ..genai.extract import conversation_from_turns, extract_turns
from ..genai.grouping import SESSION_NAME
from ..genai.model import Conversation, Turn
from ..genai.routing import GenAIRoutingPolicy
from ..otel.decode import DecodeResult
from ..otel.encode import encode_traces
from ..otel.model import LogRecord, Span, Trace, build_traces
from ..otel.store import IngestResult, Limits, LiveSession, TelemetryStore, TraceSnapshot

logger = logging.getLogger(__name__)

YIELD_INTERVAL_SECONDS = 0.01
DEBOUNCE_MIN_SECONDS = 0.25
DEBOUNCE_MAX_SECONDS = 5.0
DEBOUNCE_MAX_WAIT_SECONDS = 1.0
EXPIRE_INTERVAL_SECONDS = 10.0
MAX_TICK_SECONDS = 1.0

MAX_BYTES_ENV = "AGENTEVALS_LIVE_MAX_BYTES"
MAX_WATCHERS = 32
_SYNCED_KEY = ""


def _max_bytes_from_env() -> int | None:
    """The live store memory budget in bytes, from ``AGENTEVALS_LIVE_MAX_BYTES``."""
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"{MAX_BYTES_ENV} must be a positive number of bytes (got: {raw!r})")
    return int(raw)


def _resource_groups(items: Iterable[Any]) -> list[list[Any]]:
    """Split decoded spans or logs into routing units: one trace under one resource, in arrival order."""
    groups: dict[tuple[int, str | None], list[Any]] = {}
    for item in items:
        groups.setdefault((id(item.resource), item.trace_id), []).append(item)
    return list(groups.values())


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """One ``WatchSessionsResponse`` in the forms watchers send. Shared and never mutated."""

    with_turns: sessions_pb2.WatchSessionsResponse
    summary_only: sessions_pb2.WatchSessionsResponse
    json: str

    @classmethod
    def of(cls, with_turns: sessions_pb2.WatchSessionsResponse, summary_only=None) -> WatchEvent:
        return cls(with_turns, summary_only or with_turns, json_format.MessageToJson(with_turns, indent=None))

    @classmethod
    def upsert(cls, session) -> WatchEvent:
        summary_only = sessions_pb2.WatchSessionsResponse()
        summary_only.upsert.summary.CopyFrom(session.summary)
        return cls.of(sessions_pb2.WatchSessionsResponse(upsert=session), summary_only)


class TooManyWatchers(RuntimeError):
    pass


class Watcher:
    """One subscriber: the latest pending event per session, in the order they changed."""

    def __init__(self, include_turns: bool) -> None:
        self.include_turns = include_turns
        self._pending: OrderedDict[str, WatchEvent] = OrderedDict()
        self._wake = asyncio.Event()
        self.closed = False

    def put(self, key: str, event: WatchEvent) -> None:
        self._pending.pop(key, None)
        self._pending[key] = event
        self._wake.set()

    def close(self) -> None:
        self.closed = True
        self._wake.set()

    async def next(self) -> WatchEvent | None:
        """The next event, or ``None`` once the watcher is closed."""
        while not self._pending:
            if self.closed:
                return None
            self._wake.clear()
            await self._wake.wait()
        return self._pending.popitem(last=False)[1]

    def response(self, event: WatchEvent) -> sessions_pb2.WatchSessionsResponse:
        return event.with_turns if self.include_turns else event.summary_only


def session_facts(session: LiveSession) -> SessionFacts:
    return SessionFacts(
        session_id=session.session_id,
        received_at=session.started_at,
        is_complete=session.is_complete,
        trace_ids=tuple(session.trace_ids),
        span_count=session.span_count,
        eval_set_id=session.eval_set_id,
    )


_SYNCED = WatchEvent.of(sessions_pb2.WatchSessionsResponse(synced=sessions_pb2.Synced()))


@dataclass(slots=True)
class _Pending:
    first: float
    last: float
    complete: bool = False


class LiveManager:
    """Live ingestion, sessions and UI updates on top of :class:`TelemetryStore`."""

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        completion_grace_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        rerun_window_seconds: float | None = None,
        emitter_instance_id: str | None = None,
    ):
        overrides = {
            name: value
            for name, value in (
                ("completion_grace_seconds", completion_grace_seconds),
                ("idle_timeout_seconds", idle_timeout_seconds),
                ("rerun_window_seconds", rerun_window_seconds),
            )
            if value is not None
        }
        if limits is None and "max_bytes" not in overrides:
            budget = _max_bytes_from_env()
            if budget is not None:
                overrides["max_bytes"] = budget
        limits = dataclasses.replace(limits or Limits(), **overrides)
        self.store = TelemetryStore(GenAIRoutingPolicy(emitter_instance_id), limits)
        self.watchers: list[Watcher] = []
        self._views: dict[str, WatchEvent] = {}
        self._pending: dict[str, _Pending] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._durations: dict[str, float] = {}
        self._memo: OrderedDict[str, tuple[int, Trace | None, list[Turn]]] = OrderedDict()
        self._memo_lock = threading.Lock()
        self._wake: asyncio.Event | None = None
        self._ticker: asyncio.Task | None = None
        self._last_expire = 0.0

    # ------------------------------------------------------------ lifecycle

    @property
    def sessions(self) -> dict[str, LiveSession]:
        return self.store.sessions

    def start(self) -> None:
        if self._ticker is None:
            self._wake = asyncio.Event()
            self._ticker = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        self.close_watchers()
        tasks = [t for t in (self._ticker, *self._running.values()) if t is not None]
        self._ticker = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    def _poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------ watchers

    def subscribe(self, *, include_turns: bool) -> Watcher:
        """A watcher holding every current session, newest first, then ``synced``."""
        if len(self.watchers) >= MAX_WATCHERS:
            raise TooManyWatchers(f"at most {MAX_WATCHERS} session watchers")
        watcher = Watcher(include_turns)
        for session in sorted(self.store.sessions.values(), key=lambda s: s.started_at, reverse=True):
            watcher.put(session.session_id, self.view(session))
        watcher.put(_SYNCED_KEY, _SYNCED)
        self.watchers.append(watcher)
        return watcher

    def close_watchers(self) -> None:
        for watcher in list(self.watchers):
            self.unsubscribe(watcher)

    def unsubscribe(self, watcher: Watcher) -> None:
        if watcher in self.watchers:
            self.watchers.remove(watcher)
        watcher.close()

    def view(self, session: LiveSession) -> WatchEvent:
        """The session's latest view; a summary from the store until its first recompute."""
        cached = self._views.get(session.session_id)
        return cached if cached is not None else WatchEvent.upsert(session_view(session_facts(session)))

    def _publish(self, session_id: str, event: WatchEvent) -> None:
        for watcher in self.watchers:
            watcher.put(session_id, event)

    def _publish_removed(self, session_id: str, absorbed_by: str | None) -> None:
        removed = sessions_pb2.SessionRemoved(id=session_id, absorbed_by=absorbed_by or "")
        self._publish(session_id, WatchEvent.of(sessions_pb2.WatchSessionsResponse(removed=removed)))

    # ------------------------------------------------------------ views

    def session_info(self, session: LiveSession) -> SessionInfo:
        return SessionInfo(
            session_id=session.session_id,
            trace_id=self.store.primary_trace_id(session),
            eval_set_id=session.eval_set_id,
            span_count=session.span_count,
            is_complete=session.is_complete,
            started_at=session.started_at.isoformat(),
            metadata=session.metadata,
        )

    def session_traces(self, session: LiveSession | str) -> list[Trace]:
        session_id = session if isinstance(session, str) else session.session_id
        spans: list[Span] = []
        logs: list[LogRecord] = []
        for snap in self.store.snapshot(session_id):
            spans.extend(snap.spans)
            logs.extend(snap.logs)
        traces, _ = build_traces(spans, logs)
        return traces

    def session_document(self, session: LiveSession | str) -> dict:
        """The session as one OTLP/JSON document, every resource stamped with ``agentevals.session_name``."""
        session_id = session if isinstance(session, str) else session.session_id
        document = encode_traces(self.session_traces(session_id))
        stamp = {"key": SESSION_NAME, "value": {"stringValue": session_id}}
        for group in (*document.get("resourceSpans", []), *document.get("resourceLogs", [])):
            attrs = group.setdefault("resource", {}).setdefault("attributes", [])
            if not any(a.get("key") == SESSION_NAME for a in attrs):
                attrs.append(stamp)
        return document

    # ------------------------------------------------------------ ingest

    async def ingest_spans(self, decoded: DecodeResult) -> IngestResult:
        return await self._ingest(decoded.spans, self.store.ingest_spans)

    async def ingest_logs(self, decoded: DecodeResult) -> IngestResult:
        return await self._ingest(decoded.logs, self.store.ingest_logs)

    async def _ingest(self, items: Sequence[Any], apply) -> IngestResult:
        result = IngestResult()
        last_yield = time.monotonic()
        for group in _resource_groups(items):
            if time.monotonic() - last_yield >= YIELD_INTERVAL_SECONDS:
                await asyncio.sleep(0)
                last_yield = time.monotonic()
            result.extend(apply(group))
            self._drain()
        self._poke()
        return result

    def load_session(self, session_id: str, spans, logs, *, eval_set_id=None, metadata=None) -> LiveSession | None:
        session = self.store.load_session(session_id, spans, logs, eval_set_id=eval_set_id, metadata=metadata)
        self._drain()
        if session is not None:
            self._request(session.session_id, complete=True)
            self._poke()
        return session

    def _drain(self) -> None:
        events, self.store.events = self.store.events, []
        now = time.monotonic()
        for event in events:
            kind, session_id = event[0], event[1]
            if kind == "started":
                session = self.store.sessions.get(session_id)
                if session is not None:
                    self._publish(session_id, self.view(session))
            elif kind == "removed":
                self._forget(session_id)
                self._publish_removed(session_id, event[2])
            elif kind == "dirty":
                pending = self._pending.get(session_id)
                if pending is None:
                    self._pending[session_id] = _Pending(first=now, last=now)
                else:
                    pending.last = now
            elif kind == "reopened":
                logger.info("Session %s reopened by new spans", session_id)

    def _forget(self, session_id: str) -> None:
        self._views.pop(session_id, None)
        self._pending.pop(session_id, None)
        self._durations.pop(session_id, None)
        task = self._running.pop(session_id, None)
        if task is not None:
            task.cancel()
        live = set(self.store.traces)
        with self._memo_lock:
            for trace_id in [t for t in self._memo if t not in live]:
                del self._memo[trace_id]

    def _request(self, session_id: str, *, complete: bool = False) -> None:
        now = time.monotonic()
        pending = self._pending.setdefault(session_id, _Pending(first=now, last=now))
        if complete:
            pending.complete = True

    # ------------------------------------------------------------ ticker and recompute

    def _due(self, session_id: str, pending: _Pending) -> float:
        if pending.complete:
            return pending.first
        debounce = min(max(4 * self._durations.get(session_id, 0.0), DEBOUNCE_MIN_SECONDS), DEBOUNCE_MAX_SECONDS)
        return min(pending.last + debounce, pending.first + DEBOUNCE_MAX_WAIT_SECONDS)

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                now = time.monotonic()
                wake_at = [now + MAX_TICK_SECONDS]
                deadline = self.store.next_deadline()
                if deadline is not None:
                    wake_at.append(deadline)
                wake_at += [self._due(s, p) for s, p in self._pending.items() if s not in self._running]
                timeout = max(0.0, min(wake_at) - now)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout)
                self._wake.clear()
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live session ticker failed; continuing")

    def tick(self) -> None:
        """Complete what is due and start the recomputes that are due. Called by the ticker."""
        for session_id in self.store.tick():
            self._request(session_id, complete=True)
        now = time.monotonic()
        if now - self._last_expire >= EXPIRE_INTERVAL_SECONDS:
            self._last_expire = now
            self.store.expire()
        self._drain()
        for session_id, pending in list(self._pending.items()):
            if session_id in self._running or self._due(session_id, pending) > now:
                continue
            del self._pending[session_id]
            if session_id not in self.store.sessions:
                continue
            task = asyncio.create_task(self._recompute(session_id, pending.complete))
            self._running[session_id] = task

    async def _recompute(self, session_id: str, completed_now: bool) -> None:
        try:
            live = self.store.sessions.get(session_id)
            if live is None:
                return
            facts = session_facts(live)
            snapshots = self.store.snapshot(session_id)
            started = time.monotonic()
            view, turns = await asyncio.to_thread(self._view, session_id, facts, snapshots)
            self._durations[session_id] = time.monotonic() - started
            if self.store.sessions.get(session_id) is not live:
                return
            self._views[session_id] = view
            self._publish(session_id, view)
            if live.is_complete and (completed_now or live.completed_once):
                if not live.completed_once:
                    logger.info(
                        "Session complete: %s (%d spans, %d logs, %d turns)",
                        session_id,
                        live.span_count,
                        live.log_count,
                        turns,
                    )
                live.completed_once = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to recompute session %s", session_id)
        finally:
            if self._running.get(session_id) is asyncio.current_task():
                del self._running[session_id]
            if session_id in self._pending:
                self._poke()

    def _view(self, session_id: str, facts: SessionFacts, snapshots: list[TraceSnapshot]) -> tuple[WatchEvent, int]:
        """The session's view and turn count. Runs in a worker thread, on copied facts and snapshots."""
        conversation, spans = self._conversation(session_id, snapshots)
        return WatchEvent.upsert(session_view(facts, conversation, spans)), len(conversation.turns)

    def _conversation(
        self, session_id: str, snapshots: list[TraceSnapshot]
    ) -> tuple[Conversation, dict[tuple[str, str], Span]]:
        traces: list[Trace] = []
        turns: list[list[Turn]] = []
        for snap in snapshots:
            with self._memo_lock:
                cached = self._memo.get(snap.trace_id)
            if cached is None or cached[0] != snap.version:
                built, _ = build_traces(snap.spans, snap.logs)
                trace = built[0] if built else None
                cached = (snap.version, trace, extract_turns(trace) if trace else [])
                with self._memo_lock:
                    self._memo[snap.trace_id] = cached
            if cached[1] is not None:
                traces.append(cached[1])
                turns.append(cached[2])
        spans = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
        return conversation_from_turns(traces, turns, session_id), spans
