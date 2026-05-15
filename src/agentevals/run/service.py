"""Synchronous control surface used by ``/api/runs`` and ``/api/evaluate``.

Wraps the :class:`agentevals.storage.repos.RunRepository` with submit
idempotency, list pagination, and the 409 spec-mismatch path. Also exposes
:meth:`RunService.record_eval_run` for the ``/api/evaluate`` path, which
executes synchronously and synthesizes a Run row for visibility in run
history rather than queueing work for the worker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from ..config import EvalParams
from ..runner import RunResult
from ..storage.models import Run, RunSpec, RunStatus, TraceTarget
from ..storage.repos import ResultRepository, RunRepository
from .result_builder import build_results, summarize_run_result

logger = logging.getLogger(__name__)


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
        self._validate_spec(spec)
        run = Run(
            run_id=run_id or uuid4(),
            status=RunStatus.QUEUED,
            spec=spec,
        )
        persisted = await self._runs.create(run)
        if persisted.run_id == run.run_id and persisted.spec != spec:
            raise RunSubmitConflict(persisted)
        return persisted

    @staticmethod
    def _validate_spec(spec: RunSpec) -> None:
        try:
            EvalParams.model_validate(spec.eval_config or {})
        except ValidationError as exc:
            raise ValueError(f"Invalid eval_config: {exc}") from exc

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

    async def record_eval_run(
        self,
        *,
        params: EvalParams,
        eval_set_dict: dict[str, Any] | None,
        trace_format: str | None,
        upload_filenames: list[str] | None,
        run_result: RunResult,
    ) -> Run:
        """Persist a synchronously-completed ``/api/evaluate`` call as a Run
        row plus Result rows.

        Builds an ``uploaded`` :class:`TraceTarget` from the request metadata,
        creates a queued run, persists results, then transitions the run to
        a terminal status. Two writes (create + update_status), but the
        public :class:`RunRepository` API stays clean of executor-only
        schema knowledge.
        """
        filenames = list(upload_filenames or [])
        target = TraceTarget(
            kind="uploaded",
            trace_format=trace_format if trace_format in ("jaeger-json", "otlp-json") else None,
            trace_count=len(filenames),
            trace_files=filenames,
        )
        spec = RunSpec(
            approach="trace_replay",
            target=target,
            eval_config=params.model_dump(by_alias=False),
            eval_set=eval_set_dict,
        )

        run_id = uuid4()
        run = Run(
            run_id=run_id,
            status=RunStatus.QUEUED,
            spec=spec,
            attempt=1,
            worker_id="sync:/api/evaluate",
            started_at=datetime.now(timezone.utc),
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
