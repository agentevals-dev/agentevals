"""Live session manager: ingest into the store, completion ticker, recompute and the SSE diff.

The store is synchronous; this module owns the event loop side. Exports are applied one routing
unit at a time, yielding the loop on a time budget so a large export never holds it. Conversations
are recomputed on frozen snapshots in a worker thread, with turns memoized per trace version, and
the result is diffed against what each session already sent. The diff only appends: every live
element has an identity, so a span arriving late never repeats or retracts an element. After a
session has completed once, only ``session_complete`` is sent for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..api.models import (
    SessionInfo,
    WSSessionCompleteEvent,
    WSSessionRemovedEvent,
    WSSessionStartedEvent,
)
from ..genai.extract import conversation_from_turns, extract_turns
from ..genai.grouping import SESSION_NAME
from ..genai.messages import has_text, text_of, user_turn_messages
from ..genai.model import Conversation, Turn
from ..otel.decode import DecodeResult
from ..otel.encode import encode_traces
from ..otel.model import LogRecord, Span, SpanRef, Trace, attr_float, attr_int, build_traces
from ..otel.store import IngestResult, Limits, LiveSession, TelemetryStore, TraceSnapshot

logger = logging.getLogger(__name__)

YIELD_INTERVAL_SECONDS = 0.01
SSE_QUEUE_SIZE = 1_000
MAX_EMITTED_KEYS = 10_000
DEBOUNCE_MIN_SECONDS = 0.25
DEBOUNCE_MAX_SECONDS = 5.0
DEBOUNCE_MAX_WAIT_SECONDS = 1.0
EXPIRE_INTERVAL_SECONDS = 10.0
MAX_TICK_SECONDS = 1.0
# A recompute can produce thousands of elements at once; handing the loop back between batches
# lets SSE writers drain, so the queue bound drops only clients that are really behind.
BROADCAST_BATCH = 100


def _resource_groups(items: Iterable[Any]) -> list[list[Any]]:
    """Split decoded spans or logs into routing units: one trace under one resource, in arrival order."""
    groups: dict[tuple[int, str | None], list[Any]] = {}
    for item in items:
        groups.setdefault((id(item.resource), item.trace_id), []).append(item)
    return list(groups.values())


def _span_stub(span: Span) -> dict[str, Any]:
    return {"traceId": span.trace_id, "spanId": span.span_id, "parentSpanId": span.parent_span_id, "name": span.name}


def model_info(turn: Turn, spans: dict[tuple[str, str], Span]) -> dict[str, Any]:
    """Per turn model metadata; usage counts each logical call once."""
    info: dict[str, Any] = {}
    calls = turn.llm_calls
    usage = turn.usage
    leaf = spans.get((calls[0].ref.trace_id, calls[0].ref.span_id)) if calls else None
    for key, value in (
        ("models", sorted({c.request_model for c in calls if c.request_model})),
        ("inputTokens", usage.input_tokens),
        ("outputTokens", usage.output_tokens),
        ("provider", next((c.provider for c in calls if c.provider), None)),
        ("responseModels", sorted({c.response_model for c in calls if c.response_model})),
        ("finishReasons", sorted({r for c in calls for r in c.finish_reasons})),
        ("cacheCreationTokens", usage.cache_write_input_tokens),
        ("cacheReadTokens", usage.cache_read_input_tokens),
        ("temperature", attr_float(leaf.attributes, "gen_ai.request.temperature") if leaf else None),
        ("maxTokens", attr_int(leaf.attributes, "gen_ai.request.max_tokens") if leaf else None),
        ("errorTypes", sorted({c.error_type for c in calls if c.error_type})),
    ):
        if value:
            info[key] = value
    return info


def invocation_dict(turn: Turn, spans: dict[tuple[str, str], Span]) -> dict[str, Any]:
    """A turn as the ``session_complete`` invocation the live UI renders."""
    return {
        "invocationId": turn.ref.span_id,
        "traceId": turn.ref.trace_id,
        "userText": text_of(turn.user_input) or "",
        "agentText": text_of(turn.final_output) or "",
        "toolCalls": [{"name": t.name, "args": t.arguments or {}, "id": t.call_id} for t in turn.tool_calls],
        "toolResponses": [
            {"name": t.name, "response": t.result, "id": t.call_id} for t in turn.tool_calls if t.result is not None
        ],
        "modelInfo": model_info(turn, spans),
        "externalEvaluations": [
            {
                "name": e.name,
                "scoreValue": e.score_value,
                "scoreLabel": e.score_label,
                "explanation": e.explanation,
                "spanId": e.ref.span_id,
                "sourceService": e.source_service,
            }
            for e in turn.external_evaluations
        ],
    }


@dataclass(slots=True)
class LiveState:
    """What one session already sent over SSE."""

    keys: set = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0

    def seen(self, key: Any) -> bool:
        return key in self.keys

    def mark(self, *keys: Any) -> bool:
        """Record keys; False when the cap is reached (the element is then left to ``session_complete``)."""
        if len(self.keys) + len(keys) > MAX_EMITTED_KEYS:
            return False
        self.keys.update(keys)
        return True


def _ref_key(kind: str, ref: SpanRef | None) -> tuple | None:
    return (kind, ref.trace_id, ref.span_id) if ref else None


def live_events(session_id: str, conversation: Conversation, state: LiveState, incomplete: set[str]) -> list[dict]:
    """Elements of ``conversation`` not sent yet, as the SSE events the live UI consumes."""
    events: list[dict] = []
    last_invocation = None
    for turn in conversation.turns:
        invocation = turn.ref.trace_id if turn.ref.trace_id in incomplete else turn.ref.span_id
        last_invocation = invocation
        base = {"sessionId": session_id, "invocationId": invocation}

        text = text_of(turn.user_input)
        if text:
            source = next((c for c in turn.llm_calls if user_turn_messages(c.input_messages)), None)
            keys = [("user-text", turn.ref.trace_id, text), _ref_key("user", turn.ref)]
            if source is not None:
                keys += [_ref_key("user", source.ref), _ref_key("user", source.wrapper_ref)]
            keys = [k for k in keys if k]
            if not any(state.seen(k) for k in keys) and state.mark(*keys):
                events.append({"type": "user_input", **base, "text": text, "timestamp": turn.start_ns / 1e9})

        spoke = False
        for call in turn.llm_calls:
            if call.delegated or not has_text(call.output_messages):
                continue
            spoke = True
            keys = [k for k in (_ref_key("agent", call.ref), _ref_key("agent", call.wrapper_ref)) if k]
            if any(state.seen(k) for k in keys) or not state.mark(*keys):
                continue
            events.append(
                {
                    "type": "agent_response",
                    **base,
                    "text": text_of(call.output_messages) or "",
                    "timestamp": call.end_ns / 1e9,
                }
            )
        final = text_of(turn.final_output)
        if not spoke and final:
            key = _ref_key("agent-final", turn.ref)
            if not state.seen(key) and state.mark(key):
                events.append({"type": "agent_response", **base, "text": final, "timestamp": turn.end_ns / 1e9})

        for tool in turn.tool_calls:
            ident = tool.call_id or (tool.ref.span_id if tool.ref else f"{tool.name}@{tool.start_ns}")
            if not state.seen(("tool", ident)) and state.mark(("tool", ident)):
                events.append(
                    {
                        "type": "tool_call",
                        **base,
                        "toolCall": {"id": tool.call_id or ident, "name": tool.name, "args": tool.arguments or {}},
                        "timestamp": (tool.start_ns or turn.start_ns) / 1e9,
                    }
                )
            if tool.result is not None and not state.seen(("result", ident)) and state.mark(("result", ident)):
                events.append(
                    {
                        "type": "tool_result",
                        **base,
                        "toolCallId": tool.call_id or ident,
                        "toolName": tool.name,
                        "response": tool.result,
                        "isError": tool.is_error,
                        "timestamp": (tool.start_ns or turn.end_ns) / 1e9,
                    }
                )

    total_in = sum(t.usage.input_tokens for t in conversation.turns)
    total_out = sum(t.usage.output_tokens for t in conversation.turns)
    if total_in != state.input_tokens or total_out != state.output_tokens:
        model = next(
            (c.response_model or c.request_model for t in reversed(conversation.turns) for c in reversed(t.llm_calls)),
            None,
        )
        events.append(
            {
                "type": "token_update",
                "sessionId": session_id,
                "invocationId": last_invocation,
                "inputTokens": total_in - state.input_tokens,
                "outputTokens": total_out - state.output_tokens,
                "model": model,
            }
        )
        state.input_tokens, state.output_tokens = total_in, total_out
    return events


class SseClient:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self.dropped = False


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
        instance_id: str | None = None,
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
        limits = dataclasses.replace(limits or Limits(), **overrides)
        self.store = TelemetryStore(limits, instance_id=instance_id)
        self.clients: list[SseClient] = []
        self._states: dict[str, LiveState] = {}
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
        for client in list(self.clients):
            self._close(client)
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

    # ------------------------------------------------------------ SSE

    def register_sse_client(self) -> SseClient:
        client = SseClient()
        self.clients.append(client)
        return client

    def unregister_sse_client(self, client: SseClient) -> None:
        if client in self.clients:
            self.clients.remove(client)

    def _close(self, client: SseClient) -> None:
        self.unregister_sse_client(client)
        while not client.queue.empty():
            client.queue.get_nowait()
        client.queue.put_nowait(None)

    def broadcast(self, event: dict) -> None:
        """Queue an event for every client. A client whose queue is full is dropped; the UI
        reconnects and refetches the session list."""
        for client in list(self.clients):
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                client.dropped = True
                logger.warning("SSE client fell behind by %d events; disconnecting it", SSE_QUEUE_SIZE)
                self._close(client)

    # ------------------------------------------------------------ views

    def session_info(self, session: LiveSession, *, with_invocations: bool = False) -> SessionInfo:
        return SessionInfo(
            session_id=session.session_id,
            trace_id=self.store.primary_trace_id(session),
            eval_set_id=session.eval_set_id,
            span_count=session.span_count,
            is_complete=session.is_complete,
            started_at=session.started_at.isoformat(),
            metadata=session.metadata,
            invocations=session.invocations if with_invocations and session.is_complete else None,
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
                    self._states[session_id] = LiveState()
                    self.broadcast(WSSessionStartedEvent(session=self.session_info(session)).model_dump(by_alias=True))
            elif kind == "removed":
                self._forget(session_id)
                self.broadcast(
                    WSSessionRemovedEvent(session_id=session_id, absorbed_by=event[2]).model_dump(by_alias=True)
                )
            elif kind == "spans":
                stubs = [_span_stub(s) for s in event[2]]
                self.broadcast({"type": "span_received", "sessionId": session_id, "span": stubs[0], "spans": stubs})
            elif kind == "dirty":
                pending = self._pending.get(session_id)
                if pending is None:
                    self._pending[session_id] = _Pending(first=now, last=now)
                else:
                    pending.last = now
            elif kind == "reopened":
                logger.info("Session %s reopened by new spans", session_id)

    def _forget(self, session_id: str) -> None:
        self._states.pop(session_id, None)
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
            snapshots = self.store.snapshot(session_id)
            started = time.monotonic()
            conversation, spans = await asyncio.to_thread(self._conversation, session_id, snapshots)
            invocations = await asyncio.to_thread(lambda: [invocation_dict(t, spans) for t in conversation.turns])
            self._durations[session_id] = time.monotonic() - started
            session = self.store.sessions.get(session_id)
            if session is None:
                return
            session.invocations = invocations
            if not session.completed_once:
                state = self._states.setdefault(session_id, LiveState())
                incomplete = {s.trace_id for s in snapshots if not s.complete}
                for n, event in enumerate(live_events(session_id, conversation, state, incomplete), start=1):
                    self.broadcast(event)
                    if n % BROADCAST_BATCH == 0:
                        await asyncio.sleep(0)
            if session.is_complete and (completed_now or session.completed_once):
                session.completed_once = True
                logger.info(
                    "Session complete: %s (%d spans, %d logs, %d turns)",
                    session_id,
                    session.span_count,
                    session.log_count,
                    len(invocations),
                )
                self.broadcast(
                    WSSessionCompleteEvent(session_id=session_id, invocations=invocations).model_dump(by_alias=True)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to recompute session %s", session_id)
        finally:
            if self._running.get(session_id) is asyncio.current_task():
                del self._running[session_id]
            if session_id in self._pending:
                self._poke()

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
