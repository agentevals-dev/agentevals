"""Synchronous control surface used by ``/api/runs`` HTTP handlers.

Wraps the :class:`agentevals.storage.repos.RunRepository` with submit
idempotency, list pagination, and the 409 spec-mismatch path.

Also provides :meth:`RunService.record_completed_eval` for the
``/api/evaluate`` path: that handler executes synchronously (the trace was
already supplied as multipart and the result is being streamed back over
SSE), so we synthesize a Run row for visibility in run history rather than
queueing work for the worker.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..config import EvalParams
from ..runner import RunResult
from ..storage.models import Run, RunSpec, RunStatus
from ..storage.repos import ResultRepository, RunRepository
from .result_builder import build_results, summarize_run_result

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunSubmitConflict(Exception):
    """Raised when a re-submission's spec differs from the persisted one.

    The caller (HTTP handler) maps this to ``409 Conflict`` and returns the
    persisted run so the client can reconcile.
    """

    def __init__(self, persisted: Run) -> None:
        super().__init__(f"run {persisted.run_id} already exists with a different spec")
        self.persisted = persisted


class RunService:
    def __init__(self, runs: RunRepository, results: ResultRepository) -> None:
        self._runs = runs
        self._results = results

    async def submit(self, *, run_id: UUID | None, spec: RunSpec) -> Run:
        run = Run(
            run_id=run_id or uuid4(),
            status=RunStatus.QUEUED,
            spec=spec,
        )
        persisted = await self._runs.create(run)
        if persisted.run_id == run.run_id and not _specs_equal(persisted.spec, spec):
            raise RunSubmitConflict(persisted)
        return persisted

    async def get(self, run_id: UUID) -> Run | None:
        return await self._runs.get(run_id)

    async def list(
        self,
        *,
        status: list[RunStatus] | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[Run]:
        return await self._runs.list(status=status, limit=limit, before=before)

    async def list_results(self, run_id: UUID):
        return await self._results.list_by_run(run_id)

    async def cancel(self, run_id: UUID) -> bool:
        return await self._runs.cancel(run_id)

    async def record_completed_eval(
        self,
        *,
        spec: RunSpec,
        params: EvalParams,
        run_result: RunResult,
    ) -> Run:
        """Persist a synchronously-completed eval as a Run row plus Result rows.

        The run is created already in ``running`` state (so the row passes the
        ``run_running_has_worker`` check is sidestepped via a synthetic worker
        id), then transitioned to a terminal state in the same call. Two
        writes per eval, but using the public :class:`RunRepository` API
        avoids leaking an executor-only schema requirement into this layer.
        """
        run_id = uuid4()
        worker_id = "sync:/api/evaluate"
        run = Run(
            run_id=run_id,
            status=RunStatus.QUEUED,
            spec=spec,
            attempt=1,
            worker_id=worker_id,
            started_at=_now(),
        )
        await self._runs.create(run)

        results = build_results(run_id, params, run_result)
        await self._results.upsert_many(run_id, results)

        summary = summarize_run_result(run_result)
        if run_result.errors:
            error = "; ".join(run_result.errors[:3])
            await self._runs.update_status(run_id, RunStatus.FAILED, error=error, summary=summary)
            run.status = RunStatus.FAILED
            run.error = error
        else:
            await self._runs.update_status(run_id, RunStatus.SUCCEEDED, summary=summary)
            run.status = RunStatus.SUCCEEDED
        run.summary = summary
        return run


def _specs_equal(a: RunSpec, b: RunSpec) -> bool:
    """Deep equality on the JSON projection. Pydantic equality compares model
    instances by class identity, which trips up the round-trip from JSONB."""
    return json.dumps(a.model_dump(by_alias=False), sort_keys=True) == json.dumps(
        b.model_dump(by_alias=False), sort_keys=True
    )
