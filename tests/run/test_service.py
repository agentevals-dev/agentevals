"""RunService unit tests against memory repos."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentevals.config import EvalParams
from agentevals.run.service import RunService, RunSubmitConflict
from agentevals.runner import MetricResult, RunResult, TraceResult
from agentevals.storage.models import RunSpec, RunStatus, TraceTarget
from agentevals.storage.repos.memory import MemoryRepos


def _spec(*, marker: str = "default") -> RunSpec:
    return RunSpec(
        approach="trace_replay",
        target=TraceTarget(kind="inline", inline={"marker": marker}),
    )


@pytest.fixture
def service():
    repos = MemoryRepos.create()
    return RunService(repos.runs, repos.results), repos


class TestRunServiceSubmit:
    async def test_first_submit_creates_run(self, service):
        svc, _ = service
        run = await svc.submit(run_id=None, spec=_spec())
        assert run.run_id is not None
        assert run.status == RunStatus.QUEUED

    async def test_resubmit_with_same_id_and_spec_idempotent(self, service):
        svc, _ = service
        run = await svc.submit(run_id=None, spec=_spec())
        again = await svc.submit(run_id=run.run_id, spec=_spec())
        assert again.run_id == run.run_id

    async def test_resubmit_with_different_spec_raises_conflict(self, service):
        """409 path: re-submitting an existing run_id with a different spec
        must NOT overwrite the persisted row, and must surface the persisted
        spec to the caller for reconciliation."""
        svc, _ = service
        run = await svc.submit(run_id=None, spec=_spec(marker="A"))
        with pytest.raises(RunSubmitConflict) as excinfo:
            await svc.submit(run_id=run.run_id, spec=_spec(marker="B"))
        # The persisted spec attached to the exception should be the original
        assert excinfo.value.persisted.spec.target.inline == {"marker": "A"}

    async def test_explicit_run_id_honored(self, service):
        svc, _ = service
        run_id = uuid4()
        run = await svc.submit(run_id=run_id, spec=_spec())
        assert run.run_id == run_id


class TestRunServiceQueries:
    async def test_get_returns_none_for_unknown(self, service):
        svc, _ = service
        assert await svc.get(uuid4()) is None

    async def test_list_returns_empty_initially(self, service):
        svc, _ = service
        assert await svc.list() == []

    async def test_list_after_submit(self, service):
        svc, _ = service
        await svc.submit(run_id=None, spec=_spec())
        await svc.submit(run_id=None, spec=_spec())
        runs = await svc.list()
        assert len(runs) == 2

    async def test_cancel_unknown_run_returns_false(self, service):
        svc, _ = service
        assert await svc.cancel(uuid4()) is False


class TestRecordCompletedEval:
    """Option A: /api/evaluate synchronously persists runs + results."""

    def _params(self) -> EvalParams:
        return EvalParams(metrics=["m1"])

    def _run_result(self, *, errors=None, metrics=None) -> RunResult:
        return RunResult(
            trace_results=[
                TraceResult(
                    trace_id="trace-1",
                    num_invocations=1,
                    metric_results=metrics or [MetricResult(metric_name="m1", eval_status="PASSED", score=0.9)],
                )
            ],
            errors=errors or [],
        )

    async def test_persists_run_as_succeeded_when_no_errors(self, service):
        svc, repos = service
        run = await svc.record_completed_eval(
            spec=_spec(),
            params=self._params(),
            run_result=self._run_result(),
        )
        assert run.status == RunStatus.SUCCEEDED
        listed = await repos.runs.list()
        assert len(listed) == 1
        assert listed[0].status == RunStatus.SUCCEEDED

    async def test_persists_run_as_failed_when_errors_present(self, service):
        svc, repos = service
        run = await svc.record_completed_eval(
            spec=_spec(),
            params=self._params(),
            run_result=self._run_result(errors=["loader failed"]),
        )
        assert run.status == RunStatus.FAILED
        assert run.error and "loader failed" in run.error
        listed = await repos.runs.list()
        assert listed[0].status == RunStatus.FAILED

    async def test_persists_result_rows(self, service):
        svc, repos = service
        run = await svc.record_completed_eval(
            spec=_spec(),
            params=self._params(),
            run_result=self._run_result(),
        )
        results = await repos.results.list_by_run(run.run_id)
        assert len(results) == 1
        assert results[0].evaluator_name == "m1"

    async def test_summary_attached_to_run(self, service):
        svc, _ = service
        run = await svc.record_completed_eval(
            spec=_spec(),
            params=self._params(),
            run_result=self._run_result(
                metrics=[
                    MetricResult(metric_name="m1", eval_status="PASSED"),
                    MetricResult(metric_name="m2", eval_status="FAILED"),
                ]
            ),
        )
        assert run.summary is not None
        assert run.summary["result_counts"]["passed"] == 1
        assert run.summary["result_counts"]["failed"] == 1

    async def test_each_call_creates_distinct_run(self, service):
        svc, repos = service
        a = await svc.record_completed_eval(spec=_spec(), params=self._params(), run_result=self._run_result())
        b = await svc.record_completed_eval(spec=_spec(), params=self._params(), run_result=self._run_result())
        assert a.run_id != b.run_id
        assert len(await repos.runs.list()) == 2
