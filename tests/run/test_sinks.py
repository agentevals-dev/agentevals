"""Result sink tests.

Covers stdout / file sinks fully in-process and HttpWebhookSink against a
mock httpx transport so we exercise retry behavior without touching the network.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from agentevals.run.sinks import (
    FileSink,
    HttpWebhookSink,
    SinkFanout,
    StdoutSink,
    build_sinks,
    clear_sink_plugin_registry,
    log_registered_sinks,
    register_sink_factory,
    registered_sink_kinds,
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


@pytest.fixture
def isolated_sink_plugins():
    """``register_sink_factory`` is process-global; reset around plugin tests."""
    clear_sink_plugin_registry()
    yield
    clear_sink_plugin_registry()


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
        lines = path.read_text(encoding="utf-8").strip().splitlines()
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


class TestRegisteredSinkKinds:
    def test_includes_builtins(self):
        kinds = set(registered_sink_kinds())
        assert {"stdout", "file", "http_webhook"}.issubset(kinds)

    def test_log_registered_sinks_info(self, caplog):
        with caplog.at_level(logging.INFO, logger="agentevals.run.sinks"):
            log_registered_sinks()
        assert any("Result sinks available" in r.getMessage() for r in caplog.records)


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


class TestPluginSinkRegistry:
    async def test_register_sink_factory_kind(self, isolated_sink_plugins, tmp_path):
        path = tmp_path / "plugin.jsonl"

        def _factory(spec: dict) -> FileSink:
            return FileSink(spec["path"])

        register_sink_factory("plugin_file", _factory)
        fanout = build_sinks([{"kind": "plugin_file", "path": str(path)}])
        run_id = uuid4()
        await fanout.emit_final(run_id, {"ok": True}, attempt=1)
        assert json.loads(path.read_text(encoding="utf-8").strip())["summary"] == {"ok": True}

    async def test_programmatic_registration_overrides_builtin(self, isolated_sink_plugins):
        finals: list[dict] = []

        class CaptureSink:
            async def emit_partial(self, run_id, results, attempt):
                pass

            async def emit_final(self, run_id, summary, attempt):
                finals.append(summary)

            async def emit_error(self, run_id, error, attempt):
                pass

        register_sink_factory("stdout", lambda _spec: CaptureSink())
        fanout = build_sinks([{"kind": "stdout"}])
        await fanout.emit_final(uuid4(), {"captured": True}, attempt=1)
        assert finals == [{"captured": True}]

    async def test_factory_exception_skips_sink(self, isolated_sink_plugins, caplog):
        def broken_factory(_spec):
            raise RuntimeError("no")

        register_sink_factory("broken", broken_factory)
        with caplog.at_level(logging.ERROR):
            fanout = build_sinks([{"kind": "broken"}, {"kind": "stdout"}])
        assert isinstance(fanout, SinkFanout)
        assert any("sink factory failed for kind=broken" in r.getMessage() for r in caplog.records)
        await fanout.emit_final(uuid4(), {}, attempt=1)


class TestSinkEntryPoints:
    """Entry-point discovery without relying on packages installed in the test venv."""

    async def test_entry_point_factory_resolves_kind(self, isolated_sink_plugins, tmp_path):
        path = tmp_path / "ep.jsonl"

        def file_factory(spec: dict) -> FileSink:
            return FileSink(spec["path"])

        ep = MagicMock()
        ep.name = "from_ep"
        ep.load.return_value = file_factory

        with patch("agentevals.run.sinks.entry_points", return_value=[ep]):
            fanout = build_sinks([{"kind": "from_ep", "path": str(path)}])
        ep.load.assert_called_once_with()
        run_id = uuid4()
        await fanout.emit_final(run_id, {"via": "ep"}, attempt=1)
        assert json.loads(path.read_text(encoding="utf-8").strip())["summary"] == {"via": "ep"}

    async def test_builtin_kind_does_not_load_colliding_entry_point(self, isolated_sink_plugins, capsys):
        ep = MagicMock()
        ep.name = "stdout"
        ep.load.side_effect = AssertionError("built-in kinds must not load shadow entry points")

        with patch("agentevals.run.sinks.entry_points", return_value=[ep]):
            fanout = build_sinks([{"kind": "stdout"}])
        ep.load.assert_not_called()
        await fanout.emit_final(uuid4(), {"k": "v"}, attempt=1)
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["phase"] == "final"

    def test_entry_point_load_failure_skipped(self, isolated_sink_plugins, caplog):
        ep = MagicMock()
        ep.name = "broken_pkg_sink"
        ep.load.side_effect = ImportError("dist not installed")

        with patch("agentevals.run.sinks.entry_points", return_value=[ep]), caplog.at_level(logging.ERROR):
            build_sinks([{"kind": "broken_pkg_sink"}])
        assert any("failed to load sink entry point" in r.getMessage() for r in caplog.records)

    def test_non_callable_entry_point_skipped(self, isolated_sink_plugins, caplog):
        ep = MagicMock()
        ep.name = "bad_export"
        ep.load.return_value = "not_callable"

        with patch("agentevals.run.sinks.entry_points", return_value=[ep]), caplog.at_level(logging.WARNING):
            build_sinks([{"kind": "bad_export"}])
        assert any("not callable" in r.getMessage() for r in caplog.records)

    def test_kind_only_from_missing_plugin_is_unknown(self, isolated_sink_plugins, caplog):
        """Same behavior as an unpublished PyPI sink: no entry point, no registration."""
        with patch("agentevals.run.sinks.entry_points", return_value=[]), caplog.at_level(logging.WARNING):
            fanout = build_sinks([{"kind": "demo_ndjson", "path": "/tmp/would_not_be_used.jsonl"}])
        assert any("unknown sink kind 'demo_ndjson'" in r.getMessage() for r in caplog.records)
        assert isinstance(fanout, SinkFanout)

    async def test_missing_plugin_sink_does_not_break_fanout(self, isolated_sink_plugins, tmp_path, caplog):
        """Unknown plugin kind skipped; remaining sinks still run."""
        path = tmp_path / "only_builtin.jsonl"
        with patch("agentevals.run.sinks.entry_points", return_value=[]), caplog.at_level(logging.WARNING):
            fanout = build_sinks(
                [
                    {"kind": "demo_ndjson", "path": str(tmp_path / "absent.jsonl")},
                    {"kind": "file", "path": str(path)},
                ]
            )
        assert any("unknown sink kind 'demo_ndjson'" in r.getMessage() for r in caplog.records)
        run_id = uuid4()
        await fanout.emit_final(run_id, {"ok": True}, attempt=1)
        assert json.loads(path.read_text(encoding="utf-8").strip())["summary"] == {"ok": True}


def _demo_example_sink_installed() -> bool:
    try:
        import agentevals_example_custom_sink.sink  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _demo_example_sink_installed(), reason="install editable: uv pip install -e examples/custom_sink"
)
class TestDemoNdjsonExampleSink:
    async def test_path_dot_writes_default_ndjson(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from agentevals_example_custom_sink.sink import create_demo_sink

        sink = create_demo_sink({"path": "."})
        await sink.emit_final(uuid4(), {"x": 1}, attempt=1)
        out = tmp_path / "agentevals-demo-sink.ndjson"
        assert json.loads(out.read_text(encoding="utf-8").strip())["summary"] == {"x": 1}

    async def test_existing_directory_appends_default_filename(self, tmp_path):
        d = tmp_path / "outdir"
        d.mkdir()
        from agentevals_example_custom_sink.sink import create_demo_sink

        sink = create_demo_sink({"path": str(d)})
        await sink.emit_final(uuid4(), {}, attempt=1)
        assert (d / "agentevals-demo-sink.ndjson").exists()

    async def test_directory_with_custom_filename(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        from agentevals_example_custom_sink.sink import create_demo_sink

        sink = create_demo_sink({"path": str(d), "filename": "runs.jsonl"})
        await sink.emit_final(uuid4(), {"n": 2}, attempt=1)
        assert json.loads((d / "runs.jsonl").read_text(encoding="utf-8").strip())["summary"] == {"n": 2}


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


class TestWebhookLogs:
    """Webhook URLs and response bodies often carry tokens; logs keep the origin and the status."""

    MARK = "qxmark"

    async def _emit(self, url: str, handler) -> None:
        with _mock_async_client(httpx.MockTransport(handler)):
            await HttpWebhookSink(url, max_attempts=1).emit_final(uuid4(), {}, attempt=1)

    async def test_rejection(self, caplog):
        caplog.set_level(logging.WARNING, logger="agentevals.run.sinks")
        await self._emit(
            f"https://hooks.example.com/services/{self.MARK}?sig={self.MARK}",
            lambda request: httpx.Response(401, text=f"bad token {self.MARK}"),
        )
        assert "https://hooks.example.com returned 401" in caplog.text
        assert self.MARK not in caplog.text

    async def test_server_error(self, caplog):
        caplog.set_level(logging.WARNING, logger="agentevals.run.sinks")
        await self._emit(
            f"https://user:{self.MARK}@hooks.example.com/x",
            lambda request: httpx.Response(503, text=self.MARK),
        )
        assert "https://hooks.example.com failed after 1 attempts: HTTP 503" in caplog.text
        assert self.MARK not in caplog.text

    async def test_transport_error(self, caplog):
        caplog.set_level(logging.WARNING, logger="agentevals.run.sinks")

        def refuse(request):
            raise httpx.ConnectError(f"connect to {self.MARK} refused")

        await self._emit(f"https://hooks.example.com/{self.MARK}", refuse)
        assert "failed after 1 attempts: ConnectError" in caplog.text
        assert self.MARK not in caplog.text
