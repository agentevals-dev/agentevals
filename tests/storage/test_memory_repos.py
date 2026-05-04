"""MemoryRepos behavior tests.

These exercise the same protocol surface that PostgresRepos implements, so
the test bodies double as a contract that future tests against a live PG can
re-use (parametrize the fixture).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from agentevals.storage.models import Result, ResultStatus, Run, RunSpec, RunStatus, TraceTarget
from agentevals.storage.repos.memory import MemoryRepos


def _make_spec() -> RunSpec:
    return RunSpec(approach="trace_replay", target=TraceTarget(kind="inline", inline={"data": []}))


def _make_run(run_id: UUID | None = None) -> Run:
    return Run(run_id=run_id or uuid4(), status=RunStatus.QUEUED, spec=_make_spec())


@pytest.fixture
def repos():
    return MemoryRepos.create()


class TestRunRepository:
    async def test_create_and_get(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        fetched = await repos.runs.get(run.run_id)
        assert fetched is not None
        assert fetched.run_id == run.run_id
        assert fetched.status == RunStatus.QUEUED

    async def test_create_idempotent_returns_existing(self, repos):
        """Resubmitting the same run_id returns the persisted row, not a new
        one; this is what makes POST /api/runs idempotent."""
        run = _make_run()
        a = await repos.runs.create(run)
        b = await repos.runs.create(run)
        assert a.run_id == b.run_id
        listed = await repos.runs.list()
        assert len(listed) == 1

    async def test_list_filters_by_status(self, repos):
        a = _make_run()
        b = _make_run()
        await repos.runs.create(a)
        await repos.runs.create(b)
        await repos.runs.update_status(a.run_id, RunStatus.SUCCEEDED)
        succeeded = await repos.runs.list(status=[RunStatus.SUCCEEDED])
        queued = await repos.runs.list(status=[RunStatus.QUEUED])
        assert {r.run_id for r in succeeded} == {a.run_id}
        assert {r.run_id for r in queued} == {b.run_id}

    async def test_list_respects_limit(self, repos):
        for _ in range(5):
            await repos.runs.create(_make_run())
        page = await repos.runs.list(limit=3)
        assert len(page) == 3

    async def test_claim_next_picks_oldest_queued(self, repos):
        first = _make_run()
        second = _make_run()
        await repos.runs.create(first)
        await repos.runs.create(second)
        claimed = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
        assert claimed is not None
        assert claimed.run_id == first.run_id
        assert claimed.status == RunStatus.RUNNING
        assert claimed.attempt == 1

    async def test_claim_next_returns_none_when_empty(self, repos):
        result = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
        assert result is None

    async def test_claim_respects_max_attempts(self, repos):
        """A run that has exceeded max_attempts is invisible to claim_next so
        a poison run cannot starve fresh queued work via repeated re-claims."""
        run = _make_run()
        await repos.runs.create(run)
        for _ in range(3):
            claimed = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
            if claimed is None:
                break
            await repos.runs.update_status(claimed.run_id, RunStatus.QUEUED)
        # Reset to QUEUED but with attempt=3 already
        run_now = await repos.runs.get(run.run_id)
        assert run_now is not None
        assert run_now.attempt >= 3
        none_claimed = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
        assert none_claimed is None

    async def test_heartbeat_returns_false_for_unknown_run(self, repos):
        alive = await repos.runs.heartbeat(uuid4(), "w1", timedelta(seconds=30))
        assert alive is False

    async def test_heartbeat_returns_false_when_cancel_requested(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        claimed = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
        assert claimed is not None
        await repos.runs.cancel(claimed.run_id)
        alive = await repos.runs.heartbeat(claimed.run_id, "w1", timedelta(seconds=30))
        assert alive is False

    async def test_cancel_queued_run_marks_cancelled(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        ok = await repos.runs.cancel(run.run_id)
        assert ok is True
        fresh = await repos.runs.get(run.run_id)
        assert fresh is not None
        assert fresh.status == RunStatus.CANCELLED

    async def test_cancel_running_run_sets_flag_only(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        claimed = await repos.runs.claim_next(worker_id="w1", lease=timedelta(seconds=30), max_attempts=3)
        assert claimed is not None
        ok = await repos.runs.cancel(claimed.run_id)
        assert ok is True
        fresh = await repos.runs.get(claimed.run_id)
        assert fresh is not None
        assert fresh.status == RunStatus.RUNNING
        assert fresh.cancel_requested is True

    async def test_cancel_terminal_run_returns_false(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        await repos.runs.update_status(run.run_id, RunStatus.SUCCEEDED)
        ok = await repos.runs.cancel(run.run_id)
        assert ok is False

    async def test_update_status_sets_finished_at_for_terminal(self, repos):
        run = _make_run()
        await repos.runs.create(run)
        await repos.runs.update_status(run.run_id, RunStatus.SUCCEEDED, summary={"k": "v"})
        fresh = await repos.runs.get(run.run_id)
        assert fresh is not None
        assert fresh.finished_at is not None
        assert fresh.summary == {"k": "v"}


class TestResultRepository:
    def _make_result(self, run_id: UUID, suffix: str = "") -> Result:
        return Result(
            result_id=f"hash-{run_id}-{suffix}",
            run_id=run_id,
            eval_set_item_id=f"item-{suffix}",
            eval_set_item_name=f"trace-{suffix}",
            evaluator_name="m1",
            evaluator_type="builtin",
            status=ResultStatus.PASSED,
            score=0.9,
        )

    async def test_upsert_many_persists_results(self, repos):
        run_id = uuid4()
        results = [self._make_result(run_id, "a"), self._make_result(run_id, "b")]
        await repos.results.upsert_many(run_id, results)
        listed = await repos.results.list_by_run(run_id)
        assert len(listed) == 2
        assert {r.result_id for r in listed} == {results[0].result_id, results[1].result_id}

    async def test_upsert_many_idempotent_on_result_id(self, repos):
        """Re-upserting the same result_id replaces the row so retried
        webhook posts and worker re-execution stay deduplicated."""
        run_id = uuid4()
        first = self._make_result(run_id, "a")
        await repos.results.upsert_many(run_id, [first])
        first.score = 0.5
        await repos.results.upsert_many(run_id, [first])
        listed = await repos.results.list_by_run(run_id)
        assert len(listed) == 1
        assert listed[0].score == 0.5

    async def test_empty_upsert_is_noop(self, repos):
        run_id = uuid4()
        await repos.results.upsert_many(run_id, [])
        listed = await repos.results.list_by_run(run_id)
        assert listed == []

    async def test_delete_by_run(self, repos):
        run_id = uuid4()
        await repos.results.upsert_many(run_id, [self._make_result(run_id, "a")])
        await repos.results.delete_by_run(run_id)
        assert await repos.results.list_by_run(run_id) == []


class TestSessionRepository:
    """SessionRepository is forward-compat scaffolding in this slice; cover
    the basic CRUD surface so regressions surface if the protocol drifts."""

    async def test_upsert_and_get(self, repos):
        from agentevals.streaming.session import TraceSession

        s = TraceSession(session_id="sess-1", trace_id="t-1", eval_set_id=None)
        s.trace_ids.add("t-1")
        await repos.sessions.upsert(s)
        fetched = await repos.sessions.get("sess-1")
        assert fetched is not None
        assert fetched.session_id == "sess-1"

    async def test_find_by_trace_id(self, repos):
        from agentevals.streaming.session import TraceSession

        s = TraceSession(session_id="sess-1", trace_id="t-1", eval_set_id=None)
        s.trace_ids.update({"t-1", "t-2"})
        await repos.sessions.upsert(s)
        match = await repos.sessions.find_by_trace_id("t-2")
        assert match is not None
        assert match.session_id == "sess-1"

    async def test_delete(self, repos):
        from agentevals.streaming.session import TraceSession

        await repos.sessions.upsert(TraceSession(session_id="sess-1", trace_id="t-1", eval_set_id=None))
        await repos.sessions.delete("sess-1")
        assert await repos.sessions.get("sess-1") is None
