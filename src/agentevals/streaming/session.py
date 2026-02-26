"""Trace session tracking for live streaming."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TraceSession:
    """Represents an active trace session from a streaming agent."""

    session_id: str
    trace_id: str
    eval_set_id: str | None
    spans: list[dict] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    is_complete: bool = False
    metadata: dict = field(default_factory=dict)
