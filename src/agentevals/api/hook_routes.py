"""HTTP hook receiver for Claude Code PostToolUse and Stop hooks.

Receives hook payloads from Claude Code and feeds them into the streaming
pipeline. Hooks provide full tool I/O and agent response text that are not
available in Claude Code's OTEL logs.

Users configure Claude Code hooks to POST to http://localhost:4318/v1/hooks.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

hook_router = APIRouter()
_trace_manager: "StreamingTraceManager | None" = None


def set_hook_trace_manager(manager: "StreamingTraceManager") -> None:
    global _trace_manager
    _trace_manager = manager


@hook_router.post("/v1/hooks")
async def receive_hook(request: Request) -> Response:
    """Receive Claude Code hook events (PostToolUse, Stop)."""
    if not _trace_manager:
        return Response(status_code=503, content="Live mode not enabled")

    raw = await request.body()
    try:
        body = json.loads(raw, strict=False)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Hook JSON parse error: %s (first 200 bytes: %r)", exc, raw[:200])
        return Response(
            status_code=200,
            content='{"status":"parse_error"}',
            media_type="application/json",
        )
    hook_name = body.get("hook_event_name", "")
    logger.debug("Hook received: hook_event_name=%r session_id=%r keys=%s", hook_name, body.get("session_id"), list(body.keys())[:10])

    if hook_name not in ("PostToolUse", "PostToolUseFailure", "Stop"):
        return Response(
            status_code=200,
            content='{"status":"ignored"}',
            media_type="application/json",
        )

    session_id = body.get("session_id", "")
    if not session_id:
        return Response(
            status_code=400,
            content='{"error":"missing session_id"}',
            media_type="application/json",
        )

    body.setdefault("timestamp", time.time_ns())

    session = _find_session_by_cc_id(session_id)

    if not session:
        session = _find_session_by_cc_id(session_id, include_completed=True)

    if not session:
        metadata = {"session_name": session_id, "source": "claude_code_hooks"}
        session = await _trace_manager.get_or_create_claude_code_session(
            trace_id=session_id,
            metadata=metadata,
            session_name=session_id,
        )

    if not session:
        logger.warning("Could not create session for hook session_id=%s", session_id)
        return Response(
            status_code=200,
            content='{"status":"no_session"}',
            media_type="application/json",
        )

    if not hasattr(session, "hook_events"):
        session.hook_events = []
    session.hook_events.append(body)

    extractor = _trace_manager.incremental_extractors.get(session.session_id)
    if extractor and hasattr(extractor, "process_hook_event"):
        updates = extractor.process_hook_event(body)
        for update in updates:
            update["sessionId"] = session.session_id
            await _trace_manager.broadcast_to_ui(update)

    _trace_manager.reset_idle_timer(session.session_id)

    if hook_name == "Stop":
        _trace_manager.schedule_session_completion(session.session_id)

    return Response(
        status_code=200,
        content='{"status":"ok"}',
        media_type="application/json",
    )


def _find_session_by_cc_id(cc_session_id: str, include_completed: bool = False):
    """Find a session matching the Claude Code session_id.

    Checks both the session_id directly and the active-session-for-name mapping,
    since OTEL logs may have created the session under a different internal ID.
    When include_completed is True, also matches completed sessions (for late
    Stop hooks that arrive after idle timeout).
    """
    if not _trace_manager:
        return None

    if cc_session_id in _trace_manager.sessions:
        session = _trace_manager.sessions[cc_session_id]
        if not session.is_complete or include_completed:
            return session

    active_id = _trace_manager._active_session_for_name.get(cc_session_id)
    if active_id:
        candidate = _trace_manager.sessions.get(active_id)
        if candidate and (not candidate.is_complete or include_completed):
            return candidate

    for session in _trace_manager.sessions.values():
        if session.source == "claude_code" and (not session.is_complete or include_completed):
            cc_id = session.metadata.get("cc_session_id", "")
            if cc_id == cc_session_id:
                return session

    return None
