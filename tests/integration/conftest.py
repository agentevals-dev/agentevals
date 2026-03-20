"""Shared fixtures for integration tests.

Provides two transport tiers:
- ASGI in-process (trace_manager + otlp_client + api_client): fast, no ports
- Real uvicorn servers (live_servers): for E2E subprocess agent tests
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import httpx
import pytest
import uvicorn

from agentevals.streaming.ws_server import StreamingTraceManager

# ---------------------------------------------------------------------------
# Tier 1: ASGI in-process fixtures (session grouping + timing stress tests)
# ---------------------------------------------------------------------------


@pytest.fixture
async def trace_manager():
    """Fresh StreamingTraceManager with fast timers for integration tests."""
    mgr = StreamingTraceManager(
        completion_grace_seconds=0.1,
        idle_timeout_seconds=0.5,
        reextraction_delay_seconds=0.1,
    )
    mgr.start_cleanup_task()
    yield mgr
    await mgr.shutdown()


@pytest.fixture
async def otlp_client(trace_manager):
    """httpx client → OTLP app via ASGI transport (no real server)."""
    from agentevals.api.otlp_routes import otlp_router, set_trace_manager

    set_trace_manager(trace_manager)

    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(otlp_router)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def api_client(trace_manager):
    """httpx client → main app streaming routes via ASGI transport."""
    from agentevals.api.streaming_routes import set_trace_manager, streaming_router

    set_trace_manager(trace_manager)

    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(streaming_router, prefix="/api/streaming")

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Tier 2: Real uvicorn servers (E2E agent tests)
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_servers():
    """Start real uvicorn on ephemeral ports in a background thread.

    Returns (main_port, otlp_port, trace_manager).

    Servers run in their own event loop on a daemon thread so they can
    process HTTP requests independently of the test's event loop.
    """
    import threading
    import time

    main_port = _find_free_port()
    otlp_port = _find_free_port()

    saved_env = {
        "AGENTEVALS_LIVE": os.environ.get("AGENTEVALS_LIVE"),
        "AGENTEVALS_HEADLESS": os.environ.get("AGENTEVALS_HEADLESS"),
    }
    os.environ["AGENTEVALS_LIVE"] = "1"
    os.environ["AGENTEVALS_HEADLESS"] = "1"

    import importlib

    from agentevals.api import app as app_module

    importlib.reload(app_module)

    from agentevals.api.app import app, get_trace_manager
    from agentevals.api.otlp_app import otlp_app
    from agentevals.api.otlp_routes import set_trace_manager

    mgr = get_trace_manager()
    set_trace_manager(mgr)

    main_config = uvicorn.Config(app, host="127.0.0.1", port=main_port, log_level="warning")
    otlp_config = uvicorn.Config(otlp_app, host="127.0.0.1", port=otlp_port, log_level="warning")
    main_server = uvicorn.Server(main_config)
    otlp_server = uvicorn.Server(otlp_config)

    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.gather(main_server.serve(), otlp_server.serve()))
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    import httpx as _httpx

    for port in (main_port, otlp_port):
        reachable = False
        for _ in range(50):
            try:
                _httpx.get(f"http://127.0.0.1:{port}/", timeout=0.5)
                reachable = True
                break
            except (_httpx.ConnectError, _httpx.ReadError):
                time.sleep(0.1)
        if not reachable:
            raise RuntimeError(f"Server on port {port} did not become reachable")

    yield main_port, otlp_port, mgr

    main_server.should_exit = True
    otlp_server.should_exit = True
    thread.join(timeout=5)

    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# OTLP payload builders
# ---------------------------------------------------------------------------


def make_trace_request(
    trace_id: str,
    session_name: str | None = None,
    eval_set_id: str | None = None,
    spans: list[dict] | None = None,
    scope_name: str = "test",
) -> dict:
    """Build an ExportTraceServiceRequest JSON body."""
    resource_attrs = []
    if session_name:
        resource_attrs.append({"key": "agentevals.session_name", "value": {"stringValue": session_name}})
    if eval_set_id:
        resource_attrs.append({"key": "agentevals.eval_set_id", "value": {"stringValue": eval_set_id}})
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "scope": {"name": scope_name, "version": "0.1"},
                        "spans": spans or [],
                    }
                ],
            }
        ]
    }


def make_genai_span(
    trace_id: str = "abc123",
    span_id: str | None = None,
    parent_span_id: str | None = "parent01",
    name: str = "chat gpt-4o-mini",
    extra_attrs: list[dict] | None = None,
) -> dict:
    """Build a GenAI semconv span dict."""
    span = {
        "traceId": trace_id,
        "spanId": span_id or uuid.uuid4().hex[:16],
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}},
            *(extra_attrs or []),
        ],
        "status": {"code": 0},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def make_log_request(
    trace_id: str,
    session_name: str | None = None,
    log_records: list[dict] | None = None,
) -> dict:
    """Build an ExportLogsServiceRequest JSON body."""
    resource_attrs = []
    if session_name:
        resource_attrs.append({"key": "agentevals.session_name", "value": {"stringValue": session_name}})
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeLogs": [{"logRecords": log_records or []}],
            }
        ]
    }


def make_genai_log(
    event_name: str,
    content: str,
    trace_id: str = "abc123",
    span_id: str = "",
    role: str = "user",
) -> dict:
    """Build an OTLP log record for a gen_ai event."""
    record: dict = {
        "eventName": event_name,
        "observedTimeUnixNano": "1500000000",
        "traceId": trace_id,
        "body": {
            "kvlistValue": {
                "values": [
                    {"key": "role", "value": {"stringValue": role}},
                    {"key": "content", "value": {"stringValue": content}},
                ]
            }
        },
        "attributes": [],
    }
    if span_id:
        record["spanId"] = span_id
    return record


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def send_traces(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    """POST /v1/traces and assert success."""
    resp = await client.post("/v1/traces", json=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200, f"POST /v1/traces failed: {resp.status_code} {resp.text}"
    return resp


async def send_logs(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    """POST /v1/logs and assert success."""
    resp = await client.post("/v1/logs", json=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200, f"POST /v1/logs failed: {resp.status_code} {resp.text}"
    return resp


async def wait_for_session_complete(
    mgr: StreamingTraceManager,
    session_id: str,
    timeout: float = 5.0,
) -> None:
    """Poll until session is complete or raise TimeoutError."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = mgr.sessions.get(session_id)
        if session and session.is_complete:
            return
        await asyncio.sleep(0.05)
    existing = list(mgr.sessions.keys())
    raise TimeoutError(f"Session '{session_id}' not complete after {timeout}s. Existing sessions: {existing}")


async def wait_for_n_sessions(
    mgr: StreamingTraceManager,
    n: int,
    timeout: float = 5.0,
) -> None:
    """Wait until at least n sessions exist."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if len(mgr.sessions) >= n:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Expected {n} sessions, got {len(mgr.sessions)} after {timeout}s")


def wait_for_session_complete_sync(
    mgr: StreamingTraceManager,
    session_id: str,
    timeout: float = 5.0,
) -> None:
    """Synchronous poll for E2E tests (servers run in background thread)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = mgr.sessions.get(session_id)
        if session and session.is_complete:
            return
        time.sleep(0.2)
    existing = list(mgr.sessions.keys())
    raise TimeoutError(f"Session '{session_id}' not complete after {timeout}s. Existing sessions: {existing}")


async def get_sessions(api_client: httpx.AsyncClient) -> list[dict]:
    """GET /api/streaming/sessions and return the data list."""
    resp = await api_client.get("/api/streaming/sessions")
    assert resp.status_code == 200
    return resp.json()["data"]
