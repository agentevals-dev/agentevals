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
    cfg = {"metrics": ["tool_trajectory_avg_score"]}
    cfg.update(overrides)
    return json.dumps(cfg)


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

    def test_evaluate_empty_metrics(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": json.dumps({"metrics": ""})},
        )
        assert resp.status_code == 400

    def test_evaluate_threshold_out_of_range(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("trace.json", io.BytesIO(_make_trace_json()))},
            data={"config": _eval_config_json(threshold=1.5)},
        )
        assert resp.status_code == 400

    def test_evaluate_no_files(self):
        resp = self.client.post(
            "/api/evaluate",
            files={"trace_files": ("", io.BytesIO(b""))},
            data={"config": _eval_config_json()},
        )
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# POST /api/evaluate/stream (SSE)
# ---------------------------------------------------------------------------


class TestEvaluateStream:
    @classmethod
    def setup_class(cls):
        cls.client = TestClient(_make_app())

    @patch("agentevals.api.routes.run_evaluation", new_callable=AsyncMock)
    @patch("agentevals.api.routes.get_loader")
    def test_stream_content_type(self, mock_loader, mock_eval):
        mock_loader.return_value.load.return_value = []
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
    @patch("agentevals.api.routes.get_loader")
    def test_stream_done_event(self, mock_loader, mock_eval):
        mock_loader.return_value.load.return_value = []
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

    @patch("agentevals.api.streaming_routes.convert_traces")
    @patch("agentevals.api.streaming_routes.OtlpJsonLoader")
    def test_create_eval_set_success(self, mock_loader_cls, mock_convert):
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[{"spanId": "sp1"}])
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")

        mock_inv = MagicMock()
        mock_inv.invocation_id = "inv1"
        mock_inv.user_content = MagicMock()
        mock_inv.user_content.model_dump.return_value = {"role": "user", "parts": [{"text": "hi"}]}
        mock_inv.final_response = MagicMock()
        mock_inv.final_response.model_dump.return_value = {"role": "model", "parts": [{"text": "hey"}]}
        mock_inv.intermediate_data = None

        mock_trace = MagicMock()
        mock_trace.trace_id = "t1"
        mock_loader_cls.return_value.load.return_value = [mock_trace]
        mock_conv = MagicMock()
        mock_conv.invocations = [mock_inv]
        mock_convert.return_value = [mock_conv]

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
        assert "evalSet" in body["data"]
        assert body["data"]["numInvocations"] == 1

    @patch("agentevals.api.streaming_routes.OtlpJsonLoader")
    def test_create_eval_set_no_traces(self, mock_loader_cls):
        self.mgr.sessions.clear()
        self.mgr.sessions["s1"] = _make_session("s1", "t1", spans=[{"spanId": "sp1"}])
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")
        mock_loader_cls.return_value.load.return_value = []

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

    @patch("agentevals.api.streaming_routes.run_evaluation", new_callable=AsyncMock)
    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_evaluate_sessions_success(self, mock_create_eval, mock_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["other"] = _make_session("other", "to")
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")

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
        assert len(body["data"]["results"]) >= 1
        _assert_all_keys_camel(body)

    @patch("agentevals.api.streaming_routes.run_evaluation", new_callable=AsyncMock)
    @patch("agentevals.api.streaming_routes._do_create_eval_set", new_callable=AsyncMock)
    def test_evaluate_sessions_eval_failure(self, mock_create_eval, mock_eval):
        self.mgr.sessions.clear()
        self.mgr.sessions["golden"] = _make_session("golden", "tg")
        self.mgr.sessions["other"] = _make_session("other", "to")
        self.mgr._save_spans_to_temp_file = AsyncMock(return_value="/tmp/test.jsonl")

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


class TestStreamingDownload:
    @classmethod
    def setup_class(cls):
        cls.mgr = _make_trace_manager()
        cls.app = _make_live_app(cls.mgr)

    def test_download_missing(self):
        client = TestClient(self.app)
        resp = client.get("/api/streaming/download/nonexistent_file_abc123.json")
        assert resp.status_code == 404

    def test_download_success(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir()) as f:
            f.write('{"test": true}')
            fname = os.path.basename(f.name)

        try:
            client = TestClient(self.app)
            resp = client.get(f"/api/streaming/download/{fname}")
            assert resp.status_code == 200
            assert resp.json() == {"test": True}
        finally:
            os.unlink(os.path.join(tempfile.gettempdir(), fname))

    def test_download_path_traversal(self):
        client = TestClient(self.app)
        resp = client.get("/api/streaming/download/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)


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
