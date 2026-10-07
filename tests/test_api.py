"""API endpoint contract tests.

Tests the HTTP API contract: StandardResponse envelope, camelCase serialization,
status codes, input validation, and error responses. Business logic is mocked —
see test_runner.py, test_converter.py, etc. for those tests.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import urllib.parse
import zipfile
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentevals.api.debug_routes import debug_router
from agentevals.api.models import (
    CamelModel,
    CreateEvalSetData,
    HealthData,
    MetricInfo,
    SessionInfo,
    StandardResponse,
)
from agentevals.api.routes import _camel_keys, router
from agentevals.api.streaming_routes import streaming_router
from agentevals.runner import MetricResult, RunResult, TraceResult
from agentevals.streaming.exports import EXPORT_DIR
from agentevals.streaming.session import TraceSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")
_KEY_EXCEPTIONS = {"p50", "p95", "p99", "id"}


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(debug_router, prefix="/api/debug")
    return app


def _make_live_app(mgr) -> FastAPI:
    app = _make_app()
    app.include_router(streaming_router, prefix="/api/streaming")
    app.state.trace_manager = mgr
    return app


def _make_session(
    session_id="sess1",
    trace_id="trace1",
    is_complete=True,
    spans=None,
    logs=None,
    invocations=None,
    eval_set_id=None,
    metadata=None,
) -> TraceSession:
    return TraceSession(
        session_id=session_id,
        trace_id=trace_id,
        eval_set_id=eval_set_id,
        spans=spans or [],
        logs=logs or [],
        is_complete=is_complete,
        metadata=metadata or {},
        invocations=invocations or [],
    )


def _make_run_result() -> RunResult:
    return RunResult(
        trace_results=[
            TraceResult(
                trace_id="abc123",
                num_invocations=2,
                metric_results=[
                    MetricResult(
                        metric_name="tool_trajectory_avg_score",
                        score=0.85,
                        eval_status="PASSED",
                    )
                ],
                performance_metrics={
                    "latency": {
                        "overall": {
                            "min": 100.0,
                            "median": 120.0,
                            "max": 300.0,
                            "count": 2,
                            "p50": 120.0,
                            "p95": 250.0,
                            "p99": 300.0,
                        },
                        "llm_calls": {
                            "min": 60.0,
                            "median": 80.0,
                            "max": 200.0,
                            "count": 3,
                            "p50": 80.0,
                            "p95": 150.0,
                            "p99": 200.0,
                        },
                        "tool_executions": {
                            "min": 10.0,
                            "median": 20.0,
                            "max": 50.0,
                            "count": 2,
                            "p50": 20.0,
                            "p95": 40.0,
                            "p99": 50.0,
                        },
                    },
                    "tokens": {
                        "total_prompt": 500,
                        "total_output": 200,
                        "total": 700,
                        "per_llm_call": {
                            "min": 200.0,
                            "median": 350.0,
                            "max": 700.0,
                            "count": 3,
                            "p50": 350.0,
                            "p95": 600.0,
                            "p99": 700.0,
                        },
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 0,
                    },
                    "counts": {
                        "llm_calls": 3,
                        "tool_calls": 2,
                        "invocations": 2,
                    },
                    "models": ["gemini-2.0-flash"],
                    "tool_names": ["google_search", "read_file"],
                },
            )
        ],
    )


def _make_eval_set_json() -> bytes:
    return json.dumps(
        {
            "eval_set_id": "test_eval",
            "eval_cases": [
                {
                    "eval_id": "case_1",
                    "conversation": [
                        {
                            "invocation_id": "inv_1",
                            "user_content": {"role": "user", "parts": [{"text": "hello"}]},
                            "final_response": {"role": "model", "parts": [{"text": "hi"}]},
                        }
                    ],
                }
            ],
        }
    ).encode()


def _make_trace_json() -> bytes:
    return json.dumps(
        {
            "data": [
                {
                    "traceID": "abc123",
                    "spans": [
                        {
                            "traceID": "abc123",
                            "spanID": "span1",
                            "operationName": "test",
                            "startTime": 1000000,
                            "duration": 500000,
                            "tags": [],
                            "logs": [],
                            "processID": "p1",
                            "references": [],
                        }
                    ],
                    "processes": {"p1": {"serviceName": "test", "tags": []}},
                }
            ]
        }
    ).encode()


def _assert_envelope(response, status=200):
    assert response.status_code == status, f"Expected {status}, got {response.status_code}: {response.text}"
    body = response.json()
    assert "data" in body, f"Missing 'data' key in response: {body}"
    assert "error" in body, f"Missing 'error' key in response: {body}"
    return body


def _assert_all_keys_camel(obj, path=""):
    if isinstance(obj, dict):
        for key in obj:
            full_path = f"{path}.{key}" if path else key
            assert _CAMEL_RE.match(key) or key in _KEY_EXCEPTIONS, f"Key {full_path!r} is not camelCase"
            _assert_all_keys_camel(obj[key], full_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _assert_all_keys_camel(item, f"{path}[{i}]")


def _make_trace_manager():
    from agentevals.streaming.ws_server import StreamingTraceManager

    mgr = StreamingTraceManager()
    mgr.broadcast_to_ui = AsyncMock()
    return mgr


def _eval_config_json(**overrides) -> str:
    cfg = {"evaluators": [{"name": "tool_trajectory_avg_score", "type": "builtin"}]}
    cfg.update(overrides)
    return json.dumps(cfg)


def _judge_config(**overrides) -> dict:
    cfg = {
        "evaluators": [
            {"name": "hallucinations_v1", "type": "builtin", "judgeModel": "openai/gpt-4o", "credentialRef": "k"}
        ]
    }
    cfg.update(overrides)
    return cfg


def _capturing_run_eval(captured: dict):
    """Build an AsyncMock side_effect that records, at evaluator-invocation time, the value the
    judge would resolve for credential ``k``.

    This is the correct boundary for the sync routes: their job is to populate the credential
    ContextVar before the evaluator runs. The ContextVar -> judge injection step itself is
    already covered by test_credential_injection.py, so recording ``get_resolved_credential``
    here (rather than mocking it) is not a false positive -- it fails when the route omits the
    set/reset, which is exactly the gap being closed.
    """
    from agentevals.resolvers import get_resolved_credential

    def _side_effect(*args, **kwargs):
        captured["judge_key"] = get_resolved_credential("k")
        return _make_run_result()

    return _side_effect


def _capturing_paths(captured: dict):
    """Record the on-disk upload paths the route hands to ``run_evaluation``.

    The path traversal fix lives in how the route names the saved file, so the
    boundary that proves it is exactly the ``EvalRunConfig`` the evaluator receives.
    Existence is captured at call time, before the route's ``finally`` cleans the temp dir.
    """

    def _side_effect(cfg, *args, **kwargs):
        captured["trace_files"] = list(cfg.trace_files)
        captured["eval_set_file"] = cfg.eval_set_file
        captured["existed"] = {p: os.path.exists(p) for p in cfg.trace_files}
        return _make_run_result()

    return _side_effect


_UI_INDEX = "<!doctype html><title>agentevals-ui-index-sentinel</title>"
_UI_ASSET = "export const marker = 'ui-asset-sentinel';"


def _make_ui_client(static_dir, monkeypatch) -> TestClient:
    """Build the real app over a throwaway static dir so the SPA fallback is exercised, not copied."""
    from agentevals.api.app import create_app

    monkeypatch.delenv("AGENTEVALS_HEADLESS", raising=False)
    (static_dir / "assets").mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(_UI_INDEX)
    (static_dir / "assets" / "app.js").write_text(_UI_ASSET)
    return TestClient(create_app(static_dir=static_dir))


# ---------------------------------------------------------------------------
# Model Serialization
# ---------------------------------------------------------------------------


class TestModelSerialization:
    def test_standard_response_envelope(self):
        resp = StandardResponse(data=HealthData(status="ok", version="1.0"))
        dumped = resp.model_dump(by_alias=True)
        assert dumped == {"data": {"status": "ok", "version": "1.0"}, "error": None}

    def test_metric_info_requires_llm_alias(self):
        m = MetricInfo(
            name="test",
            category="test",
            requires_eval_set=False,
            requires_llm=True,
            requires_gcp=False,
            requires_rubrics=False,
            description="test",
            working=True,
        )
        dumped = m.model_dump(by_alias=True)
        assert "requiresLLM" in dumped
        assert "requiresLlm" not in dumped
        assert dumped["requiresLLM"] is True

    def test_metric_info_requires_gcp_alias(self):
        m = MetricInfo(
            name="test",
            category="test",
            requires_eval_set=False,
            requires_llm=False,
            requires_gcp=True,
            requires_rubrics=False,
            description="test",
            working=True,
        )
        dumped = m.model_dump(by_alias=True)
        assert "requiresGCP" in dumped
        assert "requiresGcp" not in dumped
        assert dumped["requiresGCP"] is True

    def test_session_info_camel_keys(self):
        s = SessionInfo(
            session_id="s1",
            trace_id="t1",
            span_count=5,
            is_complete=True,
            started_at="2024-01-01T00:00:00",
        )
        dumped = s.model_dump(by_alias=True)
        assert "sessionId" in dumped
        assert "spanCount" in dumped
        assert "isComplete" in dumped
        assert "startedAt" in dumped
        assert "session_id" not in dumped

    def test_run_result_nested_camel(self):
        result = _make_run_result()
        dumped = result.model_dump(by_alias=True)
        assert "traceResults" in dumped
        tr = dumped["traceResults"][0]
        assert "traceId" in tr
        assert "numInvocations" in tr
        assert "metricResults" in tr
        mr = tr["metricResults"][0]
        assert "metricName" in mr
        assert "evalStatus" in mr
        assert "perInvocationScores" in mr

    def test_camel_keys_helper(self):
        result = _camel_keys(
            {
                "llm_calls": {"p50": 1.0},
                "total_prompt": 5,
                "already_camel": [{"nested_key": True}],
            }
        )
        assert result == {
            "llmCalls": {"p50": 1.0},
            "totalPrompt": 5,
            "alreadyCamel": [{"nestedKey": True}],
        }


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    def test_health_success(self):
        body = _assert_envelope(self.client.get("/api/health"))
        assert body["data"]["status"] == "ok"
        assert "version" in body["data"]
        assert body["error"] is None

    def test_health_camel_keys(self):
        body = self.client.get("/api/health").json()
        _assert_all_keys_camel(body)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


class TestConfigEndpoint:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    def test_config_no_keys(self):
        env = {
            "GOOGLE_API_KEY": "",
            "GEMINI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            body = _assert_envelope(self.client.get("/api/config"))
        keys = body["data"]["apiKeys"]
        assert keys == {"google": False, "anthropic": False, "openai": False}

    def test_config_with_keys(self):
        env = {
            "GOOGLE_API_KEY": "test-key",
            "GEMINI_API_KEY": "",
            "ANTHROPIC_API_KEY": "test-key",
            "OPENAI_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            body = _assert_envelope(self.client.get("/api/config"))
        keys = body["data"]["apiKeys"]
        assert keys["google"] is True
        assert keys["anthropic"] is True
        assert keys["openai"] is False


# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    def test_metrics_fallback(self):
        with patch.dict("sys.modules", {"google.adk.evaluation.metric_evaluator_registry": None}):
            body = _assert_envelope(self.client.get("/api/metrics"))
        assert len(body["data"]) == 11

    def test_metrics_envelope(self):
        body = _assert_envelope(self.client.get("/api/metrics"))
        assert isinstance(body["data"], list)
        assert body["error"] is None

    def test_metrics_all_camel(self):
        body = self.client.get("/api/metrics").json()
        _assert_all_keys_camel(body)
        for m in body["data"]:
            assert "requiresLLM" in m
            assert "requiresGCP" in m
            assert "requiresEvalSet" in m
            assert "requiresRubrics" in m


# ---------------------------------------------------------------------------
# POST /api/validate/eval-set
# ---------------------------------------------------------------------------


class TestValidateEvalSet:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    def test_validate_valid(self):
        body = _assert_envelope(
            self.client.post(
                "/api/validate/eval-set",
                files={"eval_set_file": ("eval.json", io.BytesIO(_make_eval_set_json()))},
            )
        )
        assert body["data"]["valid"] is True
        assert body["data"]["evalSetId"] == "test_eval"
        assert body["data"]["numCases"] == 1

    def test_validate_invalid_json(self):
        body = _assert_envelope(
            self.client.post(
                "/api/validate/eval-set",
                files={"eval_set_file": ("eval.json", io.BytesIO(b"not json"))},
            )
        )
        assert body["data"]["valid"] is False
        assert len(body["data"]["errors"]) > 0

    def test_validate_missing_fields(self):
        body = _assert_envelope(
            self.client.post(
                "/api/validate/eval-set",
                files={"eval_set_file": ("eval.json", io.BytesIO(b"{}"))},
            )
        )
        assert body["data"]["valid"] is False


# ---------------------------------------------------------------------------
# POST /api/evaluate
# ---------------------------------------------------------------------------


class TestEvaluateTraces:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_success(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        body = _assert_envelope(resp)
        assert "traceResults" in body["data"]
        assert len(body["data"]["traceResults"]) == 1

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_camel_keys_in_result(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        body = resp.json()
        _assert_all_keys_camel(body)
        tr = body["data"]["traceResults"][0]
        perf = tr["performanceMetrics"]
        assert "llmCalls" in perf["latency"]
        assert "toolExecutions" in perf["latency"]
        assert "totalPrompt" in perf["tokens"]
        assert "totalOutput" in perf["tokens"]
        assert "perLlmCall" in perf["tokens"]

    def test_evaluate_invalid_config(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": "not json"},
        )
        assert resp.status_code == 400

    def test_evaluate_wrong_extension(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.txt", io.BytesIO(b"data"))},
            data={"config": _eval_config_json()},
        )
        assert resp.status_code == 400

    def test_evaluate_rejects_legacy_metrics_field(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": json.dumps({"metrics": ["tool_trajectory_avg_score"]})},
        )
        assert resp.status_code == 400

    def test_evaluate_threshold_out_of_range(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={
                "config": _eval_config_json(
                    evaluators=[{"name": "tool_trajectory_avg_score", "type": "builtin", "threshold": 1.5}]
                )
            },
        )
        assert resp.status_code == 400

    def test_evaluate_no_files(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("", io.BytesIO(b""))},
            data={"config": _eval_config_json()},
        )
        assert resp.status_code in (400, 422)

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_resolves_credential_refs(self, mock_eval, monkeypatch):
        monkeypatch.setenv("AE_TEST_JUDGE_KEY", "sk-resolved-multipart")
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={
                "config": json.dumps(_judge_config()),
                "credential_refs": json.dumps({"k": {"kind": "env", "name": "AE_TEST_JUDGE_KEY"}}),
            },
        )
        _assert_envelope(resp)
        assert captured["judge_key"] == "sk-resolved-multipart"

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_without_credential_refs_is_noop(self, mock_eval):
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        _assert_envelope(resp)
        assert captured["judge_key"] is None

    def test_evaluate_bad_credential_refs_returns_400(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json(), "credential_refs": "{not json"},
        )
        assert resp.status_code == 400
        assert "credentialRefs" in resp.json()["detail"]

    def test_evaluate_credential_refs_wrong_shape_returns_400(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json(), "credential_refs": json.dumps(["not", "a", "map"])},
        )
        assert resp.status_code == 400
        assert "credentialRefs" in resp.json()["detail"]

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_unresolvable_credential_returns_400(self, mock_eval, monkeypatch):
        monkeypatch.delenv("AE_MISSING_KEY", raising=False)
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={
                "config": json.dumps(_judge_config()),
                "credential_refs": json.dumps({"k": {"kind": "env", "name": "AE_MISSING_KEY"}}),
            },
        )
        assert resp.status_code == 400
        assert "Could not resolve credentialRefs" in resp.json()["detail"]
        mock_eval.assert_not_called()

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_sanitizes_traversal_trace_filename(self, mock_eval):
        captured: dict = {}
        mock_eval.side_effect = _capturing_paths(captured)
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("../../outside.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        _assert_envelope(resp)
        saved = captured["trace_files"][0]
        assert os.path.basename(saved) == "0_outside.json"
        assert ".." not in saved
        assert captured["existed"][saved] is True

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_sanitizes_traversal_eval_set_filename(self, mock_eval):
        captured: dict = {}
        mock_eval.side_effect = _capturing_paths(captured)
        resp = self.client.post(
            "/api/evaluate",
            files={
                "trace_files": ("trace.json", io.BytesIO(_make_trace_json())),
                "eval_set_file": ("../../outside.json", io.BytesIO(_make_eval_set_json())),
            },
            data={"config": _eval_config_json()},
        )
        _assert_envelope(resp)
        eval_set = captured["eval_set_file"]
        assert os.path.basename(eval_set) == "evalset_outside.json"
        assert ".." not in eval_set

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    def test_evaluate_duplicate_filenames_do_not_clobber(self, mock_eval):
        captured: dict = {}
        mock_eval.side_effect = _capturing_paths(captured)
        resp = self.client.post(
            "/api/evaluate",
            files=[
                ("trace_files", ("same.json", io.BytesIO(_make_trace_json()))),
                ("trace_files", ("same.json", io.BytesIO(_make_trace_json()))),
            ],
            data={"config": _eval_config_json()},
        )
        _assert_envelope(resp)
        names = [os.path.basename(p) for p in captured["trace_files"]]
        assert names == ["0_same.json", "1_same.json"]


# ---------------------------------------------------------------------------
# POST /api/evaluate/stream (SSE)
# ---------------------------------------------------------------------------


class TestEvaluateStream:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    @patch("agentevals.api.routes.load_traces")
    def test_stream_content_type(self, mock_load_traces, mock_eval):
        mock_load_traces.return_value = []
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_stream_invalid_config(self):
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": "not json"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert '"error"' in body
        assert "Invalid config JSON" in body

    def test_stream_wrong_extension(self):
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.txt", io.BytesIO(b"data"))},
            data={"config": _eval_config_json()},
        )
        body = resp.text
        assert '"error"' in body
        assert "Invalid file extension" in body

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    @patch("agentevals.api.routes.load_traces")
    def test_stream_done_event(self, mock_load_traces, mock_eval):
        mock_load_traces.return_value = []
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json()},
        )
        lines = resp.text.strip().split("\n")
        data_lines = [line for line in lines if line.startswith("data: ")]
        done_events = [json.loads(line[6:]) for line in data_lines if '"done"' in line]
        assert len(done_events) == 1
        done = done_events[0]
        assert done["done"] is True
        assert "result" in done
        assert "traceResults" in done["result"]

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.load_traces")
    def test_stream_resolves_credential_refs(self, mock_load_traces, mock_eval, monkeypatch):
        monkeypatch.setenv("AE_TEST_JUDGE_KEY", "sk-resolved-stream")
        mock_load_traces.return_value = [MagicMock()]
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={
                "config": json.dumps(_judge_config()),
                "credential_refs": json.dumps({"k": {"kind": "env", "name": "AE_TEST_JUDGE_KEY"}}),
            },
        )
        assert '"done"' in resp.text
        assert captured["judge_key"] == "sk-resolved-stream"

    def test_stream_bad_credential_refs(self):
        resp = self.client.post(
            "/api/evaluate/stream",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json(), "credential_refs": "{not json"},
        )
        assert resp.status_code == 200
        assert '"error"' in resp.text
        assert "credentialRefs" in resp.text

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.load_eval_set")
    @patch("agentevals.api.routes.load_traces")
    def test_stream_sanitizes_traversal_filenames(self, mock_load_traces, mock_load_eval_set, mock_eval):
        captured: dict = {"trace_files": []}

        def _record_trace(path, **_):
            captured["trace_files"].append(path)
            captured.setdefault("existed", {})[path] = os.path.exists(path)
            return [MagicMock()]

        def _record_eval_set(path):
            captured["eval_set_file"] = path
            return MagicMock()

        mock_load_traces.side_effect = _record_trace
        mock_load_eval_set.side_effect = _record_eval_set
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/stream",
            files={
                "trace_files": ("../../outside.json", io.BytesIO(_make_trace_json())),
                "eval_set_file": ("../../outside.json", io.BytesIO(_make_eval_set_json())),
            },
            data={"config": _eval_config_json()},
        )
        assert '"done"' in resp.text
        saved = captured["trace_files"][0]
        assert os.path.basename(saved) == "0_outside.json"
        assert ".." not in saved
        assert os.path.basename(captured["eval_set_file"]) == "evalset_outside.json"
        assert ".." not in captured["eval_set_file"]
        assert all(captured["existed"].values())


# ---------------------------------------------------------------------------
# POST /api/evaluate/json
# ---------------------------------------------------------------------------


def _make_otlp_json_payload() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "gcp.vertex.agent"},
                        "spans": [
                            {
                                "traceId": "abc123",
                                "spanId": "span1",
                                "name": "invoke_agent test",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class TestEvaluateJson:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_success(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {"evaluators": [{"name": "tool_trajectory_avg_score", "type": "builtin"}]},
            },
        )
        body = _assert_envelope(resp)
        assert "traceResults" in body["data"]
        assert len(body["data"]["traceResults"]) == 1

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_camel_case_config(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {
                    "evaluators": [
                        {
                            "name": "hallucinations_v1",
                            "type": "builtin",
                            "judgeModel": "gemini-2.0-flash",
                        }
                    ],
                    "maxConcurrentTraces": 5,
                },
            },
        )
        body = _assert_envelope(resp)
        assert "traceResults" in body["data"]
        call_config = mock_eval.call_args.kwargs["config"]
        assert call_config.evaluators[0].judge_model == "gemini-2.0-flash"
        assert call_config.max_concurrent_traces == 5

    def test_evaluate_json_missing_resource_spans(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={"traces": {"foo": "bar"}, "config": {}},
        )
        assert resp.status_code == 400
        assert "resourceSpans" in resp.json()["detail"]

    def test_evaluate_json_empty_traces(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={"traces": {"resourceSpans": []}, "config": {}},
        )
        assert resp.status_code == 400
        assert "No traces" in resp.json()["detail"]

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_with_eval_set(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {"evaluators": [{"name": "tool_trajectory_avg_score", "type": "builtin"}]},
                "evalSet": {
                    "eval_set_id": "test",
                    "eval_cases": [
                        {
                            "eval_id": "c1",
                            "conversation": [
                                {
                                    "invocation_id": "inv1",
                                    "user_content": {"role": "user", "parts": [{"text": "hi"}]},
                                    "final_response": {"role": "model", "parts": [{"text": "hello"}]},
                                }
                            ],
                        }
                    ],
                },
            },
        )
        _assert_envelope(resp)
        assert mock_eval.call_args.kwargs["eval_set"] is not None

    def test_evaluate_json_invalid_eval_set(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {},
                "evalSet": {"not_valid": True},
            },
        )
        assert resp.status_code == 400
        assert "eval set" in resp.json()["detail"].lower()

    def test_evaluate_json_invalid_concurrency(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {"maxConcurrentTraces": 0},
            },
        )
        assert resp.status_code == 422

    def test_evaluate_json_rejects_legacy_metrics_field(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {"metrics": ["tool_trajectory_avg_score"]},
            },
        )
        assert resp.status_code == 422

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_camel_keys_in_result(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": {"evaluators": [{"name": "tool_trajectory_avg_score", "type": "builtin"}]},
            },
        )
        body = resp.json()
        _assert_all_keys_camel(body)

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_default_config(self, mock_eval):
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={"traces": _make_otlp_json_payload()},
        )
        body = _assert_envelope(resp)
        assert "traceResults" in body["data"]

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_resolves_credential_refs(self, mock_eval, monkeypatch):
        monkeypatch.setenv("AE_TEST_JUDGE_KEY", "sk-resolved-json")
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": _judge_config(),
                "credentialRefs": {"k": {"kind": "env", "name": "AE_TEST_JUDGE_KEY"}},
            },
        )
        _assert_envelope(resp)
        assert captured["judge_key"] == "sk-resolved-json"

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_without_credential_refs_is_noop(self, mock_eval):
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate/json",
            json={"traces": _make_otlp_json_payload(), "config": _judge_config()},
        )
        _assert_envelope(resp)
        assert captured["judge_key"] is None

    def test_evaluate_json_credential_refs_wrong_shape_returns_422(self):
        resp = self.client.post(
            "/api/evaluate/json",
            json={"traces": _make_otlp_json_payload(), "credentialRefs": ["not", "a", "map"]},
        )
        assert resp.status_code == 422

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    def test_evaluate_json_unresolvable_credential_returns_400(self, mock_eval, monkeypatch):
        monkeypatch.delenv("AE_MISSING_KEY", raising=False)
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json",
            json={
                "traces": _make_otlp_json_payload(),
                "config": _judge_config(),
                "credentialRefs": {"k": {"kind": "env", "name": "AE_MISSING_KEY"}},
            },
        )
        assert resp.status_code == 400
        assert "Could not resolve credentialRefs" in resp.json()["detail"]
        mock_eval.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/evaluate/json/stream (SSE)
# ---------------------------------------------------------------------------


class TestEvaluateJsonStream:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.OtlpJsonLoader")
    def test_stream_content_type(self, mock_loader_cls, mock_eval):
        mock_loader_cls.return_value.load_from_dict.return_value = []
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={"traces": _make_otlp_json_payload(), "config": {}},
        )
        assert resp.headers["content-type"].startswith("text/event-stream")

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.OtlpJsonLoader")
    def test_stream_done_event(self, mock_loader_cls, mock_eval):
        mock_trace = MagicMock()
        mock_trace.trace_id = "abc123"
        mock_loader_cls.return_value.load_from_dict.return_value = [mock_trace]
        mock_eval.return_value = _make_run_result()

        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={"traces": _make_otlp_json_payload(), "config": {}},
        )
        lines = resp.text.strip().split("\n")
        data_lines = [line for line in lines if line.startswith("data: ")]
        done_events = [json.loads(line[6:]) for line in data_lines if '"done"' in line]
        assert len(done_events) == 1
        assert done_events[0]["done"] is True
        assert "traceResults" in done_events[0]["result"]

    def test_stream_error_on_invalid_traces(self):
        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={"traces": {"no_resource_spans": True}, "config": {}},
        )
        assert resp.status_code == 200
        body = resp.text
        assert '"error"' in body
        assert "resourceSpans" in body

    def test_stream_error_on_empty_traces(self):
        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={"traces": {"resourceSpans": []}, "config": {}},
        )
        body = resp.text
        assert '"error"' in body
        assert "No traces" in body

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.OtlpJsonLoader")
    def test_stream_resolves_credential_refs(self, mock_loader_cls, mock_eval, monkeypatch):
        monkeypatch.setenv("AE_TEST_JUDGE_KEY", "sk-resolved-json-stream")
        mock_trace = MagicMock()
        mock_trace.trace_id = "abc123"
        mock_loader_cls.return_value.load_from_dict.return_value = [mock_trace]
        captured: dict = {}
        mock_eval.side_effect = _capturing_run_eval(captured)
        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={
                "traces": _make_otlp_json_payload(),
                "config": _judge_config(),
                "credentialRefs": {"k": {"kind": "env", "name": "AE_TEST_JUDGE_KEY"}},
            },
        )
        assert '"done"' in resp.text
        assert captured["judge_key"] == "sk-resolved-json-stream"

    @patch("agentevals.api.routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.routes.OtlpJsonLoader")
    def test_stream_unresolvable_credential_yields_error(self, mock_loader_cls, mock_eval, monkeypatch):
        monkeypatch.delenv("AE_MISSING_KEY", raising=False)
        mock_trace = MagicMock()
        mock_trace.trace_id = "abc123"
        mock_loader_cls.return_value.load_from_dict.return_value = [mock_trace]
        mock_eval.return_value = _make_run_result()
        resp = self.client.post(
            "/api/evaluate/json/stream",
            json={
                "traces": _make_otlp_json_payload(),
                "config": _judge_config(),
                "credentialRefs": {"k": {"kind": "env", "name": "AE_MISSING_KEY"}},
            },
        )
        assert '"error"' in resp.text
        assert "Could not resolve credentialRefs" in resp.text
        assert '"done"' not in resp.text
        mock_eval.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/streaming/sessions
# ---------------------------------------------------------------------------


class TestStreamingSessions:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_list_sessions_empty(self):
        self.mgr.sessions.clear()
        client = TestClient(self.app)
        body = _assert_envelope(client.get("/api/streaming/sessions"))
        assert body["data"] == []

    def test_list_sessions_with_data(self):
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "t1", is_complete=False)
        self.mgr.sessions["s2"] = _make_session("s2", "t2", is_complete=True)
        client = TestClient(self.app)
        body = _assert_envelope(client.get("/api/streaming/sessions"))
        assert len(body["data"]) == 2
        _assert_all_keys_camel(body)
        ids = {s["sessionId"] for s in body["data"]}
        assert ids == {"s1", "s2"}

    def test_list_sessions_complete_includes_invocations(self):
        self.mgr.sessions.clear()
        invs = [{"invocation_id": "inv1", "user_content": "hello"}]
        self.mgr.sessions["s1"] = _make_session("s1", "t1", is_complete=True, invocations=invs)
        client = TestClient(self.app)
        body = _assert_envelope(client.get("/api/streaming/sessions"))
        assert body["data"][0]["invocations"] is not None


# ---------------------------------------------------------------------------
# POST /api/streaming/create-eval-set
# ---------------------------------------------------------------------------


class TestStreamingCreateEvalSet:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_create_eval_set_missing_session(self):
        self.mgr.sessions.clear()
        client = TestClient(self.app)
        resp = client.post(
            "/api/streaming/create-eval-set",
            json={
                "session_id": "nonexistent",
                "eval_set_id": "test",
            },
        )
        assert resp.status_code == 404

    def test_create_eval_set_success(self):
        chat_span = {
            "traceId": "a" * 32,
            "spanId": "b" * 16,
            "name": "chat gpt-4.1-mini",
            "startTimeUnixNano": "1000",
            "endTimeUnixNano": "2000",
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4.1-mini"}},
                {
                    "key": "gen_ai.input.messages",
                    "value": {
                        "stringValue": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "hi"}]}])
                    },
                },
                {
                    "key": "gen_ai.output.messages",
                    "value": {
                        "stringValue": json.dumps(
                            [{"role": "assistant", "parts": [{"type": "text", "content": "hey"}]}]
                        )
                    },
                },
            ],
        }
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "a" * 32, spans=[chat_span])

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/create-eval-set",
                json={
                    "session_id": "s1",
                    "eval_set_id": "test_eval",
                },
            )
        )
        assert body["data"]["numInvocations"] == 1
        conversation = body["data"]["evalSet"]["eval_cases"][0]["conversation"]
        assert conversation[0]["invocation_id"] == "b" * 16
        assert conversation[0]["user_content"]["parts"][0]["text"] == "hi"

    def test_create_eval_set_no_traces(self):
        """A span without a trace id cannot form a trace, so there is nothing to build from."""
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[{"spanId": "sp1"}])

        client = TestClient(self.app)
        resp = client.post(
            "/api/streaming/create-eval-set",
            json={
                "session_id": "s1",
                "eval_set_id": "test_eval",
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/streaming/evaluate-sessions
# ---------------------------------------------------------------------------


class TestStreamingEvaluateSessions:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_evaluate_sessions_missing_golden(self):
        self.mgr.sessions.clear()
        client = TestClient(self.app)
        resp = client.post(
            "/api/streaming/evaluate-sessions",
            json={
                "golden_session_id": "nonexistent",
                "eval_set_id": "e1",
            },
        )
        assert resp.status_code == 404

    @patch("agentevals.api.streaming_routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_evaluate_sessions_success(self, mock_create_eval, mock_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["other"] = _make_session("other", "to")

        mock_create_eval.return_value = StandardResponse(
            data=CreateEvalSetData(
                eval_set={"eval_set_id": "e1", "eval_cases": []},
                num_invocations=1,
            )
        )
        mock_eval.return_value = _make_run_result()

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/evaluate-sessions",
                json={
                    "golden_session_id": "golden",
                    "eval_set_id": "e1",
                },
            )
        )
        assert body["data"]["goldenSessionId"] == "golden"
        assert isinstance(body["data"]["results"], list)
        assert {r["sessionId"] for r in body["data"]["results"]} == {"golden", "other"}
        assert sorted(c.kwargs["group_key"] for c in mock_eval.await_args_list) == ["golden", "other"]
        _assert_all_keys_camel(body)

    @patch("agentevals.api.streaming_routes.run_evaluation_from_traces", new_callable=AsyncMock)
    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_evaluate_sessions_eval_failure(self, mock_create_eval, mock_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["other"] = _make_session("other", "to")

        mock_create_eval.return_value = StandardResponse(
            data=CreateEvalSetData(
                eval_set={"eval_set_id": "e1", "eval_cases": []},
                num_invocations=1,
            )
        )
        mock_eval.side_effect = RuntimeError("eval crashed")

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/evaluate-sessions",
                json={
                    "golden_session_id": "golden",
                    "eval_set_id": "e1",
                },
            )
        )
        results = body["data"]["results"]
        assert any(r.get("error") for r in results)


# ---------------------------------------------------------------------------
# POST /api/streaming/prepare-evaluation
# ---------------------------------------------------------------------------


class TestStreamingPrepareEvaluation:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_prepare_missing_golden(self):
        self.mgr.sessions.clear()
        client = TestClient(self.app)
        resp = client.post(
            "/api/streaming/prepare-evaluation",
            json={
                "golden_session_id": "nonexistent",
                "session_ids": [],
            },
        )
        assert resp.status_code == 404

    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_prepare_success(self, mock_create_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["s1"] = _make_session("s1", "t1")
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")

        mock_create_eval.return_value = StandardResponse(
            data=CreateEvalSetData(
                eval_set={"eval_set_id": "e1", "eval_cases": []},
                num_invocations=1,
            )
        )

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/prepare-evaluation",
                json={
                    "golden_session_id": "golden",
                    "session_ids": ["s1"],
                },
            )
        )
        assert "evalSetUrl" in body["data"]
        assert body["data"]["numTraces"] == 1
        _assert_all_keys_camel(body)

    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_prepare_skips_incomplete(self, mock_create_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["s1"] = _make_session("s1", "t1", is_complete=False)
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")

        mock_create_eval.return_value = StandardResponse(
            data=CreateEvalSetData(
                eval_set={"eval_set_id": "e1", "eval_cases": []},
                num_invocations=1,
            )
        )

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/prepare-evaluation",
                json={
                    "golden_session_id": "golden",
                    "session_ids": ["s1"],
                },
            )
        )
        assert body["data"]["numTraces"] == 0


# ---------------------------------------------------------------------------
# GET /api/streaming/download/{filename}
# ---------------------------------------------------------------------------


_OUT_OF_SCOPE_NAME = "out_of_scope_ref.txt"
_OUT_OF_SCOPE_TOKEN = "out-of-scope-marker-a1b2c3"


class TestStreamingDownload:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_happy_path_downloads_byte_for_byte(self):
        payload = b'{"marker":"in-scope","value":123}\n'
        target = EXPORT_DIR / "happy_download.json"
        target.write_bytes(payload)
        try:
            client = TestClient(self.app)
            resp = client.get("/api/streaming/download/happy_download.json")
            assert resp.status_code == 200
            assert resp.content == payload
        finally:
            target.unlink(missing_ok=True)

    def test_file_in_shared_temp_root_not_downloadable(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir(), encoding="utf-8"
        ) as f:
            f.write(_OUT_OF_SCOPE_TOKEN)
            fname = os.path.basename(f.name)
        try:
            client = TestClient(self.app)
            resp = client.get(f"/api/streaming/download/{fname}")
            assert resp.status_code == 404
            assert _OUT_OF_SCOPE_TOKEN not in resp.text
        finally:
            os.unlink(os.path.join(tempfile.gettempdir(), fname))

    def test_missing_bare_name_returns_404(self):
        client = TestClient(self.app)
        resp = client.get("/api/streaming/download/definitely_absent_file.json")
        assert resp.status_code == 404

    def test_non_bare_filenames_are_rejected_without_leaking(self):
        sentinel = os.path.join(tempfile.gettempdir(), _OUT_OF_SCOPE_NAME)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(_OUT_OF_SCOPE_TOKEN)

        abs_encoded = urllib.parse.quote(sentinel, safe="")
        payloads = [
            f"../{_OUT_OF_SCOPE_NAME}",
            f"..%2f..%2f{_OUT_OF_SCOPE_NAME}",
            abs_encoded,
            "..",
            ".",
        ]
        try:
            client = TestClient(self.app)
            for payload in payloads:
                resp = client.get(f"/api/streaming/download/{payload}")
                assert resp.status_code in (400, 404), payload
                assert _OUT_OF_SCOPE_TOKEN not in resp.text, payload
        finally:
            os.unlink(sentinel)

    def test_symlink_escape_is_blocked(self):
        sentinel = os.path.join(tempfile.gettempdir(), _OUT_OF_SCOPE_NAME)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(_OUT_OF_SCOPE_TOKEN)

        link = EXPORT_DIR / "linked.json"
        link.unlink(missing_ok=True)
        os.symlink(sentinel, link)
        try:
            client = TestClient(self.app)
            resp = client.get("/api/streaming/download/linked.json")
            assert resp.status_code == 404
            assert _OUT_OF_SCOPE_TOKEN not in resp.text
        finally:
            link.unlink(missing_ok=True)
            os.unlink(sentinel)

    def test_containment_is_checked_before_existence(self):
        # An out-of-scope path and a missing name must return byte-identical
        # responses, so containment is resolved before existence is checked.
        sentinel = os.path.join(tempfile.gettempdir(), _OUT_OF_SCOPE_NAME)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(_OUT_OF_SCOPE_TOKEN)

        link = EXPORT_DIR / "probe.json"
        link.unlink(missing_ok=True)
        os.symlink(sentinel, link)
        try:
            client = TestClient(self.app)
            out_of_scope = client.get("/api/streaming/download/probe.json")
            missing = client.get("/api/streaming/download/absent_probe.json")
            assert out_of_scope.status_code == missing.status_code == 404
            assert out_of_scope.json() == missing.json()
        finally:
            link.unlink(missing_ok=True)
            os.unlink(sentinel)

    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_prepared_files_download_byte_for_byte(self, mock_create_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[{"spanId": "sp1"}])

        mock_create_eval.return_value = StandardResponse(
            data=CreateEvalSetData(
                eval_set={"eval_set_id": "e1", "eval_cases": []},
                num_invocations=1,
            )
        )

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/prepare-evaluation",
                json={"golden_session_id": "golden", "session_ids": ["s1"]},
            )
        )

        for url in [body["data"]["evalSetUrl"], *body["data"]["traceUrls"]]:
            assert url.startswith("/api/streaming/download/")
            on_disk = (EXPORT_DIR / url.rsplit("/", 1)[-1]).read_bytes()
            resp = client.get(url)
            assert resp.status_code == 200
            assert resp.content == on_disk


# ---------------------------------------------------------------------------
# POST /api/streaming/get-trace
# ---------------------------------------------------------------------------


class TestStreamingGetTrace:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_get_trace_missing(self):
        self.mgr.sessions.clear()
        client = TestClient(self.app)
        resp = client.post("/api/streaming/get-trace", json={"session_id": "nope"})
        assert resp.status_code == 404

    def test_get_trace_success(self):
        self.mgr.sessions.clear()
        span = {
            "traceId": "t1",
            "spanId": "sp1",
            "operationName": "test",
            "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "2000000000",
            "attributes": [],
        }
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[span])

        client = TestClient(self.app)
        body = _assert_envelope(
            client.post(
                "/api/streaming/get-trace",
                json={"session_id": "s1"},
            )
        )
        assert body["data"]["sessionId"] == "s1"
        assert isinstance(body["data"]["traceContent"], str)
        assert body["data"]["numSpans"] >= 1

    def test_get_trace_camel_keys(self):
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[{"spanId": "sp1"}])

        body = self.client_get_trace("s1")
        _assert_all_keys_camel(body)

    def client_get_trace(self, session_id):
        client = TestClient(self.app)
        return client.post(
            "/api/streaming/get-trace",
            json={"session_id": session_id},
        ).json()


# ---------------------------------------------------------------------------
# POST /api/debug/bundle
# ---------------------------------------------------------------------------


class TestDebugBundle:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    def test_bundle_returns_zip(self):
        resp = self.client.post(
            "/api/debug/bundle",
            json={
                "user_description": "test bug",
                "browser_info": {},
                "console_logs": [],
                "app_state": {},
                "network_errors": [],
            },
        )
        assert resp.status_code == 200
        assert "application/zip" in resp.headers["content-type"]

    def test_bundle_zip_contents(self):
        resp = self.client.post(
            "/api/debug/bundle",
            json={
                "user_description": "test",
                "browser_info": {},
                "console_logs": [],
                "app_state": {},
                "network_errors": [],
            },
        )
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert any("metadata.json" in n for n in names)
        assert any("backend_logs.txt" in n for n in names)
        assert any("frontend_state.json" in n for n in names)


# ---------------------------------------------------------------------------
# POST /api/debug/load
# ---------------------------------------------------------------------------


class TestDebugLoad:
    def test_load_no_live_mode(self):
        client = TestClient(_make_app())
        resp = client.post(
            "/api/debug/load",
            files={"file": ("report.zip", io.BytesIO(b"fake"), "application/zip")},
        )
        assert resp.status_code == 503

    def test_load_invalid_zip(self):
        mgr = _make_trace_manager()
        app = _make_live_app(mgr)
        client = TestClient(app)
        resp = client.post(
            "/api/debug/load",
            files={"file": ("report.zip", io.BytesIO(b"not a zip"), "application/zip")},
        )
        assert resp.status_code == 400

    def test_load_no_sessions_in_zip(self):
        mgr = _make_trace_manager()
        app = _make_live_app(mgr)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("report/metadata.json", "{}")
        buf.seek(0)

        client = TestClient(app)
        resp = client.post(
            "/api/debug/load",
            files={"file": ("report.zip", buf, "application/zip")},
        )
        assert resp.status_code == 400

    def test_load_success(self):
        mgr = _make_trace_manager()
        mgr._extract_invocations = AsyncMock(return_value=[])
        mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")
        app = _make_live_app(mgr)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("report/sessions/sess1/spans.json", json.dumps([{"spanId": "sp1"}]))
            zf.writestr(
                "report/sessions/sess1/session_meta.json",
                json.dumps(
                    {
                        "session_id": "sess1",
                        "trace_id": "t1",
                    }
                ),
            )
        buf.seek(0)

        client = TestClient(app)
        body = _assert_envelope(
            client.post(
                "/api/debug/load",
                files={"file": ("report.zip", buf, "application/zip")},
            )
        )
        assert body["data"]["count"] == 1
        assert "sess1" in body["data"]["loadedSessions"]
        _assert_all_keys_camel(body)


# ---------------------------------------------------------------------------
# GET /stream/ui-updates (SSE)
# ---------------------------------------------------------------------------


class TestUIUpdatesSSE:
    """Regression test for the _Request alias bug.

    With ``from __future__ import annotations`` active, importing
    ``Request as _Request`` inside the ``enable_streaming`` block caused
    FastAPI to treat ``request`` as a required query parameter, returning
    422 on every SSE connection attempt.  The fix moves the import to
    module level without an alias.
    """

    def _make_streaming_app(self):
        import asyncio

        from agentevals.api.app import create_app
        from agentevals.streaming.ws_server import StreamingTraceManager

        mgr = StreamingTraceManager()
        # Replace register_sse_client so the queue immediately closes (None sentinel)
        # so the streaming response can be read synchronously in tests.
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(None)
        mgr.register_sse_client = MagicMock(return_value=q)
        mgr.unregister_sse_client = MagicMock()
        return create_app(enable_streaming=True, trace_manager=mgr)

    def test_sse_endpoint_returns_200_not_422(self):
        """GET /stream/ui-updates must return 200, not 422."""
        app = self._make_streaming_app()
        client = TestClient(app)
        resp = client.get("/stream/ui-updates")
        assert resp.status_code == 200, (
            f"Expected 200 but got {resp.status_code}. "
            "A 422 indicates the Request type annotation was not resolved correctly."
        )

    def test_sse_endpoint_content_type(self):
        """The response must use the text/event-stream media type."""
        app = self._make_streaming_app()
        client = TestClient(app)
        resp = client.get("/stream/ui-updates")
        assert resp.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# POST /api/convert — content-based format detection (issue #127)
# ---------------------------------------------------------------------------


class TestConvertAutoDetect:
    """The /convert endpoint must auto-detect Tempo/OTLP exports without a
    ``trace_format`` form field. Pre-fix, a Tempo ``.json`` upload was
    misclassified as Jaeger and rejected with a 400 'expected top-level
    data key' error.
    """

    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @staticmethod
    def _tempo_fixture_bytes() -> bytes:
        path = os.path.join(os.path.dirname(__file__), "..", "samples", "tempo_export_with_batches.json")
        with open(path, "rb") as f:
            return f.read()

    def test_tempo_json_upload_auto_detected(self):
        body = _assert_envelope(
            self.client.post(
                "/api/convert",
                files={"trace_files": ("trace.json", io.BytesIO(self._tempo_fixture_bytes()))},
            )
        )
        traces = body["data"]["traces"]
        assert len(traces) == 1
        assert traces[0]["traceId"] == "dd547580319ab0312cee07f1def50dad"
        assert len(traces[0]["invocations"]) == 1

    def test_convert_otlp_scope_schema_url_propagates_schema_version(self):
        payload = _make_otlp_json_payload()
        scope_spans = payload["resourceSpans"][0]["scopeSpans"][0]
        scope_spans["schemaUrl"] = "https://opentelemetry.io/schemas/1.39.0"
        scope_spans["spans"].append(
            {
                "traceId": scope_spans["spans"][0]["traceId"],
                "spanId": "s2",
                "parentSpanId": scope_spans["spans"][0]["spanId"],
                "name": "call_llm",
                "startTimeUnixNano": "1100000",
                "endTimeUnixNano": "1500000",
                "attributes": [
                    {
                        "key": "gcp.vertex.agent.llm_request",
                        "value": {
                            "stringValue": json.dumps({"contents": [{"role": "user", "parts": [{"text": "hello"}]}]})
                        },
                    },
                    {
                        "key": "gcp.vertex.agent.llm_response",
                        "value": {"stringValue": json.dumps({"content": {"role": "model", "parts": [{"text": "hi"}]}})},
                    },
                ],
            }
        )

        body = _assert_envelope(
            self.client.post(
                "/api/convert",
                files={"trace_files": ("trace.json", io.BytesIO(json.dumps(payload).encode()))},
            )
        )

        metadata = body["data"]["traces"][0]["metadata"]
        assert metadata["schemaVersion"] == "1.39.0"

    @patch("agentevals.api.routes.extract_trace_metadata")
    def test_convert_metadata_includes_schema_version(self, mock_extract_trace_metadata):
        mock_extract_trace_metadata.return_value = {
            "agent_name": "agent",
            "model": "gpt-4o",
            "schema_version": "1.39.0",
            "start_time": 123,
            "user_input_preview": "hello",
            "final_output_preview": "world",
        }

        body = _assert_envelope(
            self.client.post(
                "/api/convert",
                files={"trace_files": ("trace.json", io.BytesIO(self._tempo_fixture_bytes()))},
            )
        )

        metadata = body["data"]["traces"][0]["metadata"]
        assert metadata["schemaVersion"] == "1.39.0"

    def test_unknown_shape_returns_load_warning(self):
        unknown = json.dumps({"some_other_shape": []}).encode()
        resp = self.client.post(
            "/api/convert",
            files={"trace_files": ("trace.json", io.BytesIO(unknown))},
        )
        assert resp.status_code == 400
        assert "Could not detect trace format" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# SPA fallback path containment (static file serving)
# ---------------------------------------------------------------------------


class TestSpaFallbackContainment:
    def test_serves_index_at_root(self, tmp_path, monkeypatch):
        client = _make_ui_client(tmp_path, monkeypatch)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "agentevals-ui-index-sentinel" in resp.text

    def test_serves_real_asset(self, tmp_path, monkeypatch):
        client = _make_ui_client(tmp_path, monkeypatch)
        assert "ui-asset-sentinel" in client.get("/assets/app.js").text

    def test_unknown_route_falls_back_to_index(self, tmp_path, monkeypatch):
        client = _make_ui_client(tmp_path, monkeypatch)
        assert "agentevals-ui-index-sentinel" in client.get("/some/client/route").text

    def test_absolute_path_join_does_not_escape(self, tmp_path, monkeypatch):
        client = _make_ui_client(tmp_path, monkeypatch)
        outside = tmp_path.parent / "outside_root.txt"
        outside.write_text("outside-static-root")
        try:
            resp = client.get("http://testserver/" + str(outside))
            assert "outside-static-root" not in resp.text
            assert "agentevals-ui-index-sentinel" in resp.text
        finally:
            outside.unlink(missing_ok=True)

    def test_encoded_dotdot_does_not_escape(self, tmp_path, monkeypatch):
        client = _make_ui_client(tmp_path, monkeypatch)
        outside = tmp_path.parent / "outside_root.txt"
        outside.write_text("outside-static-root")
        try:
            resp = client.get("/%2e%2e/outside_root.txt")
            assert "outside-static-root" not in resp.text
            assert "agentevals-ui-index-sentinel" in resp.text
        finally:
            outside.unlink(missing_ok=True)

    def test_symlink_target_outside_root_not_followed(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / "outside_symlink_target.txt"
        outside.write_text("outside-static-root")
        client = _make_ui_client(tmp_path, monkeypatch)
        (tmp_path / "escape_link").symlink_to(outside)
        try:
            resp = client.get("/escape_link")
            assert "outside-static-root" not in resp.text
            assert "agentevals-ui-index-sentinel" in resp.text
        finally:
            outside.unlink(missing_ok=True)
