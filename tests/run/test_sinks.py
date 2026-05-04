"""Result sink tests.

Covers stdout / file sinks fully in-process and HttpWebhookSink against a
mock httpx transport so we exercise retry behavior without touching the network.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest

from agentevals.run.sinks import (
    FileSink,
    HttpWebhookSink,
    SinkFanout,
    StdoutSink,
    build_sinks,
)
from agentevals.storage.models import Result, ResultStatus


@contextlib.contextmanager
def _mock_async_client(transport: httpx.MockTransport):
    """Patch agentevals.run.sinks.httpx.AsyncClient so the sink's
    ``async with httpx.AsyncClient(...)`` call routes through the mock
    transport. Patching the symbol on the sinks module beats patching
    httpx globally, which can leak into other tests."""
    import agentevals.run.sinks as sinks_module

    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    with patch.object(sinks_module.httpx, "AsyncClient", _factory):
        yield


def _result(run_id: UUID) -> Result:
    return Result(
        result_id="rid-1",
        run_id=run_id,
        eval_set_item_id="item-1",
        eval_set_item_name="trace-1",
        evaluator_name="m1",
        evaluator_type="builtin",
        status=ResultStatus.PASSED,
        score=0.9,
    )


class TestFileSink:
    async def test_emits_partial_and_final(self, tmp_path):
        path = tmp_path / "out.jsonl"
        sink = FileSink(path)
        run_id = uuid4()
        await sink.emit_partial(run_id, [_result(run_id)], attempt=1)
        await sink.emit_final(run_id, {"trace_count": 1}, attempt=1)
        await sink.emit_error(run_id, "boom", attempt=1)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3
        partial = json.loads(lines[0])
        assert partial["phase"] == "partial"
        final = json.loads(lines[1])
        assert final["phase"] == "final"
        assert final["summary"] == {"trace_count": 1}
        error = json.loads(lines[2])
        assert error["phase"] == "error"

    async def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "out.jsonl"
        sink = FileSink(path)
        await sink.emit_final(uuid4(), {}, attempt=1)
        assert path.exists()


class TestStdoutSink:
    async def test_writes_to_stdout(self, capsys):
        sink = StdoutSink()
        run_id = uuid4()
        await sink.emit_partial(run_id, [_result(run_id)], attempt=1)
        await sink.emit_final(run_id, {"k": "v"}, attempt=1)
        captured = capsys.readouterr().out
        lines = captured.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["phase"] == "partial"
        assert json.loads(lines[1])["phase"] == "final"


class TestHttpWebhookSink:
    async def test_post_succeeds_on_2xx(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        sink = HttpWebhookSink("https://h/x")
        run_id = uuid4()
        with _mock_async_client(transport):
            await sink.emit_final(run_id, {"k": "v"}, attempt=1)
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["phase"] == "final"
        assert body["run_id"] == str(run_id)

    async def test_4xx_does_not_retry(self):
        """4xx means the receiver rejected the payload (auth, validation,
        etc); retrying would just hammer them. Errors are logged but the
        run still completes."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401, json={"error": "unauthorized"})

        transport = httpx.MockTransport(handler)
        sink = HttpWebhookSink("https://h/x", max_attempts=5)
        with _mock_async_client(transport):
            await sink.emit_final(uuid4(), {}, attempt=1)
        assert calls == 1

    async def test_5xx_retries_then_gives_up(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="busy")

        transport = httpx.MockTransport(handler)
        sink = HttpWebhookSink("https://h/x", max_attempts=3)
        with _mock_async_client(transport):
            await sink.emit_final(uuid4(), {}, attempt=1)
        assert calls == 3

    async def test_headers_from_env_resolved_at_emit_time(self, monkeypatch):
        """Reading env vars at emit time means a host can rotate the auth
        token between runs without restarting agentevals."""
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.headers))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        sink = HttpWebhookSink(
            "https://h/x",
            headers={"X-Static": "literal"},
            headers_from_env={"Authorization": "AGENTEVALS_TEST_BEARER"},
        )
        monkeypatch.setenv("AGENTEVALS_TEST_BEARER", "Bearer token-v1")
        with _mock_async_client(transport):
            await sink.emit_final(uuid4(), {}, attempt=1)
        assert captured[0].get("authorization") == "Bearer token-v1"
        assert captured[0].get("x-static") == "literal"

    async def test_headers_from_env_skipped_when_unset(self, monkeypatch):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.headers))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        sink = HttpWebhookSink(
            "https://h/x",
            headers_from_env={"Authorization": "AGENTEVALS_TEST_UNSET_VAR"},
        )
        monkeypatch.delenv("AGENTEVALS_TEST_UNSET_VAR", raising=False)
        with _mock_async_client(transport):
            await sink.emit_final(uuid4(), {}, attempt=1)
        assert "authorization" not in captured[0]


class TestBuildSinks:
    def test_stdout(self):
        fanout = build_sinks([{"kind": "stdout"}])
        assert isinstance(fanout, SinkFanout)

    def test_file(self, tmp_path):
        fanout = build_sinks([{"kind": "file", "path": str(tmp_path / "x.jsonl")}])
        assert isinstance(fanout, SinkFanout)

    def test_http_webhook_with_auth_env_extraction(self):
        fanout = build_sinks(
            [
                {
                    "kind": "http_webhook",
                    "url": "https://h/x",
                    "auth": {
                        "kind": "headers",
                        "headers": {"Authorization": {"from_env": "MY_TOKEN"}},
                    },
                }
            ]
        )
        assert isinstance(fanout, SinkFanout)

    def test_unknown_kind_skipped_not_raised(self):
        """Forward-compat: a host running a newer agentevals replica might
        emit a sink kind older replicas don't know. Skipping with a warning
        beats crashing the entire run."""
        fanout = build_sinks([{"kind": "future_kind"}, {"kind": "stdout"}])
        assert isinstance(fanout, SinkFanout)


class TestSinkFanoutErrorIsolation:
    """A sink that raises must not abort other sinks or the run itself."""

    async def test_failures_logged_not_raised(self, capsys):
        class BoomSink:
            async def emit_partial(self, run_id, results, attempt):
                raise RuntimeError("boom")

            async def emit_final(self, run_id, summary, attempt):
                raise RuntimeError("boom-final")

            async def emit_error(self, run_id, error, attempt):
                raise RuntimeError("boom-error")

        good_writes = []

        class GoodSink:
            async def emit_partial(self, run_id, results, attempt):
                good_writes.append("partial")

            async def emit_final(self, run_id, summary, attempt):
                good_writes.append("final")

            async def emit_error(self, run_id, error, attempt):
                good_writes.append("error")

        fanout = SinkFanout([BoomSink(), GoodSink()])
        run_id = uuid4()
        await fanout.emit_partial(run_id, [], attempt=1)
        await fanout.emit_final(run_id, {}, attempt=1)
        await fanout.emit_error(run_id, "x", attempt=1)
        assert good_writes == ["partial", "final", "error"]
