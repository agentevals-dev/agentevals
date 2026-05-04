"""Async run worker.

A pool of asyncio tasks each loop on ``run_repo.claim_next``, heartbeat the
lease while executing, and drive the existing
:func:`agentevals.runner.run_evaluation_from_traces` pipeline.

Cancellation is signaled by setting ``run.cancel_requested`` via
``POST /api/runs/{id}/cancel``. The heartbeat task observes the flag on each
tick and cancels the worker task; the worker catches and finalizes the run
as ``cancelled``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timedelta, timezone
from uuid import UUID

from google.adk.evaluation.eval_set import EvalSet

from ..config import EvalParams
from ..runner import RunResult, TraceResult, run_evaluation_from_traces
from ..storage.config import StorageSettings
from ..storage.models import Run, RunStatus
from ..storage.repos import ResultRepository, RunRepository
from .fetcher import resolve_fetcher
from .result_builder import build_results, summarize_run_result
from .sinks import SinkFanout, build_sinks

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _CancelledByRequest(Exception):
    """Raised inside the worker task when the heartbeat observes cancel_requested."""


class AsyncRunWorker:
    """Manages the worker task pool. ``start()`` spawns N loops; ``stop()``
    cancels them and waits for graceful shutdown."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        results: ResultRepository,
        settings: StorageSettings,
    ) -> None:
        self._runs = runs
        self._results = results
        self._settings = settings
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._worker_id_prefix = f"{socket.gethostname()}/{id(self):x}"

    async def start(self) -> None:
        self._stopping.clear()
        for i in range(self._settings.max_concurrent_runs):
            wid = f"{self._worker_id_prefix}/{i}"
            self._tasks.append(asyncio.create_task(self._loop(wid), name=f"agentevals-worker-{i}"))
        logger.info(
            "Started %d run worker(s) (lease=%ds, heartbeat=%ds, deadline=%ds)",
            self._settings.max_concurrent_runs,
            self._settings.lease_s,
            self._settings.heartbeat_s,
            self._settings.run_deadline_s,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Run workers stopped")

    async def _loop(self, worker_id: str) -> None:
        lease = timedelta(seconds=self._settings.lease_s)
        poll = self._settings.worker_poll_interval_s
        while not self._stopping.is_set():
            try:
                run = await self._runs.claim_next(
                    worker_id=worker_id,
                    lease=lease,
                    max_attempts=self._settings.max_run_attempts,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("claim_next failed; backing off")
                await asyncio.sleep(min(poll * 5, 30.0))
                continue

            if run is None:
                try:
                    await asyncio.sleep(poll)
                except asyncio.CancelledError:
                    return
                continue

            await self._execute(run, worker_id)

    async def _execute(self, run: Run, worker_id: str) -> None:
        logger.info("worker=%s claimed run=%s (attempt=%d)", worker_id, run.run_id, run.attempt)
        cancel_event = asyncio.Event()
        hb_task = asyncio.create_task(self._heartbeat(run.run_id, worker_id, cancel_event))
        sinks = build_sinks(run.spec.sinks or [])
        try:
            await self._run_evaluation(run, sinks, cancel_event)
        except asyncio.CancelledError:
            await self._runs.update_status(run.run_id, RunStatus.CANCELLED, error="worker cancelled")
            await sinks.emit_error(run.run_id, "worker cancelled", run.attempt)
            raise
        except _CancelledByRequest:
            logger.info("run=%s cancelled by request", run.run_id)
            await self._runs.update_status(run.run_id, RunStatus.CANCELLED, error="cancelled by request")
            await sinks.emit_error(run.run_id, "cancelled by request", run.attempt)
        except TimeoutError:
            logger.warning("run=%s exceeded deadline of %ds", run.run_id, self._settings.run_deadline_s)
            await self._runs.update_status(run.run_id, RunStatus.FAILED, error="deadline_exceeded")
            await sinks.emit_error(run.run_id, "deadline_exceeded", run.attempt)
        except Exception as exc:
            logger.exception("run=%s failed", run.run_id)
            await self._runs.update_status(run.run_id, RunStatus.FAILED, error=str(exc))
            await sinks.emit_error(run.run_id, str(exc), run.attempt)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_evaluation(self, run: Run, sinks: SinkFanout, cancel_event: asyncio.Event) -> None:
        params = EvalParams.model_validate(run.spec.eval_config or {})
        eval_set: EvalSet | None = None
        if run.spec.eval_set:
            eval_set = EvalSet.model_validate(run.spec.eval_set)

        fetcher = resolve_fetcher(run.spec.target)

        async def _trace_progress(trace_result: TraceResult) -> None:
            partial = build_results(run.run_id, params, RunResult(trace_results=[trace_result]))
            await self._results.upsert_many(run.run_id, partial)
            await sinks.emit_partial(run.run_id, partial, run.attempt)
            if cancel_event.is_set():
                raise _CancelledByRequest()

        async with asyncio.timeout(self._settings.run_deadline_s):
            traces = await fetcher.fetch(run.spec.target, run.spec.context)
            if cancel_event.is_set():
                raise _CancelledByRequest()
            run_result = await run_evaluation_from_traces(
                traces=traces,
                config=params,
                eval_set=eval_set,
                trace_progress_callback=_trace_progress,
            )

        results = build_results(run.run_id, params, run_result)
        await self._results.upsert_many(run.run_id, results)
        summary = summarize_run_result(run_result)
        await sinks.emit_final(run.run_id, summary, run.attempt)
        await self._runs.update_status(run.run_id, RunStatus.SUCCEEDED, summary=summary)
        logger.info(
            "run=%s succeeded (traces=%d, results=%d)",
            run.run_id,
            len(run_result.trace_results),
            len(results),
        )

    async def _heartbeat(self, run_id: UUID, worker_id: str, cancel_event: asyncio.Event) -> None:
        lease = timedelta(seconds=self._settings.lease_s)
        interval = self._settings.heartbeat_s
        try:
            while True:
                await asyncio.sleep(interval)
                alive = await self._runs.heartbeat(run_id, worker_id, lease)
                if not alive:
                    cancel_event.set()
                    return
        except asyncio.CancelledError:
            return
