"""HTTP-level tests for /api/runs endpoints."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agentevals.api.app import create_app
from agentevals.run.service import RunService
from agentevals.storage.repos.memory import MemoryRepos


@pytest.fixture
def memory_app(monkeypatch):
    """App with the storage env unset; backend defaults to memory and
    /api/runs handlers should return 503 with a configuration hint."""
    for var in ("AGENTEVALS_STORAGE_BACKEND", "AGENTEVALS_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return create_app()


@pytest.fixture
def stubbed_app(memory_app):
    """App that has a memory-backed RunService injected onto app.state, so
    we can exercise /api/runs handler logic without standing up a real PG."""
    repos = MemoryRepos.create()
    memory_app.state.run_service = RunService(repos.runs, repos.results)
    return memory_app, repos


class TestMemoryBackendReturns503:
    def test_get_runs(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.get("/api/runs")
        assert r.status_code == 503
        assert "AGENTEVALS_STORAGE_BACKEND=postgres" in r.json()["detail"]

    def test_post_run(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.post(
                "/api/runs",
                json={"spec": {"approach": "trace_replay", "target": {"kind": "inline", "inline": {}}}},
            )
        assert r.status_code == 503

    def test_get_run_by_id(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.get(f"/api/runs/{uuid4()}")
        assert r.status_code == 503

    def test_get_run_results(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.get(f"/api/runs/{uuid4()}/results")
        assert r.status_code == 503

    def test_cancel_run(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.post(f"/api/runs/{uuid4()}/cancel")
        assert r.status_code == 503

    def test_health_endpoint_unaffected(self, memory_app):
        with TestClient(memory_app) as client:
            r = client.get("/api/health")
        assert r.status_code == 200


class TestSubmitRun:
    def _payload(self, *, marker="x"):
        return {
            "spec": {
                "approach": "trace_replay",
                "target": {"kind": "inline", "inline": {"m": marker}},
            }
        }

    def test_submit_returns_202(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r = client.post("/api/runs", json=self._payload())
        assert r.status_code == 202
        body = r.json()
        assert body["data"]["status"] == "queued"
        assert body["data"]["runId"]

    def test_submit_with_explicit_id(self, stubbed_app):
        app, _ = stubbed_app
        run_id = "11111111-1111-1111-1111-111111111111"
        payload = {**self._payload(), "runId": run_id}
        with TestClient(app) as client:
            r = client.post("/api/runs", json=payload)
        assert r.status_code == 202
        assert r.json()["data"]["runId"] == run_id

    def test_idempotent_resubmit_same_spec(self, stubbed_app):
        app, _ = stubbed_app
        run_id = "22222222-2222-2222-2222-222222222222"
        payload = {**self._payload(marker="same"), "runId": run_id}
        with TestClient(app) as client:
            r1 = client.post("/api/runs", json=payload)
            r2 = client.post("/api/runs", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["data"]["runId"] == r2.json()["data"]["runId"]

    def test_resubmit_with_different_spec_returns_409(self, stubbed_app):
        app, _ = stubbed_app
        run_id = "33333333-3333-3333-3333-333333333333"
        with TestClient(app) as client:
            r1 = client.post("/api/runs", json={**self._payload(marker="A"), "runId": run_id})
            r2 = client.post("/api/runs", json={**self._payload(marker="B"), "runId": run_id})
        assert r1.status_code == 202
        assert r2.status_code == 409
        body = r2.json()
        assert "already exists" in body["detail"]["message"]
        assert body["detail"]["persisted"]["runId"] == run_id

    def test_invalid_target_kind_rejected(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r = client.post(
                "/api/runs",
                json={"spec": {"approach": "trace_replay", "target": {"kind": "not-a-kind"}}},
            )
        assert r.status_code == 422

    def test_invalid_legacy_eval_config_rejected(self, stubbed_app):
        app, _ = stubbed_app
        payload = self._payload()
        payload["spec"]["evalConfig"] = {"judgeModel": "gemini-2.5-flash"}
        with TestClient(app) as client:
            r = client.post("/api/runs", json=payload)
        assert r.status_code == 422
        assert "Invalid eval_config" in r.json()["detail"]


class TestGetAndListRuns:
    def test_unknown_run_id_returns_404(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r = client.get(f"/api/runs/{uuid4()}")
        assert r.status_code == 404

    def test_list_empty_then_after_submit(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r1 = client.get("/api/runs")
            assert r1.json()["data"] == []
            client.post(
                "/api/runs",
                json={"spec": {"approach": "trace_replay", "target": {"kind": "inline", "inline": {}}}},
            )
            r2 = client.get("/api/runs")
            assert len(r2.json()["data"]) == 1

    def test_list_status_filter(self, stubbed_app):
        app, repos = stubbed_app
        with TestClient(app) as client:
            client.post(
                "/api/runs",
                json={"spec": {"approach": "trace_replay", "target": {"kind": "inline", "inline": {}}}},
            )
            r = client.get("/api/runs?status=queued")
            assert len(r.json()["data"]) == 1
            r = client.get("/api/runs?status=succeeded")
            assert r.json()["data"] == []


class TestCancelRun:
    def test_cancel_unknown_run_404(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r = client.post(f"/api/runs/{uuid4()}/cancel")
        assert r.status_code == 404

    def test_cancel_queued_run_marks_cancelled(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            sub = client.post(
                "/api/runs",
                json={"spec": {"approach": "trace_replay", "target": {"kind": "inline", "inline": {}}}},
            )
            run_id = sub.json()["data"]["runId"]
            r = client.post(f"/api/runs/{run_id}/cancel")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "cancelled"

    def test_get_run_results_for_unknown_run_404(self, stubbed_app):
        app, _ = stubbed_app
        with TestClient(app) as client:
            r = client.get(f"/api/runs/{uuid4()}/results")
        assert r.status_code == 404


MARK = "qxmark"


def _secret_payload(**spec):
    return {
        "spec": {
            "approach": "trace_replay",
            "target": {
                "kind": "http",
                "baseUrl": f"https://reader:{MARK}@traces.example.com/api?key={MARK}",
                "traceId": "1dbd779ff94bf24c7b84678a5dc0346c",
            },
            "context": {"headers": {"Authorization": f"Bearer {MARK}"}},
            "sinks": [
                {
                    "kind": "http_webhook",
                    "url": f"https://hooks.example.com/services/{MARK}",
                    "headers": {"X-Api-Key": MARK, "X-Signature": {"from_env": "HOOK_SIGNATURE"}},
                }
            ],
            **spec,
        }
    }


class TestSecretsStayOutOfResponses:
    def test_every_read_is_redacted(self, stubbed_app):
        app, repos = stubbed_app
        with TestClient(app) as client:
            submitted = client.post("/api/runs", json=_secret_payload())
            run_id = submitted.json()["data"]["runId"]
            responses = [
                submitted,
                client.get(f"/api/runs/{run_id}"),
                client.get("/api/runs"),
                client.post(f"/api/runs/{run_id}/cancel"),
            ]
        for response in responses:
            assert response.status_code in (200, 202)
            assert MARK not in response.text
        spec = responses[1].json()["data"]["spec"]
        assert spec["target"]["baseUrl"] == "https://traces.example.com/api"
        assert spec["context"]["headers"]["Authorization"] == "REDACTED"
        assert spec["sinks"][0]["url"] == "https://hooks.example.com/REDACTED"
        assert spec["sinks"][0]["headers"]["X-Signature"] == {"from_env": "HOOK_SIGNATURE"}

    async def test_the_stored_run_keeps_the_real_values(self, stubbed_app):
        app, repos = stubbed_app
        with TestClient(app) as client:
            run_id = client.post("/api/runs", json=_secret_payload()).json()["data"]["runId"]
        stored = await repos.runs.get(UUID(run_id))
        assert stored.spec.context["headers"]["Authorization"] == f"Bearer {MARK}"
        assert stored.spec.sinks[0]["url"] == f"https://hooks.example.com/services/{MARK}"

    def test_conflict_body_is_redacted(self, stubbed_app):
        app, _ = stubbed_app
        run_id = "44444444-4444-4444-4444-444444444444"
        with TestClient(app) as client:
            client.post("/api/runs", json={**_secret_payload(), "runId": run_id})
            r = client.post("/api/runs", json={**_secret_payload(eval_set={"eval_set_id": "x"}), "runId": run_id})
        assert r.status_code == 409
        assert MARK not in r.text
        assert r.json()["detail"]["persisted"]["runId"] == run_id

    @pytest.mark.parametrize("trace_id", ["../../admin", "1dbd779ff94bf24c/x", "1dbd779ff94bf24"])
    def test_http_target_needs_a_hex_trace_id(self, stubbed_app, trace_id):
        app, _ = stubbed_app
        payload = _secret_payload()
        payload["spec"]["target"]["traceId"] = trace_id
        with TestClient(app) as client:
            r = client.post("/api/runs", json=payload)
        assert r.status_code == 422
        assert "trace_id" in r.json()["detail"]
