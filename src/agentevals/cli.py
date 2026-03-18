"""CLI entry point for agentevals.

Usage::

    agentevals run samples/helm.json --eval-set samples/eval_set_helm.json
    agentevals run samples/helm.json -m tool_trajectory_avg_score -m response_match_score
    agentevals run samples/helm.json --eval-set samples/eval_set_helm.json --output json
    agentevals list-metrics
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import click

from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="agentevals")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (-v for INFO, -vv for DEBUG).",
)
def main(verbose: int) -> None:
    """agentevals: Evaluate agent traces using ADK's scoring framework."""
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@main.command()
@click.argument("trace_files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "--eval-set",
    "-e",
    type=click.Path(exists=True),
    default=None,
    help="Path to a golden eval set JSON file (ADK EvalSet format).",
)
@click.option(
    "--metric",
    "-m",
    multiple=True,
    default=None,
    help="Metric(s) to evaluate. Can be specified multiple times. Default: tool_trajectory_avg_score.",
)
@click.option(
    "--format",
    "-f",
    "trace_format",
    default="jaeger-json",
    help="Trace file format.",
)
@click.option(
    "--judge-model",
    "-j",
    default=None,
    help="LLM model for judge-based metrics (default: gemini-2.5-flash).",
)
@click.option(
    "--threshold",
    "-t",
    type=float,
    default=None,
    help="Score threshold for pass/fail.",
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["table", "json", "summary"]),
    default="table",
    help="Output format.",
)
@click.option(
    "--config",
    "-c",
    "config_file",
    type=click.Path(exists=True),
    default=None,
    help="Path to an eval config YAML file defining metrics (including custom).",
)
def run(
    trace_files: tuple[str, ...],
    eval_set: str | None,
    metric: tuple[str, ...] | None,
    trace_format: str,
    judge_model: str | None,
    threshold: float | None,
    output: str,
    config_file: str | None,
) -> None:
    """Evaluate trace file(s) against specified metrics."""
    from .config import EvalRunConfig
    from .output import format_results
    from .runner import run_evaluation

    explicit_metrics = list(metric) if metric else []

    if config_file:
        from .eval_config_loader import load_eval_config, merge_configs

        file_config = load_eval_config(config_file)

        cli_config = EvalRunConfig(
            trace_files=list(trace_files),
            eval_set_file=eval_set,
            metrics=explicit_metrics,
            trace_format=trace_format,
            judge_model=judge_model,
            threshold=threshold,
            output_format=output,
        )
        config = merge_configs(file_config, cli_config)
    else:
        effective_metrics = explicit_metrics or ["tool_trajectory_avg_score"]
        config = EvalRunConfig(
            trace_files=list(trace_files),
            eval_set_file=eval_set,
            metrics=effective_metrics,
            trace_format=trace_format,
            judge_model=judge_model,
            threshold=threshold,
            output_format=output,
        )

    result = asyncio.run(run_evaluation(config))
    formatted = format_results(result, fmt=output)
    click.echo(formatted)

    has_failure = any(
        mr.eval_status == "FAILED" or mr.error
        for tr in result.trace_results
        for mr in tr.metric_results
    )
    if has_failure or result.errors:
        sys.exit(1)


@main.command("list-metrics")
def list_metrics() -> None:
    """List all available evaluation metrics."""
    try:
        from google.adk.evaluation.metric_evaluator_registry import (
            DEFAULT_METRIC_EVALUATOR_REGISTRY,
        )

        metrics = DEFAULT_METRIC_EVALUATOR_REGISTRY.get_registered_metrics()
        click.echo("Available metrics:\n")
        for m in metrics:
            desc = m.description or "No description"
            click.echo(f"  {m.metric_name}")
            click.echo(f"    {desc}")
            if m.metric_value_info and m.metric_value_info.interval:
                iv = m.metric_value_info.interval
                lo = f"{'(' if iv.open_at_min else '['}{iv.min_value}"
                hi = f"{iv.max_value}{')' if iv.open_at_max else ']'}"
                click.echo(f"    Value range: {lo}, {hi}")
            click.echo()
    except ImportError as exc:
        click.echo(
            f"Could not load full metric registry ({exc}).\n"
            "Some eval dependencies may be missing. Install with:\n"
            '  pip install "google-adk[eval]"\n'
        )
        click.echo("Known built-in metrics:\n")
        from google.adk.evaluation.eval_metrics import PrebuiltMetrics

        for pm in PrebuiltMetrics:
            click.echo(f"  {pm.value}")
        click.echo()


def _link_server_shutdown(*servers) -> None:
    """Link multiple uvicorn servers so a single SIGINT shuts down all of them.

    Uvicorn installs per-server signal handlers; the last server's handler
    overwrites earlier ones.  This replaces handle_exit on every server with
    a shared callback that sets should_exit / force_exit on all of them.
    """
    import signal as _signal

    def _shared_exit(sig, frame):
        force = all(s.should_exit for s in servers)
        for s in servers:
            if force and sig == _signal.SIGINT:
                s.force_exit = True
            else:
                s.should_exit = True

    for s in servers:
        s.handle_exit = _shared_exit


@main.command("serve")
@click.option(
    "--dev",
    is_flag=True,
    help="Enable dev mode with WebSocket support for live streaming.",
)
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind the server to.",
)
@click.option(
    "--port",
    "-p",
    default=8001,
    help="Port to bind the server to.",
)
@click.option(
    "--otlp-port",
    default=4318,
    help="Port for OTLP HTTP receiver (default: 4318, standard OTLP HTTP port).",
)
@click.option(
    "--eval-sets",
    type=click.Path(exists=True),
    default=None,
    help="Directory containing eval set JSON files to pre-load.",
)
@click.option(
    "--headless",
    is_flag=True,
    help="Run in headless mode (no browser launch).",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (-v for INFO, -vv for DEBUG).",
)
def serve(dev: bool, host: str, port: int, otlp_port: int, eval_sets: str | None, headless: bool, verbose: int) -> None:
    """Start the agentevals API server.

    Use --dev to enable live streaming mode for agent development.
    """
    import uvicorn
    from pathlib import Path

    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if headless:
        os.environ["AGENTEVALS_HEADLESS"] = "1"

    static_dir = Path(__file__).parent / "_static"
    has_ui = static_dir.is_dir() and (static_dir / "index.html").exists()

    os.environ["AGENTEVALS_LIVE"] = "1"

    if dev:
        click.echo("agentevals dev server starting...")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}  (OTEL_EXPORTER_OTLP_ENDPOINT default)")
        click.echo(f"  WebSocket: ws://{host}:{port}/ws/traces")
        click.echo(f"  API:       http://{host}:{port}/api")
        click.echo("  Web UI:    http://localhost:5173")
        click.echo()

        if eval_sets:
            click.echo(f"  Eval sets: {eval_sets}")
            click.echo()

        click.echo("Waiting for agent connections...")
        click.echo()

        src_path = Path(__file__).parent.parent

        async def _run_dev_servers():
            main_config = uvicorn.Config(
                "agentevals.api.app:app",
                host=host,
                port=port,
                reload=True,
                reload_dirs=[str(src_path)],
                log_level="info",
            )
            otlp_config = uvicorn.Config(
                "agentevals.api.otlp_app:otlp_app",
                host=host,
                port=otlp_port,
                reload=True,
                reload_dirs=[str(src_path)],
                log_level="info",
            )
            main_server = uvicorn.Server(main_config)
            otlp_server = uvicorn.Server(otlp_config)
            _link_server_shutdown(main_server, otlp_server)
            await asyncio.gather(main_server.serve(), otlp_server.serve())

        asyncio.run(_run_dev_servers())
    elif has_ui and not headless:
        click.echo(f"agentevals: http://{host}:{port}")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}")
        click.echo()

        async def _run_ui_servers():
            main_config = uvicorn.Config(
                "agentevals.api.app:app",
                host=host,
                port=port,
                reload=False,
                log_level="warning",
            )
            otlp_config = uvicorn.Config(
                "agentevals.api.otlp_app:otlp_app",
                host=host,
                port=otlp_port,
                reload=False,
                log_level="warning",
            )
            main_server = uvicorn.Server(main_config)
            otlp_server = uvicorn.Server(otlp_config)
            _link_server_shutdown(main_server, otlp_server)
            await asyncio.gather(main_server.serve(), otlp_server.serve())

        asyncio.run(_run_ui_servers())
    else:
        click.echo(f"agentevals API:  http://{host}:{port}/api")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}")
        click.echo()

        async def _run_headless_servers():
            main_config = uvicorn.Config(
                "agentevals.api.app:app",
                host=host,
                port=port,
                reload=False,
                log_level="warning",
            )
            otlp_config = uvicorn.Config(
                "agentevals.api.otlp_app:otlp_app",
                host=host,
                port=otlp_port,
                reload=False,
                log_level="warning",
            )
            main_server = uvicorn.Server(main_config)
            otlp_server = uvicorn.Server(otlp_config)
            _link_server_shutdown(main_server, otlp_server)
            await asyncio.gather(main_server.serve(), otlp_server.serve())

        asyncio.run(_run_headless_servers())


@main.command("mcp")
@click.option(
    "--server-url",
    default=None,
    help="agentevals server URL for session tools (default: http://localhost:8001 or AGENTEVALS_SERVER_URL).",
)
def mcp_command(server_url: str | None) -> None:
    """Start the MCP server on stdio for use with Claude Code and other MCP clients."""
    try:
        from .mcp_server import create_server
    except ImportError:
        click.echo('MCP requires the live extras: pip install "agentevals[live]"', err=True)
        sys.exit(1)

    create_server(server_url=server_url).run("stdio")


if __name__ == "__main__":
    main()
