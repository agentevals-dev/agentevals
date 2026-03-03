"""Trace session tracking for live streaming."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MAX_SPANS_PER_SESSION = 10000
MAX_LOGS_PER_SESSION = 5000


@dataclass
class TraceSession:
    """Represents an active trace session from a streaming agent."""

    session_id: str
    trace_id: str
    eval_set_id: str | None
    spans: list[dict] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    is_complete: bool = False
    metadata: dict = field(default_factory=dict)

    def can_accept_span(self) -> bool:
        """Check if session can accept another span without exceeding limits."""
        return len(self.spans) < MAX_SPANS_PER_SESSION

    def can_accept_log(self) -> bool:
        """Check if session can accept another log without exceeding limits."""
        return len(self.logs) < MAX_LOGS_PER_SESSION
