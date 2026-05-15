"""CLI startup wiring tests."""

from __future__ import annotations

import sys
import textwrap
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from agentevals import cli
from agentevals.runner import RunResult


class _FakeGrpcServer:
    def __init__(self):
        self.started = False

    async def start(self) -> None:
        self.started = True


class _FakeUvicornConfig:
    def __init__(self, app, **kwargs):
        self.app = app
        self.kwargs = kwargs


class _FakeUvicornServer:
    def __init__(self, config):
        self.config = config
        self.should_exit = False
        self.force_exit = False
        self.handle_exit = None

    async def serve(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_servers_shares_one_trace_manager_across_live_servers(monkeypatch):
    fake_grpc_server = _FakeGrpcServer()
    fake_stop_grpc = AsyncMock()
    created_servers: list[_FakeUvicornServer] = []
    captured: dict[str, object] = {}

    def fake_create_otlp_grpc_server(*, host, port, manager):
        captured["host"] = host
        captured["port"] = port
        captured["manager"] = manager
        return fake_grpc_server

    def fake_server_factory(config):
        server = _FakeUvicornServer(config)
        created_servers.append(server)
        return server

    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.Config = _FakeUvicornConfig
    fake_uvicorn.Server = fake_server_factory

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "create_otlp_grpc_server", fake_create_otlp_grpc_server)
    monkeypatch.setattr(cli, "stop_otlp_grpc_server", fake_stop_grpc)

    await cli._run_servers("127.0.0.1", 8001, 4318, 4317)

    assert len(created_servers) == 2
    main_app = created_servers[0].config.app
    otlp_app = created_servers[1].config.app
    manager = captured["manager"]

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 4317
    assert main_app.state.trace_manager is manager
    assert otlp_app.state.trace_manager is manager
    assert "reload" not in created_servers[0].config.kwargs
    assert "reload_dirs" not in created_servers[0].config.kwargs
    assert "reload" not in created_servers[1].config.kwargs
    assert "reload_dirs" not in created_servers[1].config.kwargs
    assert fake_grpc_server.started is True
    assert created_servers[0].handle_exit is not None
    assert created_servers[1].handle_exit is not None
    assert any(route.path == "/ws/traces" for route in main_app.routes)
    assert any(route.path == "/stream/ui-updates" for route in main_app.routes)
    assert any(route.path == "/v1/traces" for route in otlp_app.routes)
    assert any(route.path == "/v1/logs" for route in otlp_app.routes)
    fake_stop_grpc.assert_awaited_once_with(fake_grpc_server)


def test_run_preserves_config_file_settings_when_flags_omitted(monkeypatch, tmp_path):
    trace_file = tmp_path / "trace.json"
    trace_file.write_text("{}")
    config_file = tmp_path / "eval.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            evaluators:
              - name: tool_trajectory_avg_score
                type: builtin
            eval_set: from-config.json
            trace_format: otlp-json
            output: json
            """
        )
    )

    captured = {}

    async def fake_run_evaluation(config):
        captured["config"] = config
        return RunResult()

    monkeypatch.setattr("agentevals.runner.run_evaluation", fake_run_evaluation)
    monkeypatch.setattr("agentevals.output.format_results", lambda result, fmt: f"format={fmt}")

    result = CliRunner().invoke(cli.main, ["run", str(trace_file), "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert captured["config"].trace_files == [str(trace_file)]
    assert captured["config"].eval_set_file == "from-config.json"
    assert captured["config"].trace_format == "otlp-json"
    assert captured["config"].output_format == "json"
    assert "format=json" in result.output


def test_run_merges_explicit_metrics_with_config_file(monkeypatch, tmp_path):
    trace_file = tmp_path / "trace.json"
    trace_file.write_text("{}")
    config_file = tmp_path / "eval.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            evaluators:
              - name: tool_trajectory_avg_score
                type: builtin
              - name: custom_eval
                type: code
                path: ./examples/custom_evaluators/tool_call_checker.py
            """
        )
    )

    captured = {}

    async def fake_run_evaluation(config):
        captured["config"] = config
        return RunResult()

    monkeypatch.setattr("agentevals.runner.run_evaluation", fake_run_evaluation)
    monkeypatch.setattr("agentevals.output.format_results", lambda result, fmt: f"format={fmt}")

    result = CliRunner().invoke(
        cli.main,
        ["run", str(trace_file), "--config", str(config_file), "-m", "response_match_score"],
    )

    assert result.exit_code == 0, result.output
    assert [e.name for e in captured["config"].evaluators] == [
        "tool_trajectory_avg_score",
        "custom_eval",
        "response_match_score",
    ]
