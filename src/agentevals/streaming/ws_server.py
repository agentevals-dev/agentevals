"""WebSocket server for streaming OTel spans from agents."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..api.models import (
    SessionInfo,
    WSSessionCompleteEvent,
    WSSessionRemovedEvent,
    WSSessionStartedEvent,
    WSSpanReceivedEvent,
)
from ..genai.extract import extract_conversation
from ..genai.grouping import SESSION_NAME
from ..genai.messages import text_of
from ..genai.model import Turn
from ..otel.decode import decode_bare_spans_json
from ..otel.encode import encode_traces
from ..otel.model import (
    EMPTY_SCOPE,
    LogRecord,
    Resource,
    Scope,
    Trace,
    attr_float,
    attr_int,
    build_traces,
)
from ..trace_attrs import OTEL_SERVICE_NAME
from .exports import EXPORT_DIR, export_name
from .incremental_processor import IncrementalInvocationExtractor
from .session import TraceSession

logger = logging.getLogger(__name__)


class StreamingTraceManager:
    """Manages active trace sessions from WebSocket clients.

    Args:
        session_ttl_hours: How long to keep completed sessions in memory (default: 2 hours)
        max_sessions: Maximum number of sessions to keep (default: 100)
        completion_grace_seconds: Delay after root span before completing session (default: 3.0)
        idle_timeout_seconds: Complete session after this many seconds of inactivity (default: 30.0)
        reextraction_delay_seconds: Debounce delay for late-log re-extraction (default: 2.0)
    """

    def __init__(
        self,
        session_ttl_hours: int = 2,
        max_sessions: int = 100,
        completion_grace_seconds: float = 3.0,
        idle_timeout_seconds: float = 30.0,
        reextraction_delay_seconds: float = 2.0,
    ):
        self.sessions: dict[str, TraceSession] = {}
        self.incremental_extractors: dict[str, IncrementalInvocationExtractor] = {}
        self.sse_queues: list[asyncio.Queue] = []
        self.session_ttl = timedelta(hours=session_ttl_hours)
        self.max_sessions = max_sessions
        self.completion_grace_seconds = completion_grace_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.reextraction_delay_seconds = reextraction_delay_seconds
        self._cleanup_task: asyncio.Task | None = None
        self._completion_timers: dict[str, asyncio.Task] = {}
        self._idle_timers: dict[str, asyncio.Task] = {}
        self._orphan_logs: list[dict] = []
        self._orphan_log_max_age = timedelta(seconds=60)
        self._active_session_for_name: dict[str, str] = {}

    def register_sse_client(self) -> asyncio.Queue:
        """Register a new SSE client and return its queue."""
        queue: asyncio.Queue = asyncio.Queue()
        self.sse_queues.append(queue)
        return queue

    def unregister_sse_client(self, queue: asyncio.Queue) -> None:
        """Unregister an SSE client."""
        if queue in self.sse_queues:
            self.sse_queues.remove(queue)

    def start_cleanup_task(self) -> None:
        """Start the background task for cleaning up old sessions."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_old_sessions_loop())
            logger.info("Started session cleanup task (TTL: %s, max: %d)", self.session_ttl, self.max_sessions)

    async def shutdown(self) -> None:
        """Gracefully shut down: close SSE clients and cancel background tasks."""
        for queue in self.sse_queues:
            queue.put_nowait(None)
        pending = list(self._completion_timers.values()) + list(self._idle_timers.values())
        if self._cleanup_task:
            pending.append(self._cleanup_task)
            self._cleanup_task = None
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._completion_timers.clear()
        self._idle_timers.clear()

    async def _cleanup_old_sessions_loop(self) -> None:
        """Periodically clean up old sessions to prevent memory leak."""
        while True:
            try:
                await asyncio.sleep(3600)
                removed_count = self._cleanup_old_sessions()
                if removed_count > 0:
                    logger.info("Cleaned up %d old sessions", removed_count)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error in cleanup task: %s", exc)

    def _cleanup_old_sessions(self) -> int:
        """Remove sessions older than TTL or enforce max session limit.

        Returns:
            Number of sessions removed
        """
        now = datetime.now(UTC)
        to_remove = []

        for session_id, session in self.sessions.items():
            age = now - session.started_at
            if session.is_complete and age > self.session_ttl:
                to_remove.append(session_id)

        if len(self.sessions) - len(to_remove) > self.max_sessions:
            sorted_sessions = sorted(
                [(sid, s) for sid, s in self.sessions.items() if s.is_complete and sid not in to_remove],
                key=lambda x: x[1].started_at,
            )
            excess_count = len(self.sessions) - len(to_remove) - self.max_sessions
            for i in range(min(excess_count, len(sorted_sessions))):
                to_remove.append(sorted_sessions[i][0])

        for session_id in to_remove:
            del self.sessions[session_id]
            if session_id in self.incremental_extractors:
                del self.incremental_extractors[session_id]
            for key in (session_id, f"_reextract_{session_id}"):
                if key in self._completion_timers:
                    self._completion_timers.pop(key).cancel()
            if session_id in self._idle_timers:
                self._idle_timers.pop(session_id).cancel()
            logger.debug("Removed old session: %s", session_id)

        cutoff = now - self._orphan_log_max_age
        self._orphan_logs = [e for e in self._orphan_logs if e["buffered_at"] >= cutoff]

        return len(to_remove)

    async def broadcast_to_ui(self, event: dict) -> None:
        """Broadcast event to all connected SSE clients."""
        for queue in self.sse_queues:
            try:
                await queue.put(event)
            except Exception as exc:
                logger.warning("Failed to broadcast to SSE client: %s", exc)

    def buffer_orphan_log(self, trace_id: str, session_name: str | None, log_event: dict) -> None:
        """Buffer a log event that arrived before its session was created.

        OTLP BatchLogRecordProcessor and BatchSpanProcessor flush independently.
        Logs may arrive at /v1/logs before the first span arrives at /v1/traces,
        at which point no session exists yet. These orphan logs are buffered and
        replayed when the matching session is created.
        """
        self._orphan_logs.append(
            {
                "trace_id": trace_id,
                "session_name": session_name,
                "log_event": log_event,
                "buffered_at": datetime.now(UTC),
            }
        )

    def _replay_orphan_logs(self, session: TraceSession) -> list[dict]:
        """Replay buffered orphan logs that match the given session.

        Returns the replayed log events for further processing (e.g., incremental
        extraction, broadcasting).
        """
        cutoff = datetime.now(UTC) - self._orphan_log_max_age
        remaining = []
        replayed = []

        for entry in self._orphan_logs:
            if entry["buffered_at"] < cutoff:
                continue

            matched = entry["trace_id"] in session.trace_ids or (
                entry["session_name"] and self._active_session_for_name.get(entry["session_name"]) == session.session_id
            )

            if matched:
                session.trace_ids.add(entry["trace_id"])
                session.logs.append(entry["log_event"])
                replayed.append(entry["log_event"])
            else:
                remaining.append(entry)

        self._orphan_logs = remaining

        if replayed:
            logger.info(
                "Replayed %d orphan logs into session %s",
                len(replayed),
                session.session_id,
            )

        return replayed

    async def get_or_create_otlp_session(self, trace_id: str, metadata: dict) -> TraceSession:
        """Get existing session for trace_id or create a new one (OTLP path).

        Groups spans by session_name (from resource attributes) or
        gen_ai.conversation.id (OTel semconv), not by trace_id.
        A single session can contain spans from multiple traces — this is common
        with GenAI semconv instrumentation where each LLM call creates its own
        independent trace, and with multi-turn agent conversations where each
        turn produces a separate trace sharing the same conversation ID.
        """
        conversation_id = metadata.get("conversation_id")
        session_name = metadata.get("session_name") or conversation_id or f"otlp-{trace_id[:12]}"

        active_id = self._active_session_for_name.get(session_name)
        if active_id:
            active = self.sessions.get(active_id)
            if active and not active.is_complete:
                active.trace_ids.add(trace_id)
                if conversation_id:
                    await self._absorb_orphan_for_trace(trace_id, active)
                return active
            if active and active.is_complete and conversation_id:
                self._reopen_session(active, trace_id, session_name)
                await self._absorb_orphan_for_trace(trace_id, active)
                return active

        existing = self.find_session_by_trace_id(trace_id)
        if existing:
            if existing.is_complete:
                self._reopen_session(existing, trace_id, session_name)
            else:
                existing.trace_ids.add(trace_id)
            self._active_session_for_name[session_name] = existing.session_id
            return existing

        session_id = session_name
        if session_id in self.sessions:
            counter = 2
            while f"{session_name}-{counter}" in self.sessions:
                counter += 1
            session_id = f"{session_name}-{counter}"

        session = TraceSession(
            session_id=session_id,
            trace_id=trace_id,
            eval_set_id=metadata.get("eval_set_id"),
            metadata={k: v for k, v in metadata.get("resource_attrs", {}).items() if not k.startswith("agentevals.")},
            source="otlp",
            trace_ids={trace_id},
        )

        self.sessions[session_id] = session
        self._active_session_for_name[session_name] = session_id
        self.incremental_extractors[session_id] = IncrementalInvocationExtractor()

        replayed = self._replay_orphan_logs(session)
        extractor = self.incremental_extractors.get(session_id)
        if extractor and replayed:
            for log_event in replayed:
                updates = extractor.process_log(log_event)
                for update in updates:
                    update["sessionId"] = session_id
                    await self.broadcast_to_ui(update)

        await self.broadcast_to_ui(
            WSSessionStartedEvent(
                session=SessionInfo(
                    session_id=session_id,
                    trace_id=trace_id,
                    eval_set_id=metadata.get("eval_set_id"),
                    span_count=0,
                    is_complete=False,
                    started_at=session.started_at.isoformat(),
                    metadata=session.metadata,
                ),
            ).model_dump(by_alias=True)
        )

        logger.info("Auto-created OTLP session: %s (trace: %s)", session_id, trace_id)
        return session

    def schedule_session_completion(self, session_id: str) -> None:
        """Schedule session completion after root span arrival.

        Starts a 3-second grace period to allow late-arriving child spans
        from the same OTLP batch to be included before finalizing.
        """
        if session_id in self._completion_timers:
            self._completion_timers[session_id].cancel()

        self._completion_timers[session_id] = asyncio.create_task(
            self._delayed_complete(session_id, self.completion_grace_seconds)
        )

    def reset_idle_timer(self, session_id: str) -> None:
        """Reset the idle timeout for an OTLP session.

        Fallback completion after 30 seconds of no new spans or logs.
        Primary completion uses root span detection (3-second grace period),
        which handles most cases. This idle timeout catches edge cases like
        agent crashes or traces that never emit a root span.
        """
        if session_id in self._idle_timers:
            self._idle_timers[session_id].cancel()

        self._idle_timers[session_id] = asyncio.create_task(
            self._delayed_complete(session_id, self.idle_timeout_seconds)
        )

    def schedule_log_reextraction(self, session_id: str) -> None:
        """Schedule re-extraction of invocations after late-arriving logs.

        Logs from BatchLogRecordProcessor may arrive after span-triggered
        session completion. This debounces re-extraction so multiple log
        batches are coalesced into a single re-extraction pass.
        """
        key = f"_reextract_{session_id}"
        if key in self._completion_timers:
            self._completion_timers[key].cancel()

        self._completion_timers[key] = asyncio.create_task(
            self._delayed_reextract(session_id, self.reextraction_delay_seconds)
        )

    def _reopen_session(self, session: TraceSession, trace_id: str, session_name: str) -> None:
        """Reopen a completed session when a trace_id already in the session
        receives more spans after completion (split-batch scenario).

        The OTLP BatchSpanProcessor may flush one turn's spans across the
        completion boundary: some child spans arrive before the grace period
        fires, and the root span (plus remaining children) arrives after.
        Because the trace_id was already registered in the session, we know
        these late spans belong here rather than to a new agent run.
        """
        session.is_complete = False
        session.completed_at = None
        session.trace_ids.add(trace_id)
        self._active_session_for_name[session_name] = session.session_id
        self.incremental_extractors[session.session_id] = IncrementalInvocationExtractor()
        self.reset_idle_timer(session.session_id)
        logger.info(
            "Reopened session %s for trace %s (%d spans so far)",
            session.session_id,
            trace_id,
            len(session.spans),
        )

    async def _absorb_orphan_for_trace(self, trace_id: str, target: TraceSession) -> None:
        """Merge an orphan session into the target when conversation_id is discovered.

        When infrastructure spans (no conversation_id) arrive before agent spans,
        they create a separate session keyed by trace_id. Once the conversation_id
        is known and routes to the correct session, the orphan's data is merged
        and the orphan session is removed.
        """
        orphan = None
        orphan_id = None
        for sid, session in self.sessions.items():
            if sid == target.session_id:
                continue
            if trace_id in session.trace_ids:
                orphan = session
                orphan_id = sid
                break

        if not orphan:
            return

        target.spans.extend(orphan.spans)
        target.logs.extend(orphan.logs)
        target.trace_ids.update(orphan.trace_ids)
        if orphan.has_root_span:
            target.has_root_span = True

        del self.sessions[orphan_id]
        for name, mapped_id in list(self._active_session_for_name.items()):
            if mapped_id == orphan_id:
                del self._active_session_for_name[name]
        for timer_map in (self._completion_timers, self._idle_timers):
            if orphan_id in timer_map:
                timer_map.pop(orphan_id).cancel()
        self.incremental_extractors.pop(orphan_id, None)

        await self.broadcast_to_ui(
            WSSessionRemovedEvent(
                session_id=orphan_id,
                absorbed_by=target.session_id,
            ).model_dump(by_alias=True)
        )
        logger.info(
            "Absorbed orphan session %s (%d spans) into %s",
            orphan_id,
            len(orphan.spans),
            target.session_id,
        )

    async def _delayed_complete(self, session_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._complete_otlp_session(session_id)

    async def _delayed_reextract(self, session_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._reextract_with_logs(session_id)

    def find_session_by_trace_id(self, trace_id: str) -> TraceSession | None:
        """Find a session that contains the given trace_id.

        Matches both active and recently-completed sessions so that
        late-arriving logs can still be associated with their session.
        """
        for session in self.sessions.values():
            if trace_id in session.trace_ids:
                return session
        return None

    async def _reextract_with_logs(self, session_id: str) -> None:
        """Re-extract invocations after late logs arrive for a completed session."""
        session = self.sessions.get(session_id)
        if not session:
            return

        key = f"_reextract_{session_id}"
        if key in self._completion_timers:
            del self._completion_timers[key]

        logger.info(
            "Re-extracting invocations with %d late logs for session %s",
            len(session.logs),
            session_id,
        )

        invocations_data = await self._extract_invocations(session)
        session.invocations = invocations_data

        await self.broadcast_to_ui(
            WSSessionCompleteEvent(
                session_id=session_id,
                invocations=invocations_data,
            ).model_dump(by_alias=True)
        )

    async def _complete_otlp_session(self, session_id: str) -> None:
        """Mark an OTLP session as complete and extract invocations.

        Equivalent to the WebSocket 'session_end' handler. Idempotent — does
        nothing if the session is already complete or missing.
        """
        session = self.sessions.get(session_id)
        if not session or session.is_complete:
            return

        session.is_complete = True
        session.completed_at = datetime.now(UTC)

        for name, sid in list(self._active_session_for_name.items()):
            if sid == session_id:
                del self._active_session_for_name[name]
                break

        if session_id in self._completion_timers:
            self._completion_timers.pop(session_id).cancel()
        if session_id in self._idle_timers:
            self._idle_timers.pop(session_id).cancel()

        logger.info(
            "OTLP session complete: %s (%d spans, %d logs)",
            session_id,
            len(session.spans),
            len(session.logs),
        )

        invocations_data = await self._extract_invocations(session)
        session.invocations = invocations_data

        await self.broadcast_to_ui(
            WSSessionCompleteEvent(
                session_id=session_id,
                invocations=invocations_data,
            ).model_dump(by_alias=True)
        )

        if session_id in self.incremental_extractors:
            del self.incremental_extractors[session_id]

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Handle WebSocket connection from an agent.

        Manages the lifecycle of a WebSocket connection, receiving span events
        and broadcasting updates to connected UI clients.

        Args:
            websocket: The WebSocket connection to handle
        """
        await websocket.accept()
        session_id = None

        try:
            async for message in websocket.iter_text():
                event = json.loads(message)

                if event["type"] == "session_start":
                    session_id = event["session_id"]
                    logger.info("Received session_start event: %s", session_id)

                    session = TraceSession(
                        session_id=session_id,
                        trace_id=event["trace_id"],
                        eval_set_id=event.get("eval_set_id"),
                        metadata=event.get("metadata", {}),
                    )
                    self.sessions[session_id] = session
                    self.incremental_extractors[session_id] = IncrementalInvocationExtractor()

                    broadcast_event = WSSessionStartedEvent(
                        session=SessionInfo(
                            session_id=session_id,
                            trace_id=event["trace_id"],
                            eval_set_id=event.get("eval_set_id"),
                            span_count=0,
                            is_complete=False,
                            started_at=session.started_at.isoformat(),
                            metadata=event.get("metadata", {}),
                        ),
                    ).model_dump(by_alias=True)
                    logger.info("Broadcasting session_started to %d SSE clients", len(self.sse_queues))
                    await self.broadcast_to_ui(broadcast_event)

                    logger.info("Session started: %s", session_id)

                elif event["type"] == "span":
                    sid = event["session_id"]

                    if sid not in self.sessions:
                        logger.warning("Span for unknown session: %s", sid)
                        continue

                    session = self.sessions[sid]

                    if not session.can_accept_span():
                        logger.warning(
                            "Session %s has reached max span limit (%d), rejecting new span", sid, len(session.spans)
                        )
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"Session has reached maximum span limit ({len(session.spans)})",
                            }
                        )
                        continue

                    session.spans.append(event["span"])

                    extractor = self.incremental_extractors.get(sid)
                    if extractor:
                        updates = extractor.process_span(event["span"])
                        for update in updates:
                            update["sessionId"] = sid
                            await self.broadcast_to_ui(update)

                    await self.broadcast_to_ui(
                        WSSpanReceivedEvent(
                            session_id=sid,
                            span=event["span"],
                        ).model_dump(by_alias=True)
                    )

                elif event["type"] == "log":
                    sid = event["session_id"]
                    log_event = event["log"]

                    if sid not in self.sessions:
                        logger.warning("Log for unknown session: %s", sid)
                        continue

                    session = self.sessions[sid]

                    if not session.can_accept_log():
                        logger.warning(
                            "Session %s has reached max log limit (%d), rejecting new log", sid, len(session.logs)
                        )
                        await websocket.send_json(
                            {"type": "error", "message": f"Session has reached maximum log limit ({len(session.logs)})"}
                        )
                        continue

                    session.logs.append(log_event)

                    extractor = self.incremental_extractors.get(sid)
                    if extractor:
                        updates = extractor.process_log(log_event)
                        for update in updates:
                            update["sessionId"] = sid
                            await self.broadcast_to_ui(update)
                    else:
                        logger.warning(f"No extractor found for session {sid}")

                elif event["type"] == "session_end":
                    sid = event["session_id"]

                    if sid not in self.sessions:
                        logger.warning("End for unknown session: %s", sid)
                        continue

                    session = self.sessions[sid]
                    session.is_complete = True

                    logger.info("Session ended: %s (%d spans, %d logs)", sid, len(session.spans), len(session.logs))

                    invocations_data = await self._extract_invocations(session)
                    session.invocations = invocations_data

                    complete_event = WSSessionCompleteEvent(
                        session_id=sid,
                        invocations=invocations_data,
                    ).model_dump(by_alias=True)
                    logger.info("Broadcasting session_complete to %d SSE clients", len(self.sse_queues))
                    await self.broadcast_to_ui(complete_event)

                    if sid in self.incremental_extractors:
                        del self.incremental_extractors[sid]

                    await websocket.send_json({"type": "session_complete", "invocations": invocations_data})

        except WebSocketDisconnect:
            if session_id and session_id in self.sessions:
                if not self.sessions[session_id].is_complete:
                    logger.warning("Client disconnected without ending session: %s", session_id)
                else:
                    logger.info("Client disconnected after session end: %s", session_id)

    async def _save_spans_to_temp_file(self, session: TraceSession) -> Path:
        """Write the session as one OTLP/JSON export request (a single JSONL line) to the export dir."""
        temp_file = EXPORT_DIR / export_name(session.session_id, prefix="agentevals_", suffix=".jsonl")
        document = encode_traces(self.session_traces(session))
        with open(temp_file, "w", encoding="utf-8") as f:  # noqa: ASYNC230
            f.write(json.dumps(document) + "\n")
        return temp_file

    def session_traces(self, session: TraceSession) -> list[Trace]:
        """The session's spans and logs as envelope traces, keeping their real trace ids.

        Interim bridge from the session's stored dicts: scope and service name were flattened
        into attributes and metadata at ingest, and stored log records keep only their span id,
        so those are restored here. Logs are joined to spans by id, never copied into spans. The
        resource carries ``agentevals.session_name`` so exports of the session group as one conversation.
        """
        resource_attrs = {SESSION_NAME: session.session_id}
        service_name = (session.metadata or {}).get(OTEL_SERVICE_NAME)
        if service_name:
            resource_attrs[OTEL_SERVICE_NAME] = service_name
        resource = Resource(attributes=resource_attrs)
        decoded = decode_bare_spans_json(session.spans, strict=False)
        scopes: dict[tuple[str, str | None], Scope] = {}
        spans = []
        for span in decoded.spans:
            name = span.attributes.get("otel.scope.name")
            version = span.attributes.get("otel.scope.version")
            key = (name if isinstance(name, str) else "", version if isinstance(version, str) else None)
            scope = scopes.setdefault(key, Scope(name=key[0], version=key[1]))
            spans.append(dataclasses.replace(span, resource=resource, scope=scope))
        trace_of_span = {s.span_id: s.trace_id for s in spans}
        only_trace = next(iter({s.trace_id for s in spans})) if len({s.trace_id for s in spans}) == 1 else None
        logs = [
            log for log in (_session_log(entry, trace_of_span, only_trace, resource) for entry in session.logs) if log
        ]
        traces, _ = build_traces(spans, logs)
        return traces

    async def _extract_invocations(self, session: TraceSession) -> list[dict]:
        """Turns of the session as the ``session_complete`` invocation dicts the UI renders."""
        try:
            traces = self.session_traces(session)
            if not traces:
                logger.warning("No traces loaded from session %s", session.session_id)
                return []
            conversation = extract_conversation(traces, session.session_id)
            spans = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
            return [
                {
                    "invocationId": turn.ref.span_id,
                    "userText": text_of(turn.user_input) or "",
                    "agentText": text_of(turn.final_output) or "",
                    "toolCalls": [
                        {"name": t.name, "args": t.arguments or {}, "id": t.call_id} for t in turn.tool_calls
                    ],
                    "toolResponses": [
                        {"name": t.name, "response": t.result, "id": t.call_id}
                        for t in turn.tool_calls
                        if t.result is not None
                    ],
                    "modelInfo": _model_info(turn, spans),
                }
                for turn in conversation.turns
            ]
        except Exception:
            logger.exception("Failed to extract invocations")
            return []


def _session_log(
    entry: Any, trace_of_span: dict[str, str], only_trace: str | None, resource: Resource
) -> LogRecord | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("event_name"), str):
        return None
    span_id = entry.get("span_id") or None
    trace_id = entry.get("trace_id") or trace_of_span.get(span_id or "") or only_trace
    try:
        time_ns = int(entry.get("timestamp") or 0) or None
    except (TypeError, ValueError):
        time_ns = None
    attributes = entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
    return LogRecord(
        time_unix_nano=time_ns,
        observed_time_unix_nano=None,
        event_name=entry["event_name"],
        severity_number=None,
        severity_text=None,
        body=entry.get("body"),
        attributes=attributes,
        trace_id=trace_id,
        span_id=span_id if trace_id else None,
        flags=None,
        resource=resource,
        scope=EMPTY_SCOPE,
    )


def _model_info(turn: Turn, spans: dict) -> dict[str, Any]:
    """Per turn model metadata; usage counts each logical call once."""
    info: dict[str, Any] = {}
    calls = turn.llm_calls
    models = sorted({c.request_model for c in calls if c.request_model})
    response_models = sorted({c.response_model for c in calls if c.response_model})
    finish = sorted({r for c in calls for r in c.finish_reasons})
    errors = sorted({c.error_type for c in calls if c.error_type})
    usage = turn.usage
    provider = next((c.provider for c in calls if c.provider), None)
    leaf = spans.get((calls[0].ref.trace_id, calls[0].ref.span_id)) if calls else None
    temperature = attr_float(leaf.attributes, "gen_ai.request.temperature") if leaf else None
    max_tokens = attr_int(leaf.attributes, "gen_ai.request.max_tokens") if leaf else None
    for key, value in (
        ("models", models),
        ("inputTokens", usage.input_tokens),
        ("outputTokens", usage.output_tokens),
        ("provider", provider),
        ("responseModels", response_models),
        ("finishReasons", finish),
        ("cacheCreationTokens", usage.cache_write_input_tokens),
        ("cacheReadTokens", usage.cache_read_input_tokens),
        ("temperature", temperature),
        ("maxTokens", max_tokens),
        ("errorTypes", errors),
    ):
        if value:
            info[key] = value
    return info
