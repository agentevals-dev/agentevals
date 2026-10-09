"""Receiver overload answers, grouped conversion, and debug bundles."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentevals.api import debug_routes
from agentevals.api.debug_routes import debug_router
from agentevals.api.otlp_app import create_otlp_app
from agentevals.api.otlp_grpc import OtlpTraceService
from agentevals.api.routes import router
from agentevals.otel.store import Limits
from agentevals.streaming.manager import LiveManager
from otel.builders import assistant, chat, named, request, user


def _json(document: dict) -> bytes:
    return json.dumps(document).encode()


class TestOverload:
    async def test_http_answers_503_with_retry_after_when_full(self):
        mgr = LiveManager(Limits(max_sessions=1))
        app = create_otlp_app(trace_manager=mgr)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://otlp") as client:
            first = await client.post(
                "/v1/traces",
                content=_json(request([chat(trace="a")], named("a"))),
                headers={"Content-Type": "application/json"},
            )
            full = await client.post(
                "/v1/traces",
                content=_json(request([chat(trace="b")], named("b"))),
                headers={"Content-Type": "application/json"},
            )
        assert first.status_code == 200
        assert full.status_code == 503
        assert full.headers["retry-after"]

    async def test_permanent_limits_stay_partial_success(self):
        mgr = LiveManager(Limits(spans_per_trace=1))
        app = create_otlp_app(trace_manager=mgr)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://otlp") as client:
            await client.post(
                "/v1/traces",
                content=_json(request([chat(span_id="c1")], named("a"))),
                headers={"Content-Type": "application/json"},
            )
            resp = await client.post(
                "/v1/traces",
                content=_json(request([chat(span_id="c2")], named("a"))),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        assert int(resp.json()["partialSuccess"]["rejectedSpans"]) == 1

    async def test_grpc_aborts_unavailable_when_full(self):
        import grpc
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
        from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

        def export(name: str, trace_byte: int) -> ExportTraceServiceRequest:
            span = Span(
                trace_id=bytes([trace_byte]) * 16,
                span_id=b"\x01" * 8,
                name="chat",
                start_time_unix_nano=1,
                end_time_unix_nano=2,
            )
            span.attributes.append(KeyValue(key="gen_ai.operation.name", value=AnyValue(string_value="chat")))
            rs = ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])
            rs.resource.attributes.append(KeyValue(key="agentevals.session_name", value=AnyValue(string_value=name)))
            return ExportTraceServiceRequest(resource_spans=[rs])

        class Context:
            code = None

            async def abort(self, code, message):
                self.code = code
                raise RuntimeError(message)

        service = OtlpTraceService(LiveManager(Limits(max_sessions=1)))
        await service.Export(export("a", 1), Context())
        context = Context()
        with pytest.raises(RuntimeError, match="capacity"):
            await service.Export(export("b", 2), context)
        assert context.code == grpc.StatusCode.UNAVAILABLE


def _round_trip_file() -> bytes:
    ask = chat(
        trace="a", span_id="c1", inputs=[user("Roll")], outputs=[assistant(tool_calls=[("call_1", "roll_die", {})])]
    )
    answer = chat(
        trace="b",
        span_id="c2",
        start=3_000,
        end=4_000,
        inputs=[
            user("Roll"),
            assistant(tool_calls=[("call_1", "roll_die", {})]),
            {"role": "tool", "parts": [{"type": "tool_call_response", "id": "call_1", "response": 4}]},
        ],
        outputs=[assistant("4")],
    )
    return _json(request([ask, answer], named("run")))


class TestConvertGrouping:
    def _convert(self, data: dict) -> dict:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        files = [("trace_files", ("session.json", _round_trip_file(), "application/json"))]
        return TestClient(app).post("/api/convert", files=files, data=data)

    def test_default_is_one_entry_per_trace(self):
        entries = self._convert({}).json()["data"]["traces"]
        assert len(entries) == 2
        assert all(e["traceIds"] == [e["traceId"]] for e in entries)

    def test_auto_groups_a_named_session_into_one_entry(self):
        entries = self._convert({"group_by": "auto"}).json()["data"]["traces"]
        assert len(entries) == 1
        assert len(entries[0]["traceIds"]) == 2
        assert len(entries[0]["invocations"]) == 1

    def test_unknown_group_by_is_rejected(self):
        assert self._convert({"group_by": "everything"}).status_code == 400


def _live_app(mgr: LiveManager) -> TestClient:
    app = FastAPI()
    app.state.trace_manager = mgr
    app.include_router(debug_router, prefix="/api/debug")
    return TestClient(app)


async def _manager_with(session_name: str) -> LiveManager:
    from agentevals.otel.decode import decode_json_document

    mgr = LiveManager()
    await mgr.ingest_spans(decode_json_document(request([chat()], named(session_name)), strict=True))
    return mgr


class TestDebugBundle:
    async def test_session_directories_are_numbered_not_named(self):
        mgr = await _manager_with("../../outside/name")
        resp = _live_app(mgr).post("/api/debug/bundle", json={})
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        session_files = [n for n in names if "/sessions/" in n]
        assert session_files and all(n.split("/sessions/")[1].startswith("0001/") for n in session_files)
        assert not any(".." in n for n in names)

    async def test_bundle_loads_back(self):
        source = await _manager_with("captured")
        bundle = _live_app(source).post("/api/debug/bundle", json={}).content
        target = LiveManager()
        resp = _live_app(target).post("/api/debug/load", files={"file": ("b.zip", bundle, "application/zip")})
        assert resp.json()["data"]["loadedSessions"] == ["captured"]
        assert target.sessions["captured"].span_count == 1

    def test_legacy_bundle_joins_logs_by_span_id(self):
        span_dict = chat(span_id="c1", content=False)
        logs = [
            {
                "event_name": "gen_ai.user.message",
                "span_id": span_dict["spanId"],
                "body": {"content": "legacy hi"},
                "timestamp": "1",
            }
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("r/sessions/old/spans.json", json.dumps([span_dict]))
            zf.writestr("r/sessions/old/logs.json", json.dumps(logs))
            zf.writestr("r/sessions/old/session_meta.json", json.dumps({"session_id": "old"}))
        mgr = LiveManager()
        resp = _live_app(mgr).post("/api/debug/load", files={"file": ("b.zip", buf.getvalue(), "application/zip")})
        assert resp.status_code == 200
        assert mgr.sessions["old"].log_count == 1

    @pytest.mark.parametrize(
        "cap,value", [("MAX_ENTRY_BYTES", 10), ("MAX_TOTAL_ENTRY_BYTES", 10), ("MAX_BUNDLE_BYTES", 10)]
    )
    def test_bundle_caps(self, monkeypatch, cap, value):
        monkeypatch.setattr(debug_routes, cap, value)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("r/sessions/s/otlp.json", json.dumps(request([chat()])))
        resp = _live_app(LiveManager()).post(
            "/api/debug/load", files={"file": ("b.zip", buf.getvalue(), "application/zip")}
        )
        assert resp.status_code == 413

    def test_session_count_cap(self, monkeypatch):
        monkeypatch.setattr(debug_routes, "MAX_BUNDLE_SESSIONS", 1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for n in range(2):
                zf.writestr(f"r/sessions/{n}/otlp.json", json.dumps(request([chat(trace=f"t{n}")])))
        resp = _live_app(LiveManager()).post(
            "/api/debug/load", files={"file": ("b.zip", buf.getvalue(), "application/zip")}
        )
        assert resp.status_code == 413

    def test_unreadable_entry_is_a_client_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("r/sessions/s/otlp.json", "{not json")
        resp = _live_app(LiveManager()).post(
            "/api/debug/load", files={"file": ("b.zip", buf.getvalue(), "application/zip")}
        )
        assert resp.status_code == 400


class TestMemoryBudgetSetting:
    def test_env_sets_the_budget(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_LIVE_MAX_BYTES", "4096")
        assert LiveManager().store.limits.max_bytes == 4096

    @pytest.mark.parametrize("value", ["lots", "-1", "0"])
    def test_invalid_values_are_errors(self, monkeypatch, value):
        monkeypatch.setenv("AGENTEVALS_LIVE_MAX_BYTES", value)
        with pytest.raises(ValueError, match="AGENTEVALS_LIVE_MAX_BYTES"):
            LiveManager()
