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
from datetime import datetime, timezone

import click

from . import __version__


def _relative_time(iso_str: str | None) -> str:
    """Format an ISO 8601 timestamp as a human-readable relative time string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        years = days // 365
        return f"{years}y ago"
    except (ValueError, TypeError):
        return ""


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

    has_failure = any(mr.eval_status == "FAILED" or mr.error for tr in result.trace_results for mr in tr.metric_results)
    if has_failure or result.errors:
        sys.exit(1)


@main.command("list-metrics")
def list_metrics() -> None:
    """List all available evaluation metrics.

    DEPRECATED: use ``agentevals evaluator list --source builtin`` instead.
    """
    click.echo(
        "Note: list-metrics is deprecated. Use 'agentevals evaluator list --source builtin' instead.\n",
        err=True,
    )
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


# ---------------------------------------------------------------------------
# agentevals evaluator ...
# ---------------------------------------------------------------------------


@main.group()
def evaluator() -> None:
    """Manage evaluators: scaffold, list, and discover."""


@evaluator.command("init")
@click.argument("name")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    default=".",
    help="Parent directory for the new evaluator folder (default: current directory).",
)
@click.option(
    "--runtime",
    "-r",
    default=None,
    help="Language runtime: py, js, ts (default: inferred from name or py).",
)
def evaluator_init(name: str, output_dir: str, runtime: str | None) -> None:
    """Scaffold a new evaluator with boilerplate code and an evaluator.yaml manifest.

    NAME is the evaluator name. If it ends with a recognized extension (.py, .js,
    .ts) the language is inferred automatically; otherwise use --runtime.

    \b
    Examples:
      agentevals evaluator init my_evaluator
      agentevals evaluator init my_evaluator.ts
      agentevals evaluator init my_evaluator --runtime js
    """
    from pathlib import Path as _Path

    from .evaluator.templates import scaffold_evaluator

    try:
        evaluator_dir = scaffold_evaluator(name, output_dir=_Path(output_dir), runtime=runtime)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Created evaluator in {evaluator_dir}/")
    click.echo()
    click.echo("Files:")
    for f in sorted(evaluator_dir.iterdir()):
        click.echo(f"  {f.relative_to(evaluator_dir.parent)}")
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Implement your scoring logic in the generated code file")
    click.echo("  2. Add it to your eval_config.yaml under 'evaluators':")
    click.echo()

    code_files = [f for f in evaluator_dir.iterdir() if f.suffix in (".py", ".js", ".ts")]
    evaluator_name = evaluator_dir.name
    if code_files:
        rel = code_files[0].relative_to(evaluator_dir.parent)
        click.echo("     evaluators:")
        click.echo(f"       - name: {evaluator_name}")
        click.echo("         type: code")
        click.echo(f"         path: ./{rel}")
        click.echo("         threshold: 0.5")
    click.echo()
    click.echo("  3. Run: agentevals run <trace_file> --config eval_config.yaml")


@evaluator.command("runtimes")
def evaluator_runtimes() -> None:
    """Show supported language runtimes and execution environments."""
    from .custom_evaluators import _EXECUTOR_FACTORIES, get_runtimes

    click.echo("Language runtimes:\n")
    for rt in get_runtimes():
        exts = ", ".join(rt.extensions)
        available = "available" if rt.is_available() else "not found"
        click.echo(f"  {rt.name:<12}  extensions: {exts:<16}  ({available})")

    click.echo("\nExecutors:\n")
    for name in sorted(_EXECUTOR_FACTORIES):
        click.echo(f"  {name}")

    click.echo()


@evaluator.command("list")
@click.option(
    "--source",
    "-s",
    type=click.Choice(["all", "builtin", "github"]),
    default="all",
    help="Filter evaluators by source (default: all).",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Ignore cached results and fetch fresh data.",
)
def evaluator_list(source: str, refresh: bool) -> None:
    """List available evaluators from all registered sources."""
    from .evaluator.sources import _cache_dir, get_sources

    if refresh:
        import shutil

        cache = _cache_dir()
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)

    sources = get_sources()
    if source != "all":
        sources = [s for s in sources if s.source_name == source]

    click.echo("  Fetching evaluators...", nl=False)
    all_evaluators = asyncio.run(_collect_evaluators(sources))
    click.echo("\r" + " " * 30 + "\r", nl=False)

    if not all_evaluators:
        click.echo("No evaluators found.")
        return

    max_name = max(len(g.name) for g in all_evaluators)
    max_src = max(len(g.source) for g in all_evaluators)

    has_updated = any(g.last_updated for g in all_evaluators)
    updated_col_width = 10 if has_updated else 0

    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 120

    overhead = max_name + max_src + 8
    if has_updated:
        overhead += updated_col_width + 2
    desc_width = max(20, term_width - overhead)

    hdr_updated = f"  {'UPDATED':<{updated_col_width}}" if has_updated else ""
    sep_updated = f"  {'-' * updated_col_width}" if has_updated else ""

    click.echo(f"  {'NAME':<{max_name}}  {'SOURCE':<{max_src}}{hdr_updated}  DESCRIPTION")
    click.echo(f"  {'-' * max_name}  {'-' * max_src}{sep_updated}  {'-' * min(40, desc_width)}")

    for g in sorted(all_evaluators, key=lambda x: (x.source, x.name)):
        lang = f" [{g.language}]" if g.language else ""
        desc = g.description + lang
        if len(desc) > desc_width:
            desc = desc[: desc_width - 3] + "..."
        col_updated = f"  {_relative_time(g.last_updated):<{updated_col_width}}" if has_updated else ""
        click.echo(f"  {g.name:<{max_name}}  {g.source:<{max_src}}{col_updated}  {desc}")

    click.echo(f"\n  {len(all_evaluators)} evaluator(s) found.")


async def _collect_evaluators(sources):
    """Gather evaluator lists from all sources concurrently."""
    import asyncio as _asyncio

    from .evaluator.sources import EvaluatorInfo

    results: list[EvaluatorInfo] = []
    tasks = [s.list_evaluators() for s in sources]
    for evaluators in await _asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(evaluators, BaseException):
            click.echo(f"  Warning: failed to fetch from a source: {evaluators}", err=True)
            continue
        results.extend(evaluators)
    return results


@evaluator.command("config")
@click.argument("name")
@click.option(
    "--path",
    "-p",
    "evaluator_path",
    default=None,
    help="Path to the evaluator script (used for local code evaluators).",
)
@click.option(
    "--threshold",
    "-t",
    type=float,
    default=None,
    help="Score threshold (default: 0.5 for custom evaluators).",
)
def evaluator_config(name: str, evaluator_path: str | None, threshold: float | None) -> None:
    """Generate an eval_config.yaml snippet for an evaluator."""
    import yaml as _yaml

    from .builtin_metrics import METRICS_NEEDING_EXPECTED, METRICS_NEEDING_GCP, METRICS_NEEDING_LLM
    from .evaluator.sources import get_sources

    sources = get_sources()
    all_evaluators = asyncio.run(_collect_evaluators(sources))

    match = next((g for g in all_evaluators if g.name == name), None)

    if match and match.source == "builtin":
        needs_eval_set = name in METRICS_NEEDING_EXPECTED
        needs_llm = name in METRICS_NEEDING_LLM
        needs_gcp = name in METRICS_NEEDING_GCP

        entry: dict = {"name": name, "type": "builtin"}
        if threshold is not None:
            entry["threshold"] = threshold
        else:
            entry["threshold"] = 0.5
        if needs_llm:
            entry["judge_model"] = "gemini-2.5-flash"

        snippet: dict = {"evaluators": [entry]}

        notes: list[str] = []
        if needs_eval_set:
            notes.append("Requires --eval-set (golden eval set with expected responses)")
        if needs_llm:
            notes.append("Requires GOOGLE_API_KEY (or GEMINI_API_KEY) for LLM judge")
        if needs_gcp:
            notes.append("Requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION (Vertex AI)")

        comment = "# Add to your eval_config.yaml under 'evaluators':"
        if notes:
            comment += "\n#\n# Notes:\n" + "\n".join(f"#   - {n}" for n in notes)
    elif match and match.source != "builtin":
        entry: dict = {
            "name": name,
            "type": "remote",
            "source": match.source,
            "ref": match.ref or f"evaluators/{name}",
        }
        if threshold is not None:
            entry["threshold"] = threshold
        else:
            entry["threshold"] = 0.5
        entry["executor"] = "local"
        snippet = {"evaluators": [entry]}
        comment = "# Add to your eval_config.yaml under 'evaluators':"
    else:
        path_val = evaluator_path or f"./{name}/{name}.py"
        entry = {
            "name": name,
            "type": "code",
            "path": path_val,
        }
        if threshold is not None:
            entry["threshold"] = threshold
        else:
            entry["threshold"] = 0.5
        entry["executor"] = "local"
        snippet = {"evaluators": [entry]}
        comment = "# Add to your eval_config.yaml under 'evaluators':"

    rendered = _yaml.dump(snippet, default_flow_style=False, sort_keys=False)
    click.echo(f"\n{comment}\n")
    click.echo(rendered)


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


async def _run_servers(
    host: str,
    port: int,
    otlp_port: int,
    *,
    mcp_port: int | None = None,
    reload: bool = False,
    reload_dirs: list[str] | None = None,
    log_level: str = "warning",
) -> None:
    """Start the main API, OTLP HTTP server, and optionally MCP (Streamable HTTP)."""
    import uvicorn

    shared_kwargs: dict = {
        "host": host,
        "reload": reload,
        "log_level": log_level,
    }
    if reload_dirs:
        shared_kwargs["reload_dirs"] = reload_dirs

    main_server = uvicorn.Server(uvicorn.Config("agentevals.api.app:app", port=port, **shared_kwargs))
    otlp_server = uvicorn.Server(uvicorn.Config("agentevals.api.otlp_app:otlp_app", port=otlp_port, **shared_kwargs))
    servers: list = [main_server, otlp_server]

    if mcp_port is not None:
        from .mcp_server import create_server as create_mcp_server

        backend = (os.environ.get("AGENTEVALS_SERVER_URL") or f"http://127.0.0.1:{port}").rstrip("/")
        mcp_instance = create_mcp_server(
            server_url=backend,
            host=host,
            port=mcp_port,
        )
        mcp_app = mcp_instance.streamable_http_app()
        mcp_kwargs = {**shared_kwargs, "reload": False, "port": mcp_port}
        mcp_uvicorn = uvicorn.Server(uvicorn.Config(mcp_app, **mcp_kwargs))
        servers.append(mcp_uvicorn)

    _link_server_shutdown(*servers)
    await asyncio.gather(*(s.serve() for s in servers))


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
    "--mcp-port",
    default=None,
    type=int,
    help="If set, expose the MCP interface over Streamable HTTP on this port (requires [live] extras).",
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
def serve(
    dev: bool,
    host: str,
    port: int,
    otlp_port: int,
    mcp_port: int | None,
    eval_sets: str | None,
    headless: bool,
    verbose: int,
) -> None:
    """Start the agentevals API server.

    Use --dev to enable live streaming mode for agent development.
    """
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

    if mcp_port is not None:
        try:
            from . import mcp_server as _mcp_server_check  # noqa: F401
        except ImportError:
            click.echo('MCP HTTP requires the live extras: pip install "agentevals[live]"', err=True)
            raise SystemExit(1) from None

    if dev:
        click.echo("agentevals dev server starting...")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}  (OTEL_EXPORTER_OTLP_ENDPOINT default)")
        click.echo(f"  WebSocket: ws://{host}:{port}/ws/traces")
        click.echo(f"  API:       http://{host}:{port}/api")
        if mcp_port is not None:
            click.echo(f"  MCP:       http://{host}:{mcp_port}/mcp")
        click.echo("  Web UI:    http://localhost:5173")
        click.echo()

        if eval_sets:
            click.echo(f"  Eval sets: {eval_sets}")
            click.echo()

        click.echo("Waiting for agent connections...")
        click.echo()

        src_path = Path(__file__).parent.parent
        reload_dirs = [str(src_path)]
        asyncio.run(
            _run_servers(
                host,
                port,
                otlp_port,
                mcp_port=mcp_port,
                reload=True,
                reload_dirs=reload_dirs,
                log_level="info",
            )
        )
    elif has_ui and not headless:
        click.echo(f"agentevals: http://{host}:{port}")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}")
        if mcp_port is not None:
            click.echo(f"  MCP (Streamable HTTP): http://{host}:{mcp_port}/mcp")
        click.echo()

        asyncio.run(_run_servers(host, port, otlp_port, mcp_port=mcp_port))
    else:
        click.echo(f"agentevals API:  http://{host}:{port}/api")
        click.echo(f"  OTLP HTTP: http://{host}:{otlp_port}")
        if mcp_port is not None:
            click.echo(f"  MCP (Streamable HTTP): http://{host}:{mcp_port}/mcp")
        click.echo()

        asyncio.run(_run_servers(host, port, otlp_port, mcp_port=mcp_port))


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
