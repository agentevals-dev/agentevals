"""asyncpg-backed repository implementations.

Plain SQL, no ORM. The connection pool is created in
``storage.postgres.pool.create_pool`` and lives on :class:`PostgresRepos`;
each method acquires a connection from the pool for the duration of a single
query or transaction.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from ..models import Result, ResultStatus, Run, RunSpec, RunStatus
from . import Repos

if TYPE_CHECKING:
    import asyncpg

    from ...streaming.session import TraceSession

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_session(row: "asyncpg.Record") -> "TraceSession":
    from ...streaming.session import TraceSession

    return TraceSession(
        session_id=row["session_id"],
        trace_id=row["trace_id"],
        eval_set_id=row["eval_set_id"],
        started_at=row["started_at"],
        is_complete=row["is_complete"],
        completed_at=row["completed_at"],
        metadata=dict(row["metadata"]) if row["metadata"] else {},
        source=row["source"],
        has_root_span=row["has_root_span"],
        trace_ids=set(row["trace_ids"] or []),
    )


def _row_to_run(row: "asyncpg.Record") -> Run:
    spec_json = row["spec"]
    spec_dict = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
    summary_json = row["summary"]
    summary = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    return Run(
        run_id=row["run_id"],
        status=RunStatus(row["status"]),
        spec=RunSpec.model_validate(spec_dict),
        attempt=row["attempt"],
        worker_id=row["worker_id"],
        error=row["error"],
        summary=summary,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested=row["cancel_requested"],
    )


def _row_to_result(row: "asyncpg.Record") -> Result:
    details_json = row["details"]
    details = json.loads(details_json) if isinstance(details_json, str) else details_json
    tokens_json = row["tokens_used"]
    tokens = json.loads(tokens_json) if isinstance(tokens_json, str) else tokens_json
    return Result(
        result_id=row["result_id"],
        run_id=row["run_id"],
        eval_set_item_id=row["eval_set_item_id"],
        eval_set_item_name=row["eval_set_item_name"],
        evaluator_name=row["evaluator_name"],
        evaluator_type=row["evaluator_type"],
        status=ResultStatus(row["status"]),
        score=row["score"],
        per_invocation_scores=list(row["per_invocation_scores"] or []),
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        details=dict(details) if details else {},
        error_text=row["error_text"],
        tokens_used=dict(tokens) if tokens else None,
        latency_ms=row["latency_ms"],
        created_at=row["created_at"],
    )


class PostgresSessionRepository:
    def __init__(self, pool: "asyncpg.Pool", schema: str) -> None:
        self._pool = pool
        self._schema = schema

    @property
    def _t(self) -> str:
        return f'"{self._schema}".session'

    async def get(self, session_id: str) -> "TraceSession | None":
        row = await self._pool.fetchrow(f"SELECT * FROM {self._t} WHERE session_id = $1", session_id)
        return _row_to_session(row) if row else None

    async def upsert(self, session: "TraceSession") -> None:
        await self._pool.execute(
            f"""
            INSERT INTO {self._t}
                (session_id, trace_id, trace_ids, eval_set_id, source, is_complete,
                 has_root_span, metadata, started_at, completed_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, now())
            ON CONFLICT (session_id) DO UPDATE SET
                trace_id     = EXCLUDED.trace_id,
                trace_ids    = EXCLUDED.trace_ids,
                eval_set_id  = EXCLUDED.eval_set_id,
                source       = EXCLUDED.source,
                is_complete  = EXCLUDED.is_complete,
                has_root_span= EXCLUDED.has_root_span,
                metadata     = EXCLUDED.metadata,
                started_at   = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at,
                updated_at   = now()
            """,
            session.session_id,
            session.trace_id,
            sorted(session.trace_ids),
            session.eval_set_id,
            session.source,
            session.is_complete,
            session.has_root_span,
            json.dumps(session.metadata or {}),
            session.started_at,
            session.completed_at,
        )

    async def delete(self, session_id: str) -> None:
        await self._pool.execute(f"DELETE FROM {self._t} WHERE session_id = $1", session_id)

    async def list_all(self) -> "list[TraceSession]":
        rows = await self._pool.fetch(f"SELECT * FROM {self._t} ORDER BY started_at DESC")
        return [_row_to_session(r) for r in rows]

    async def find_by_trace_id(self, trace_id: str) -> "TraceSession | None":
        row = await self._pool.fetchrow(
            f"SELECT * FROM {self._t} WHERE $1 = ANY(trace_ids) OR trace_id = $1 ORDER BY started_at DESC LIMIT 1",
            trace_id,
        )
        return _row_to_session(row) if row else None


class PostgresRunRepository:
    def __init__(self, pool: "asyncpg.Pool", schema: str) -> None:
        self._pool = pool
        self._schema = schema

    @property
    def _t(self) -> str:
        return f'"{self._schema}".run'

    async def create(self, run: Run) -> Run:
        spec_json = run.spec.model_dump_json(by_alias=False)
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO {self._t}
                (run_id, status, approach, spec, attempt, created_at)
            VALUES ($1, $2, $3, $4::jsonb, 0, $5)
            ON CONFLICT (run_id) DO NOTHING
            RETURNING *
            """,
            run.run_id,
            run.status.value,
            run.spec.approach,
            spec_json,
            run.created_at,
        )
        if row is not None:
            return _row_to_run(row)
        existing = await self.get(run.run_id)
        if existing is None:
            raise RuntimeError(f"run {run.run_id} disappeared between INSERT ... ON CONFLICT and SELECT")
        return existing

    async def get(self, run_id: UUID) -> Run | None:
        row = await self._pool.fetchrow(f"SELECT * FROM {self._t} WHERE run_id = $1", run_id)
        return _row_to_run(row) if row else None

    async def list(
        self,
        *,
        status: list[RunStatus] | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[Run]:
        clauses: list[str] = []
        args: list[object] = []
        if status:
            args.append([s.value for s in status])
            clauses.append(f"status = ANY(${len(args)})")
        if before:
            args.append(before)
            clauses.append(f"created_at < ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        rows = await self._pool.fetch(
            f"SELECT * FROM {self._t} {where} ORDER BY created_at DESC LIMIT ${len(args)}",
            *args,
        )
        return [_row_to_run(r) for r in rows]

    async def claim_next(self, *, worker_id: str, lease: timedelta, max_attempts: int) -> Run | None:
        lease_seconds = int(lease.total_seconds())
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {self._t}
                       SET status = 'running',
                           worker_id = $1,
                           claimed_at = now(),
                           lease_expires_at = now() + make_interval(secs => $2),
                           started_at = COALESCE(started_at, now()),
                           attempt = attempt + 1
                     WHERE run_id = (
                       SELECT run_id FROM {self._t}
                        WHERE attempt < $3
                          AND cancel_requested = FALSE
                          AND (status = 'queued'
                               OR (status = 'running' AND lease_expires_at < now()))
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                     )
                     RETURNING *
                    """,
                    worker_id,
                    lease_seconds,
                    max_attempts,
                )
        return _row_to_run(row) if row else None

    async def heartbeat(self, run_id: UUID, worker_id: str, lease: timedelta) -> bool:
        lease_seconds = int(lease.total_seconds())
        row = await self._pool.fetchrow(
            f"""
            UPDATE {self._t}
               SET lease_expires_at = now() + make_interval(secs => $1)
             WHERE run_id = $2
               AND worker_id = $3
               AND status = 'running'
               AND cancel_requested = FALSE
            RETURNING run_id
            """,
            lease_seconds,
            run_id,
            worker_id,
        )
        return row is not None

    async def update_status(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        error: str | None = None,
        summary: dict | None = None,
    ) -> None:
        terminal = status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)
        await self._pool.execute(
            f"""
            UPDATE {self._t}
               SET status = $1,
                   error = COALESCE($2, error),
                   summary = COALESCE($3::jsonb, summary),
                   finished_at = CASE WHEN $4 THEN now() ELSE finished_at END,
                   worker_id = CASE WHEN $4 THEN NULL ELSE worker_id END,
                   lease_expires_at = CASE WHEN $4 THEN NULL ELSE lease_expires_at END,
                   claimed_at = CASE WHEN $4 THEN NULL ELSE claimed_at END
             WHERE run_id = $5
            """,
            status.value,
            error,
            json.dumps(summary) if summary is not None else None,
            terminal,
            run_id,
        )

    async def cancel(self, run_id: UUID) -> bool:
        row = await self._pool.fetchrow(
            f"""
            UPDATE {self._t}
               SET cancel_requested = TRUE,
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END
             WHERE run_id = $1
               AND status IN ('queued', 'running')
            RETURNING run_id
            """,
            run_id,
        )
        return row is not None


class PostgresResultRepository:
    def __init__(self, pool: "asyncpg.Pool", schema: str) -> None:
        self._pool = pool
        self._schema = schema

    @property
    def _t(self) -> str:
        return f'"{self._schema}".result'

    async def upsert_many(self, run_id: UUID, results: list[Result]) -> None:
        if not results:
            return
        rows = [
            (
                r.result_id,
                r.run_id,
                r.eval_set_item_id,
                r.eval_set_item_name,
                r.evaluator_name,
                r.evaluator_type,
                r.status.value,
                r.score,
                [s for s in r.per_invocation_scores if s is not None],
                r.trace_id,
                r.span_id,
                json.dumps(r.details or {}),
                r.error_text,
                json.dumps(r.tokens_used) if r.tokens_used is not None else None,
                r.latency_ms,
                r.created_at,
            )
            for r in results
        ]
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    f"""
                    INSERT INTO {self._t}
                        (result_id, run_id, eval_set_item_id, eval_set_item_name,
                         evaluator_name, evaluator_type, status, score,
                         per_invocation_scores, trace_id, span_id, details,
                         error_text, tokens_used, latency_ms, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb,
                            $13, $14::jsonb, $15, $16)
                    ON CONFLICT (result_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        score = EXCLUDED.score,
                        per_invocation_scores = EXCLUDED.per_invocation_scores,
                        details = EXCLUDED.details,
                        error_text = EXCLUDED.error_text,
                        tokens_used = EXCLUDED.tokens_used,
                        latency_ms = EXCLUDED.latency_ms
                    """,
                    rows,
                )

    async def list_by_run(self, run_id: UUID) -> list[Result]:
        rows = await self._pool.fetch(
            f"SELECT * FROM {self._t} WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
        return [_row_to_result(r) for r in rows]

    async def delete_by_run(self, run_id: UUID) -> None:
        await self._pool.execute(f"DELETE FROM {self._t} WHERE run_id = $1", run_id)


class PostgresRepos(Repos):
    """Repos backed by a single asyncpg pool. ``close()`` shuts the pool down."""

    def __init__(self, *, pool: "asyncpg.Pool", schema: str) -> None:
        super().__init__(
            sessions=PostgresSessionRepository(pool, schema),
            runs=PostgresRunRepository(pool, schema),
            results=PostgresResultRepository(pool, schema),
            backend="postgres",
        )
        self._pool = pool

    @classmethod
    async def create(cls, *, pool: "asyncpg.Pool", schema: str) -> "PostgresRepos":
        return cls(pool=pool, schema=schema)

    async def close(self) -> None:
        await self._pool.close()
