<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/agentevals-dev/agentevals/main/docs/assets/logo-color-on-transparent.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/agentevals-dev/agentevals/main/docs/assets/logo-dark-on-transparent.svg">
    <img src="https://raw.githubusercontent.com/agentevals-dev/agentevals/main/docs/assets/logo-color-on-transparent.svg" alt="agentevals" width="420" />
  </picture>
</p>

<h1 align="center">Ship Agents Reliably</h1>

<p align="center">
Benchmark your agents before they hit production.<br>
agentevals scores performance and inference quality from OpenTelemetry traces. No re-runs, no guesswork.
</p>

<p align="center">
  <a href="https://github.com/agentevals-dev/agentevals/stargazers"><img src="https://img.shields.io/github/stars/agentevals-dev/agentevals?style=social" alt="GitHub Stars"></a>
  &nbsp;
  <a href="https://discord.gg/cpveEn8Ah2"><img src="https://img.shields.io/discord/1435836734666707190?label=Discord&logo=discord&logoColor=white&color=5865F2" alt="Discord"></a>
  &nbsp;
  <a href="https://github.com/agentevals-dev/agentevals/releases"><img src="https://img.shields.io/github/v/release/agentevals-dev/agentevals?label=Release" alt="Release"></a>
  &nbsp;
  <a href="https://github.com/agentevals-dev/agentevals/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="License"></a>
  &nbsp;
  <a href="https://pypi.org/project/agentevals-cli/"><img src="https://img.shields.io/pypi/v/agentevals-cli?label=PyPI&color=blue" alt="PyPI"></a>
</p>

<p align="center">
  <a href="#installation">Install</a> · <a href="#quick-start">Quick Start</a> · <a href="https://github.com/agentevals-dev/agentevals/releases">Releases</a> · <a href="CONTRIBUTING.md">Contributing</a> · <a href="https://discord.gg/cpveEn8Ah2">Discord</a>
</p>

---

## What is agentevals?

agentevals is a framework-agnostic evaluation solution that scores AI agent behavior directly from [OpenTelemetry](https://opentelemetry.io/) traces. Record your agent's actions once, then evaluate as many times as you want without re-executing or burning extra tokens.

It works with any OTel-instrumented framework (LangChain, Strands, Google ADK, OpenAI Agents SDK, and others), supports Jaeger JSON and native OTLP trace formats, and ships with built-in evaluators, custom evaluator support, and LLM-based judges.

- **No re-execution**: score agents from existing traces without replaying expensive LLM calls
- **Golden eval sets**: compare actual behavior against defined expected behaviors for deterministic pass/fail gating
- **Custom evaluators**: write scoring logic in Python, JavaScript, or any language, or offload scoring to OpenAI Eval API
- **CI/CD ready**: gate deployments on quality thresholds directly in your pipeline
- **Local-first**: no cloud dependency required; everything runs on your machine
- **Multiple interfaces**: CLI for scripting and CI, Web UI for visual inspection, MCP server for conversational evaluation, Helm chart for Kubernetes environments

> [!IMPORTANT]
> This project is under active development. Expect breaking changes.

## Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Use-cases and Integrations](#use-cases-and-integrations)
- [CLI](#cli)
- [Custom Evaluators](#custom-evaluators)
- [Web UI](#web-ui)
- [Deployment](#deployment)
- [MCP Server](#mcp-server)
- [Claude Code Skills](#claude-code-skills)
- [Examples](#examples)
- [Docs](#docs)
- [Development](#development)
- [FAQ](#faq)

## Installation

**From PyPI** (recommended): the published package includes the **CLI**, **REST API**, and **embedded web UI**.

```bash
pip install agentevals-cli
```

Optional extras:

```bash
pip install "agentevals-cli[live]"        # MCP server support
pip install "agentevals-cli[openai]"      # OpenAI Evals API graders
```

**GitHub [releases](../../releases)** also ship **core** wheels (CLI and API only) and **bundle** wheels (with the embedded UI) if you need a specific version or offline `pip install ./path/to.whl`.

**From source** with `uv` or Nix:

```bash
uv sync
# or: nix develop .
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for build instructions.

## Quick Start

Examples use `agentevals` on your PATH after `pip install agentevals-cli`. If you are working from a clone of this repo, use `uv run agentevals` instead.

The `samples/` directory includes real traces from a Kubernetes Helm agent and matching eval sets that define expected behavior (which tools should be called, what the response should contain).

**Score a trace against an eval set:**

```bash
agentevals run samples/helm.json \
  --eval-set samples/eval_set_helm.json \
  -m tool_trajectory_avg_score
```

The agent was asked to list Helm releases. The eval set expects a call to `helm_list_releases`. It matches:

```
Trace: 3e289017fe03ffd7c4145316d2eb3d0d
Invocations: 1
        Metric                       Score  Status      Per-Invocation  Time
------  -------------------------  -------  --------  ----------------  ------
[PASS]  tool_trajectory_avg_score        1  PASSED                   1  0ms
```

**Catch a mismatch.** Run a different trace against the same eval set:

```bash
agentevals run samples/k8s.json \
  --eval-set samples/eval_set_helm.json \
  -m tool_trajectory_avg_score
```

This trace is from a different agent session that never called the expected tool. The evaluation fails:

```
[FAIL]  tool_trajectory_avg_score        0  FAILED                   0  0ms
  Invocation 1 trajectory mismatch:
    Expected:
      - helm_list_releases({})
    Actual:
      (none)
```

**Evaluate multiple dimensions at once:**

```bash
agentevals run samples/helm_3.json \
  --eval-set samples/evalset_helm_3_2026-02-23.json \
  -m tool_trajectory_avg_score \
  -m response_match_score
```

`tool_trajectory_avg_score` checks whether the right tools were called. `response_match_score` checks whether the agent's final answer matches the expected response.

**Explore visually.** Launch the Web UI and upload traces from the browser:

```bash
agentevals serve
# opens http://localhost:8001
```

You can also point any OTel-instrumented agent directly at the built-in receiver (`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`). The UI streams tool calls, inputs, and outputs live as your agent runs. For production setups, the same receiver slots into a Kubernetes OTel Collector pipeline as an exporter destination. See [Use-cases and Integrations](#use-cases-and-integrations) and the [Kubernetes example](examples/kubernetes/README.md) for walkthroughs.

**Next steps:**

- `agentevals evaluator list` to see all built-in and community evaluators
- [Custom Evaluators](#custom-evaluators) to write your own scoring logic

## Use-cases and Integrations

### Zero-Code (Recommended)

Point any OTel-instrumented agent at the agentevals receiver. No SDK, no code changes:

```bash
# Terminal 1
agentevals serve --dev

# Terminal 2
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_RESOURCE_ATTRIBUTES="agentevals.session_name=my-agent"
python your_agent.py
```

For OTLP/gRPC exporters, use:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

Traces stream to the UI in real-time. Works with LangChain, Strands, Google ADK, OpenAI Agents SDK, or any framework that emits OTel spans (`http/protobuf`, `http/json`, and OTLP/gRPC supported). Sessions are auto-created and grouped by `agentevals.session_name`. Set `agentevals.eval_set_id` to associate traces with an eval set.

See [examples/zero-code-examples/](examples/zero-code-examples/) for working examples.

### AgentEvals SDK

For programmatic session lifecycle and decorator API:

```python
from agentevals import AgentEvals

app = AgentEvals()

with app.session(eval_set_id="my-eval"):
    agent.invoke("Roll a 20-sided die for me")
```

Requires `pip install "agentevals-cli[streaming]"`. See [examples/sdk_example/](examples/sdk_example/) for framework-specific patterns.

## CLI

```bash
# Multiple traces, JSON output
agentevals run samples/helm.json samples/k8s.json \
  --eval-set samples/eval_set_helm.json \
  -m tool_trajectory_avg_score \
  --output json

# List available evaluators
agentevals evaluator list

# Flexible trajectory matching (EXACT | IN_ORDER | ANY_ORDER)
agentevals run trace.json \
  --eval-set eval_set.json \
  -m tool_trajectory_avg_score \
  --trajectory-match-type IN_ORDER
```

Run `agentevals run --help` for all options.

## Custom Evaluators

Write scoring logic in Python, JavaScript, or any language. Scaffold a new evaluator with:

```bash
agentevals evaluator init my_evaluator
```

Reference it alongside built-in metrics in an eval config:

```yaml
evaluators:
  - name: tool_trajectory_avg_score
    type: builtin
  - name: my_evaluator
    type: code
    path: ./evaluators/my_evaluator.py
    threshold: 0.7
```

Evaluators with a `requirements.txt` get automatic virtual environment management. You can also use `type: remote` for community evaluators from GitHub, or `type: openai_eval` to delegate grading to the [OpenAI Evals API](https://developers.openai.com/api/reference/resources/evals/methods/create) (requires `pip install "agentevals-cli[openai]"`).

See the [Custom Evaluators guide](docs/custom-evaluators.md) for the full protocol reference, SDK helpers, and how to contribute evaluators.

## Web UI

```bash
agentevals serve            # bundled UI on http://localhost:8001
```

Upload traces and eval sets, select metrics, and view results with interactive span trees. Live-streamed traces appear in the "Local Dev" tab, grouped by session ID. For running from source, see [DEVELOPMENT.md](DEVELOPMENT.md).

Interactive API docs are available at `/docs` (Swagger) and `/redoc` while the server is running. The OTLP receiver on port 4318 serves its own docs at `http://localhost:4318/docs`.

## Deployment

### Docker

A `Dockerfile` is included at the project root. The image bundles the API, web UI, and OTLP receiver:

```bash
docker build -t agentevals .
docker run -p 8001:8001 -p 4317:4317 -p 4318:4318 agentevals
```

| Port | Purpose |
|------|---------|
| 8001 | Web UI and REST API |
| 4317 | OTLP gRPC receiver (traces and logs) |
| 4318 | OTLP HTTP receiver (traces and logs) |
| 8080 | MCP (Streamable HTTP) |

### Helm

The Helm chart is published as an OCI artifact to GitHub Container Registry:

```bash
helm install agentevals oci://ghcr.io/agentevals-dev/agentevals/helm/agentevals
```

Pass `--version <x.y.z>` to pin to a specific release. Available versions are listed under [packages](https://github.com/agentevals-dev/agentevals/pkgs/container/agentevals%2Fhelm%2Fagentevals).

The source for the chart lives in [`charts/agentevals/`](charts/agentevals/) if you want to install from a local checkout instead.

See the [Kubernetes example](examples/kubernetes/README.md) for an end-to-end walkthrough deploying agentevals alongside kagent and an OTel Collector on Kubernetes.

#### Postgres backend (`/api/runs`)

By default the chart deploys agentevals with an in-memory backend; runs and results are not persisted. To enable the async `POST /api/runs` pipeline with durable Postgres-backed state:

```bash
# Bundled Postgres (dev / evaluation only):
helm install agentevals oci://ghcr.io/agentevals-dev/agentevals/helm/agentevals \
    --set storage.backend=postgres \
    --set database.postgres.bundled.enabled=true

# Or supply an external Postgres DSN:
helm install agentevals oci://ghcr.io/agentevals-dev/agentevals/helm/agentevals \
    --set storage.backend=postgres \
    --set database.postgres.url='postgresql://user:pass@host:5432/dbname'
```

When `storage.backend=postgres` the app applies any pending schema migrations on startup (advisory-lock protected, safe across replicas) and starts an in-process worker that processes the run queue. Without `storage.backend=postgres` the `/api/runs` endpoints return 503 with a hint pointing at the env var.

## MCP Server

Exposes evaluation tools to MCP clients. A `.mcp.json` at the project root lets Claude Code pick it up automatically.

| Tool | Requires `serve` | Description |
|------|:---:|-------------|
| `list_metrics` | yes | List available metrics |
| `evaluate_traces` | no | Evaluate local trace files (OTLP or Jaeger) |
| `list_sessions` | yes | List streaming sessions |
| `summarize_session` | yes | Structured summary of a session's tool calls |
| `evaluate_sessions` | yes | Evaluate sessions against a golden reference |

```bash
# Custom server URL (requires pip install "agentevals-cli[live]")
AGENTEVALS_SERVER_URL=http://localhost:9000 agentevals mcp
```

The React UI and MCP server share the same in-memory session state and can run simultaneously.

## Claude Code Skills

Two slash-command workflows in `.claude/skills/`, available automatically in this repo:

| Skill | What it does |
|-------|-------------|
| `/eval` | Score traces or compare sessions against a golden reference |
| `/inspect` | Turn-by-turn narrative of a live session with anomaly detection |

## Examples

Working examples are in the [`examples/`](examples/) directory:

| Example | Description |
|---------|-------------|
| [ADK](examples/zero-code-examples/adk/) | Google ADK agent with zero-code OTel export |
| [LangChain](examples/zero-code-examples/langchain/) | LangChain agent with zero-code OTel export |
| [Strands](examples/zero-code-examples/strands/) | Strands SDK agent with zero-code OTel export |
| [OpenAI Agents](examples/zero-code-examples/openai-agents/) | OpenAI Agents SDK with zero-code OTel export |
| [Ollama](examples/zero-code-examples/ollama/) | LangChain + Ollama for local LLM evaluation |
| [Kubernetes](examples/kubernetes/) | End-to-end deployment with kagent and OTel Collector |

## Docs

| Guide | Description |
|-------|-------------|
| [Eval Set Format](docs/eval-set-format.md) | Schema, field reference, and examples for golden eval set JSON files |
| [Custom Evaluators](docs/custom-evaluators.md) | Write your own scoring logic in Python, JavaScript, or any language |
| [Live Streaming](docs/streaming.md) | Real-time trace streaming, dev server setup, and session management |
| [OpenTelemetry Compatibility](docs/otel-compatibility.md) | Supported OTel conventions, message delivery mechanisms, and OTLP receiver |

## Development

```bash
uv run pytest                      # run tests
uv run agentevals serve --dev      # backend
cd ui && npm run dev               # frontend (separate terminal)
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for build tiers, Makefile targets, and Nix setup. To contribute, see [CONTRIBUTING.md](CONTRIBUTING.md).

## FAQ

**Do I need a database or any infrastructure to run agentevals?**

No. agentevals is a single `pip install` with no database, no message queue, and no external services. The CLI evaluates trace files directly from disk. The web UI and live streaming use in-memory session state.

**Does the CLI require a running server?**

No. `agentevals run` evaluates trace files entirely offline. The server (`agentevals serve`) is only needed for the web UI, live OTLP streaming, and server-dependent MCP tools like `list_sessions`.

**Can I use agentevals in CI/CD?**

Yes. Pass trace files and an eval set, set a threshold, and let the exit code gate your deployment. Combine with `--output json` for machine-readable results. No server process needed.

**What if I switch agent frameworks?**

Because agentevals uses OpenTelemetry as its universal interface, switching frameworks does not require changing your evaluation setup. As long as your new framework emits OTel spans, the same eval sets and metrics work as before.

**Can I write evaluators in my own language?**

Yes. A custom evaluator is any program that reads JSON from stdin and writes a score to stdout. Python and JavaScript have first-class scaffolding support (`agentevals evaluator init`), but any language works.

**Can I plug agentevals into an existing OTel pipeline?**

Yes. The OTLP receiver on port 4318 accepts standard `http/protobuf` and `http/json` trace exports, so it slots into any OpenTelemetry pipeline as just another exporter destination. If your pipeline uses gRPC (port 4317), place an [OTel Collector](https://opentelemetry.io/docs/collector/) in front to bridge gRPC to HTTP. The [Kubernetes example](examples/kubernetes/README.md) shows this pattern.

**How does this compare to ADK's evaluations?**

Unlike ADK's eval method, which couples agent execution with evaluation, agentevals only handles scoring: it takes pre-recorded traces and compares them against expected behavior using metrics like tool trajectory matching, response quality, and LLM-based judgments.

However, if you're iterating on your agents locally, you can point your agents to agentevals and you will see rich runtime information in your browser. For more details, use the bundled wheel and explore the Local Development option in the UI.

**How does this compare to Bedrock AgentCore's evaluation?**

AgentCore's evaluation integration (via `strands-agents-evals`) also couples agent execution with evaluation. It re-invokes the agent for each test case, converts the resulting OTel spans to AWS's ADOT format, and scores them against 4 built-in evaluators (Helpfulness, Accuracy, Harmfulness, Relevance) via a cloud API call. This means you need an AWS account, valid credentials, and network access for every evaluation.

agentevals scores pre-recorded traces locally without re-running anything. It works with standard Jaeger JSON and OTLP formats from any framework, supports open-ended metrics (tool trajectory matching, LLM-based judges, custom scorers), and ships with a CLI, web UI, and MCP server. No cloud dependency required, though we do include all ADK's GCP-based evals as of now.

**How does this compare to LangSmith?**

LangSmith is a cloud platform (self-hosting requires an Enterprise plan) where offline evaluation re-executes your application against curated datasets. Its deepest integration is with LangChain/LangGraph, though it can work with other frameworks. agentevals scores pre-recorded OTel traces without re-execution, requires no cloud account or enterprise license, and uses OpenTelemetry as the universal interface rather than a proprietary SDK.

**How does this compare to Langfuse?**

Langfuse is a full observability platform (requires Postgres, ClickHouse, Redis, and S3 for self-hosting) that supports both offline experiments (re-execution) and online evaluation of ingested traces. Traces must be ingested into Langfuse first via its SDK or OTel integration before they can be scored. agentevals evaluates raw OTel trace files or live OTLP streams directly with no database or platform infrastructure required.

**How does this compare to Opik?**

Opik's primary evaluation path re-runs your application code against a dataset, incurring additional LLM costs per eval run. It also supports online evaluation rules that auto-score production traces. While Opik supports OpenTelemetry ingestion alongside its own SDK, its evaluation workflow still centers on re-execution against datasets. agentevals evaluates pre-recorded OTel traces from any framework without re-execution, and runs entirely locally with no cloud dependency.
