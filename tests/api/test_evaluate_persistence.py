"""Option A: /api/evaluate variants persist when run_service is configured.

These tests stub a memory-backed RunService onto app.state so we can drive
the persistence path without standing up a real Postgres. The lifespan
itself only configures run_service when AGENTEVALS_STORAGE_BACKEND=postgres,
so production behavior matches: memory backend leaves runId=null and never
writes; postgres backend persists.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentevals.api.app import create_app
from agentevals.run.service import RunService
from agentevals.storage.repos.memory import MemoryRepos

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_TRACE = REPO_ROOT / "samples" / "helm.json"


def _has_sample() -> bool:
    return SAMPLE_TRACE.exists()


@pytest.fixture
def app_no_runs():
    """No run_service injected, so /api/evaluate runs but does not persist."""
    return create_app()


@pytest.fixture
def app_with_runs():
    """Memory-backed run_service simulates the postgres-enabled deployment."""
    repos = MemoryRepos.create()
    app = create_app()
    app.state.run_service = RunService(repos.runs, repos.results)
    return app, repos


@pytest.mark.skipif(not _has_sample(), reason="samples/helm.json missing")
class TestEvaluateMultipartSync:
    def test_no_run_id_in_response_when_run_service_unset(self, app_no_runs):
        with TestClient(app_no_runs) as client:
            with SAMPLE_TRACE.open("rb") as f:
                r = client.post(
                    "/api/evaluate",
                    files={"trace_files": ("helm.json", f, "application/json")},
                    data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                )
        assert r.status_code == 200
        assert r.json()["data"].get("runId") is None

    def test_run_persisted_when_run_service_set(self, app_with_runs):
        app, repos = app_with_runs
        with TestClient(app) as client:
            with SAMPLE_TRACE.open("rb") as f:
                r = client.post(
                    "/api/evaluate",
                    files={"trace_files": ("helm.json", f, "application/json")},
                    data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                )
        assert r.status_code == 200
        run_id = r.json()["data"]["runId"]
        assert run_id is not None
        runs = asyncio.run(repos.runs.list())
        assert len(runs) == 1
        run = runs[0]
        assert str(run.run_id) == run_id
        # Status is succeeded because no top-level errors fired even though
        # the metric_result inside may have errored (no eval_set provided).
        assert run.status.value in ("succeeded", "failed")
        # The "uploaded" target kind captures audit metadata about the upload
        assert run.spec.target.kind == "uploaded"
        assert run.spec.target.trace_files == ["helm.json"]
        assert run.spec.target.trace_count == 1

    def test_results_persisted_alongside_run(self, app_with_runs):
        app, repos = app_with_runs
        with TestClient(app) as client:
            with SAMPLE_TRACE.open("rb") as f:
                r = client.post(
                    "/api/evaluate",
                    files={"trace_files": ("helm.json", f, "application/json")},
                    data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                )
        run_id = r.json()["data"]["runId"]
        results = asyncio.run(repos.results.list_by_run(_uuid(run_id)))
        assert len(results) >= 1
        for res in results:
            assert res.evaluator_type in ("builtin", "code", "remote", "openai_eval")
            assert res.run_id == _uuid(run_id)

    def test_each_call_creates_distinct_run(self, app_with_runs):
        """Multiple UI uploads accumulate in run history; each gets its own
        Run row. This is the core OSS user value of Option A."""
        app, repos = app_with_runs
        with TestClient(app) as client:
            for _ in range(3):
                with SAMPLE_TRACE.open("rb") as f:
                    client.post(
                        "/api/evaluate",
                        files={"trace_files": ("helm.json", f, "application/json")},
                        data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                    )
        runs = asyncio.run(repos.runs.list())
        assert len(runs) == 3
        assert len({r.run_id for r in runs}) == 3

    def test_persistence_failure_does_not_break_response(self, app_with_runs, monkeypatch):
        """The eval result must reach the caller even if persistence fails;
        history is best-effort, the eval contract is not."""
        app, repos = app_with_runs

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated persistence outage")

        monkeypatch.setattr(app.state.run_service, "record_completed_eval", boom)
        with TestClient(app) as client:
            with SAMPLE_TRACE.open("rb") as f:
                r = client.post(
                    "/api/evaluate",
                    files={"trace_files": ("helm.json", f, "application/json")},
                    data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                )
        assert r.status_code == 200
        assert r.json()["data"].get("runId") is None


@pytest.mark.skipif(not _has_sample(), reason="samples/helm.json missing")
class TestEvaluateSseStream:
    def test_done_event_includes_run_id_when_persisted(self, app_with_runs):
        app, _repos = app_with_runs
        with TestClient(app) as client:
            with SAMPLE_TRACE.open("rb") as f:
                with client.stream(
                    "POST",
                    "/api/evaluate/stream",
                    files={"trace_files": ("helm.json", f, "application/json")},
                    data={"config": '{"metrics": ["tool_trajectory_avg_score"]}'},
                ) as resp:
                    body = b"".join(resp.iter_bytes()).decode()
        # The done event payload is JSON in the last `data:` block.
        done_payload = _last_done_payload(body)
        assert done_payload is not None
        assert done_payload.get("result", {}).get("runId") is not None


def _last_done_payload(sse_text: str) -> dict | None:
    """Pick the SSE event whose JSON carries ``done: true`` (the SSEDoneEvent
    shape from api/models.py — ``{"done": true, "result": {...}}``)."""
    last = None
    for line in sse_text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[len("data: ") :])
        except json.JSONDecodeError:
            continue
        if payload.get("done") is True:
            last = payload
    return last


def _uuid(value):
    from uuid import UUID

    return UUID(value)
