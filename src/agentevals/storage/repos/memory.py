"""In-process dict-backed implementations of the repository protocols.

Used as the default for OSS so ``agentevals run trace.json`` and ``helm
install agentevals`` keep working with no external dependencies. Behavior
matches the pre-existing :class:`StreamingTraceManager.sessions` dict that
this code replaces.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from ..models import Result, Run, RunStatus
from . import Repos, ResultRepository, RunRepository, SessionRepository

if TYPE_CHECKING:
    from ...streaming.session import TraceSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, TraceSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> TraceSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def upsert(self, session: TraceSession) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def list_all(self) -> list[TraceSession]:
        async with self._lock:
            return list(self._sessions.values())

    async def find_by_trace_id(self, trace_id: str) -> TraceSession | None:
        async with self._lock:
            for session in self._sessions.values():
                if trace_id in session.trace_ids:
                    return session
            return None


class MemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[UUID, Run] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: Run) -> Run:
        async with self._lock:
            existing = self._runs.get(run.run_id)
            if existing is not None:
                return existing
            self._runs[run.run_id] = run
            return run

    async def get(self, run_id: UUID) -> Run | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def list(
        self,
        *,
        status: list[RunStatus] | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[Run]:
        async with self._lock:
            runs = list(self._runs.values())
        runs.sort(key=lambda r: r.created_at, reverse=True)
        if status:
            runs = [r for r in runs if r.status in status]
        if before:
            runs = [r for r in runs if r.created_at < before]
        return runs[:limit]

    async def claim_next(self, *, worker_id: str, lease: timedelta, max_attempts: int) -> Run | None:
        now = _now()
        async with self._lock:
            candidates = [r for r in self._runs.values() if r.status == RunStatus.QUEUED and r.attempt < max_attempts]
            candidates.sort(key=lambda r: r.created_at)
            if not candidates:
                return None
            run = candidates[0]
            run.status = RunStatus.RUNNING
            run.worker_id = worker_id
            run.attempt += 1
            run.started_at = run.started_at or now
            return run

    async def heartbeat(self, run_id: UUID, worker_id: str, lease: timedelta) -> bool:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.worker_id != worker_id:
                return False
            return not run.cancel_requested

    async def update_status(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        error: str | None = None,
        summary: dict | None = None,
    ) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = status
            if error is not None:
                run.error = error
            if summary is not None:
                run.summary = summary
            if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
                run.finished_at = _now()

    async def cancel(self, run_id: UUID) -> bool:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
                return False
            run.cancel_requested = True
            if run.status == RunStatus.QUEUED:
                run.status = RunStatus.CANCELLED
                run.finished_at = _now()
            return True


class MemoryResultRepository:
    def __init__(self) -> None:
        self._results: dict[str, Result] = {}
        self._by_run: dict[UUID, list[str]] = {}
        self._lock = asyncio.Lock()

    async def upsert_many(self, run_id: UUID, results: list[Result]) -> None:
        async with self._lock:
            for r in results:
                self._results[r.result_id] = r
                ids = self._by_run.setdefault(run_id, [])
                if r.result_id not in ids:
                    ids.append(r.result_id)

    async def list_by_run(self, run_id: UUID) -> list[Result]:
        async with self._lock:
            ids = self._by_run.get(run_id, [])
            return [self._results[i] for i in ids if i in self._results]

    async def delete_by_run(self, run_id: UUID) -> None:
        async with self._lock:
            for rid in self._by_run.pop(run_id, []):
                self._results.pop(rid, None)


class MemoryRepos(Repos):
    @classmethod
    def create(cls) -> "MemoryRepos":
        return cls(
            sessions=MemorySessionRepository(),
            runs=MemoryRunRepository(),
            results=MemoryResultRepository(),
            backend="memory",
        )


__all__ = [
    "MemoryRepos",
    "MemoryResultRepository",
    "MemoryRunRepository",
    "MemorySessionRepository",
]
