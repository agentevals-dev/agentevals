"""Repository protocols and the bundle holder used by ``/api/runs``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from ..models import Result, Run, RunStatus

if False:  # for type checking only — avoids circular import at runtime
    from ...streaming.session import TraceSession


class SessionRepository(Protocol):
    """Tracks streaming TraceSession metadata.

    Spans and logs themselves stay in-process on the StreamingTraceManager in
    this OSS slice; only the session lifecycle row is persisted.
    """

    async def get(self, session_id: str) -> "TraceSession | None": ...
    async def upsert(self, session: "TraceSession") -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def list_all(self) -> "list[TraceSession]": ...
    async def find_by_trace_id(self, trace_id: str) -> "TraceSession | None": ...


class RunRepository(Protocol):
    async def create(self, run: Run) -> Run:
        """Insert a new run. Idempotent on ``run_id`` — if a row exists with
        the same id, returns the persisted row unchanged.
        """

    async def get(self, run_id: UUID) -> Run | None: ...
    async def list(
        self,
        *,
        status: list[RunStatus] | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[Run]: ...
    async def claim_next(self, *, worker_id: str, lease: timedelta, max_attempts: int) -> Run | None:
        """Atomically claim a queued or lease-expired run via SELECT FOR UPDATE
        SKIP LOCKED. Returns ``None`` if no work is available.
        """

    async def heartbeat(self, run_id: UUID, worker_id: str, lease: timedelta) -> bool:
        """Extend the lease. Returns False if the run was cancelled or lost."""

    async def update_status(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        error: str | None = None,
        summary: dict | None = None,
    ) -> None: ...
    async def cancel(self, run_id: UUID) -> bool:
        """Mark cancel_requested=True; the worker observes on next heartbeat."""


class ResultRepository(Protocol):
    async def upsert_many(self, run_id: UUID, results: list[Result]) -> None:
        """Idempotent bulk insert/update on ``result_id``."""

    async def list_by_run(self, run_id: UUID) -> list[Result]: ...
    async def delete_by_run(self, run_id: UUID) -> None: ...


@dataclass
class Repos:
    """Bundle of the three repos plus a close hook for the underlying pool."""

    sessions: SessionRepository
    runs: RunRepository
    results: ResultRepository
    backend: str

    async def close(self) -> None:
        pass


__all__ = [
    "Repos",
    "ResultRepository",
    "RunRepository",
    "SessionRepository",
]
