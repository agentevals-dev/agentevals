"""WebSocket server for streaming OTel spans from agents."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..api.models import (
    SessionInfo,
    WSSessionCompleteEvent,
    WSSessionStartedEvent,
    WSSpanReceivedEvent,
)
from ..converter import convert_traces
from ..extraction import extract_token_usage_from_attrs, is_llm_span, parse_tool_response_content
from ..loader.base import Trace
from ..loader.otlp import OtlpJsonLoader
from ..trace_attrs import OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_REQUEST_MODEL
from ..utils.log_enrichment import enrich_spans_with_logs
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

        Groups spans by session_name (from resource attributes), not by trace_id.
        A single session can contain spans from multiple traces — this is common
        with GenAI semconv instrumentation where each LLM call creates its own
        independent trace.
        """
        session_name = metadata.get("session_name") or f"otlp-{trace_id[:12]}"

        active_id = self._active_session_for_name.get(session_name)
        if active_id:
            active = self.sessions.get(active_id)
            if active and not active.is_complete:
                active.trace_ids.add(trace_id)
                return active

        existing = self.find_session_by_trace_id(trace_id)
        if existing and existing.is_complete:
            self._reopen_session(existing, trace_id, session_name)
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
        """Save spans to a temporary OTLP JSONL file.

        Args:
            session: The trace session containing spans to save

        Returns:
            Path to the temporary JSONL file containing the spans
        """
        temp_file = Path(tempfile.gettempdir()) / f"agentevals_{session.session_id}.jsonl"

        enriched_spans = enrich_spans_with_logs(session.spans, session.logs, session.session_id)

        with open(temp_file, "w") as f:  # noqa: ASYNC230
            for span in enriched_spans:
                span_copy = span.copy()
                span_copy["traceId"] = session.trace_id
                f.write(json.dumps(span_copy) + "\n")

        return temp_file

    async def _extract_invocations(self, session: TraceSession) -> list[dict]:
        """Extract invocations from session spans for UI display.

        Converts raw OTLP spans into structured invocation data with user/agent messages,
        tool calls, and model information for display in the UI.

        Args:
            session: The trace session containing spans to extract invocations from

        Returns:
            List of invocation dictionaries with the following structure:
                - invocationId: Unique identifier for the invocation
                - userText: User's input text
                - agentText: Agent's response text
                - toolCalls: List of tool calls with name and args
                - modelInfo: Model metadata (model name, tokens, etc.)
        """
        try:
            temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)

            has_genai_spans = any(
                span.get("attributes", [])
                and any(
                    attr.get("key") in (OTEL_GENAI_REQUEST_MODEL, OTEL_GENAI_INPUT_MESSAGES)
                    for attr in span.get("attributes", [])
                )
                for span in session.spans
            )

            if has_genai_spans and not session.logs:
                logger.warning(
                    "Session %s has GenAI spans but no logs. "
                    "Message content will be missing unless spans already enriched.",
                    session.session_id,
                )

            enriched_spans = enrich_spans_with_logs(session.spans, session.logs, session.session_id)

            for span in enriched_spans:
                span_copy = span.copy()
                span_copy["traceId"] = session.trace_id
                temp_file.write(json.dumps(span_copy) + "\n")
            temp_file.close()

            logger.debug("Saved %d enriched spans to %s", len(enriched_spans), temp_file.name)

            loader = OtlpJsonLoader()
            traces = loader.load(temp_file.name)

            if not traces:
                logger.warning("No traces loaded from session %s", session.session_id)
                return []

            logger.debug("Loaded %d traces", len(traces))

            conversion_results = convert_traces(traces)

            if not conversion_results:
                logger.warning("No conversion results")
                return []

            invocations_data = []

            for trace_idx, conv_result in enumerate(conversion_results):
                if conv_result.warnings:
                    logger.warning("Conversion warnings: %s", conv_result.warnings)

                trace = traces[trace_idx] if trace_idx < len(traces) else None

                for inv_idx, inv in enumerate(conv_result.invocations):
                    user_text = ""
                    if inv.user_content and inv.user_content.parts:
                        user_text = " ".join(p.text for p in inv.user_content.parts if p.text)

                    agent_text = ""
                    if inv.final_response and inv.final_response.parts:
                        for part in inv.final_response.parts:
                            if part.text:
                                agent_text += part.text

                    tool_calls = []
                    if inv.intermediate_data and inv.intermediate_data.tool_uses:
                        for tool_use in inv.intermediate_data.tool_uses:
                            tool_calls.append(
                                {
                                    "name": tool_use.name,
                                    "args": tool_use.args if hasattr(tool_use, "args") else {},
                                    "id": getattr(tool_use, "id", None),
                                }
                            )

                    tool_responses = []
                    if inv.intermediate_data and inv.intermediate_data.tool_responses:
                        for tr in inv.intermediate_data.tool_responses:
                            tool_responses.append(
                                {
                                    "name": tr.name,
                                    "response": tr.response if hasattr(tr, "response") else {},
                                    "id": getattr(tr, "id", None),
                                }
                            )

                    model_info = {}
                    if trace:
                        model_info = self._extract_model_info_from_trace(trace, inv_idx)

                    invocations_data.append(
                        {
                            "invocationId": inv.invocation_id,
                            "userText": user_text,
                            "agentText": agent_text,
                            "toolCalls": tool_calls,
                            "toolResponses": tool_responses,
                            "modelInfo": model_info,
                        }
                    )

            logger.debug("Extracted %d invocations from %d traces", len(invocations_data), len(conversion_results))

            self._augment_tool_responses_from_logs(invocations_data, session)

            return invocations_data

        except Exception:
            logger.exception("Failed to extract invocations")
            return []

    def _extract_model_info_from_trace(self, trace: Trace, invocation_idx: int) -> dict:
        """Extract model information from LLM spans in the trace."""
        model_info: dict[str, Any] = {}
        models_used: set[str] = set()
        total_input_tokens = 0
        total_output_tokens = 0

        llm_spans = [s for s in trace.all_spans if is_llm_span(s) or "call_llm" in s.operation_name]

        for span in llm_spans:
            in_toks, out_toks, model = extract_token_usage_from_attrs(span.tags)
            if model and model != "unknown":
                models_used.add(model)
            else:
                genai_model = span.get_tag(OTEL_GENAI_REQUEST_MODEL)
                if genai_model:
                    models_used.add(genai_model)
            total_input_tokens += in_toks
            total_output_tokens += out_toks

        if models_used:
            model_info["models"] = list(models_used)
        if total_input_tokens > 0:
            model_info["inputTokens"] = total_input_tokens
        if total_output_tokens > 0:
            model_info["outputTokens"] = total_output_tokens

        return model_info

    @staticmethod
    def _augment_tool_responses_from_logs(invocations_data: list[dict], session: TraceSession) -> None:
        """Fill in missing tool responses from session logs (e.g. LangChain gen_ai.tool.message)."""
        if not session.logs:
            return

        needs_responses = any(inv.get("toolCalls") and not inv.get("toolResponses") for inv in invocations_data)
        if not needs_responses:
            return

        tool_names: dict[str, str] = {}
        for inv in invocations_data:
            for tc in inv.get("toolCalls", []):
                tc_id = tc.get("id")
                if tc_id:
                    tool_names[tc_id] = tc["name"]

        tool_results_by_span: dict[str, list[dict]] = {}
        for log_event in session.logs:
            if log_event.get("event_name") != "gen_ai.tool.message":
                continue
            body = log_event.get("body", {})
            if not isinstance(body, dict):
                continue
            span_id = log_event.get("span_id", "")
            tool_id = body.get("id", "")
            content = body.get("content")
            if content is None:
                continue

            response = parse_tool_response_content(content)
            tool_results_by_span.setdefault(span_id, []).append(
                {
                    "name": body.get("name") or tool_names.get(tool_id, "unknown"),
                    "response": response,
                    "id": tool_id,
                }
            )

        if not tool_results_by_span:
            return

        for inv in invocations_data:
            if inv.get("toolResponses"):
                continue
            inv_id = inv.get("invocationId", "")
            bare_span_id = inv_id.removeprefix("genai-")
            responses = tool_results_by_span.get(bare_span_id, [])
            if responses:
                inv["toolResponses"] = responses
