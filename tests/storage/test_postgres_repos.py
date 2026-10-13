"""Postgres run and result repositories against a live database.

Runs only when AGENTEVALS_TEST_DATABASE_URL points at a disposable Postgres; each test gets a fresh
schema, dropped afterwards.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from agentevals.storage.models import Result, ResultStatus, Run, RunSpec, RunStatus, TraceTarget
from agentevals.storage.postgres.migrator import Migrator
from agentevals.storage.repos.postgres import PostgresRepos

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTEVALS_TEST_DATABASE_URL"),
    reason="requires AGENTEVALS_TEST_DATABASE_URL pointing at a disposable Postgres",
)

SCHEMA = "agentevals_test_repos"
LEASE = timedelta(seconds=30)
MAX_ATTEMPTS = 3
EXPIRED = timedelta(minutes=-1)


@pytest.fixture
async def db():
    import asyncpg

    dsn = os.environ["AGENTEVALS_TEST_DATABASE_URL"]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    await pool.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    await Migrator(dsn=dsn, schema=SCHEMA, lock_timeout_s=10).up()
    try:
        yield PostgresRepos(pool=pool, schema=SCHEMA), pool
    finally:
        await pool.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await pool.close()


async def _queued(repos: PostgresRepos) -> UUID:
    run = Run(run_id=uuid4(), status=RunStatus.QUEUED, spec=RunSpec(target=TraceTarget(kind="inline", inline={})))
    return (await repos.runs.create(run)).run_id


async def _running(pool, run_id: UUID, *, attempt: int, lease_left: timedelta, cancel: bool = False) -> None:
    await pool.execute(
        f"""
        UPDATE "{SCHEMA}".run
           SET status = 'running', worker_id = 'gone-worker', claimed_at = now() - interval '5 minutes',
               started_at = now() - interval '5 minutes', lease_expires_at = now() + $2,
               attempt = $3, cancel_requested = $4
         WHERE run_id = $1
        """,
        run_id,
        lease_left,
        attempt,
        cancel,
    )


async def _claim(repos: PostgresRepos) -> Run | None:
    return await repos.runs.claim_next(worker_id="new-worker", lease=LEASE, max_attempts=MAX_ATTEMPTS)


class TestResults:
    async def test_unscored_invocations_keep_their_position(self, db):
        repos, _ = db
        run_id = await _queued(repos)
        result = Result(
            result_id="r1",
            run_id=run_id,
            eval_set_item_id="item",
            eval_set_item_name="trace",
            evaluator_name="tool_trajectory_avg_score",
            evaluator_type="builtin",
            status=ResultStatus.PASSED,
            score=0.75,
            per_invocation_scores=[0.5, None, 1.0],
        )
        await repos.results.upsert_many(run_id, [result])
        [stored] = await repos.results.list_by_run(run_id)
        assert stored.per_invocation_scores == [0.5, None, 1.0]

        await repos.results.upsert_many(run_id, [result.model_copy(update={"per_invocation_scores": [None]})])
        [updated] = await repos.results.list_by_run(run_id)
        assert updated.per_invocation_scores == [None]


class TestExpiredLeases:
    async def test_the_last_attempt_fails_the_run(self, db):
        repos, pool = db
        run_id = await _queued(repos)
        await _running(pool, run_id, attempt=MAX_ATTEMPTS, lease_left=EXPIRED)
        assert await _claim(repos) is None
        run = await repos.runs.get(run_id)
        assert (run.status, run.error) == (RunStatus.FAILED, "worker lease expired on the last attempt")
        assert run.finished_at is not None

    async def test_a_cancel_request_cancels_the_run(self, db):
        repos, pool = db
        run_id = await _queued(repos)
        await _running(pool, run_id, attempt=1, lease_left=EXPIRED, cancel=True)
        assert await _claim(repos) is None
        run = await repos.runs.get(run_id)
        assert (run.status, run.error) == (RunStatus.CANCELLED, None)
        assert run.finished_at is not None

    async def test_a_run_with_attempts_left_is_claimed_again(self, db):
        repos, pool = db
        run_id = await _queued(repos)
        await _running(pool, run_id, attempt=1, lease_left=EXPIRED)
        claimed = await _claim(repos)
        assert (claimed.run_id, claimed.attempt, claimed.worker_id) == (run_id, 2, "new-worker")
        assert claimed.status == RunStatus.RUNNING

    async def test_a_live_lease_is_left_alone(self, db):
        repos, pool = db
        run_id = await _queued(repos)
        await _running(pool, run_id, attempt=MAX_ATTEMPTS, lease_left=-EXPIRED)
        assert await _claim(repos) is None
        run = await repos.runs.get(run_id)
        assert (run.status, run.worker_id, run.finished_at) == (RunStatus.RUNNING, "gone-worker", None)

    async def test_finished_runs_are_never_touched(self, db):
        repos, pool = db
        run_id = await _queued(repos)
        await _running(pool, run_id, attempt=MAX_ATTEMPTS, lease_left=EXPIRED)
        await repos.runs.update_status(run_id, RunStatus.SUCCEEDED, summary={"passed": 1})
        before = await repos.runs.get(run_id)
        assert await _claim(repos) is None
        assert await repos.runs.get(run_id) == before
