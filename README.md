<h1 align="center">agentevals</h1>

`agentevals` scores agent behavior from OpenTelemetry traces without re-running the agent. It parses trace spans from `otlp` streams, or Jaeger JSON format and evaluates them against golden eval sets using ADK's evaluation framework.

The tool provides a CLI for local dev work, scripting and CI pipelines, a web UI for visual inspection, EvalSet creation and interactive evaluation, and an MCP server so Claude Code can run evaluations and inspect live sessions directly from a conversation.

> [!IMPORTANT]
> This project is under active development. Expect breaking changes.

## Installation

Download a release wheel from the [releases page](../../releases). Two variants are available — both share the same filename but differ in contents:

| Variant | Description |
|---------|-------------|
| **core** | CLI + REST API — batch evaluation only |
| **bundle** | CLI + REST API + WebSocket streaming + embedded web UI |

```bash
pip install agentevals-<version>-py3-none-any.whl
```

The bundled wheel enables live streaming (WebSocket + SSE + session management) automatically when served.

To also use the MCP server (`agentevals mcp`), install with the `[live]` extra which adds `mcp` and `httpx`:

```bash
pip install "agentevals-<version>-py3-none-any.whl[live]"
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for build instructions.

## Getting Started

Install dependencies using `uv` or Nix:

```bash
# Using uv directly
uv sync

# Using Nix (includes all dependencies)
nix develop .


```

Run a quick evaluation:

```bash
uv run agentevals run samples/helm.json --eval-set samples/eval_set_helm.json -m tool_trajectory_avg_score
```

## CLI Usage

Score a single trace:

```bash
uv run agentevals run samples/helm.json --eval-set samples/eval_set_helm.json -m tool_trajectory_avg_score
```

Score multiple traces at once:

```bash
uv run agentevals run samples/helm.json samples/k8s.json --eval-set samples/eval_set_helm.json -m tool_trajectory_avg_score
```

Output as JSON for programmatic consumption:

```bash
uv run agentevals run samples/helm.json --eval-set samples/eval_set_helm.json --output json
```

List available metrics:

```bash
uv run agentevals list-metrics
```

## Web UI

The React-based UI provides visual trace inspection and interactive evaluation.

**Installed bundle** (single command, UI served at port 8001):

```bash
agentevals serve
```

**From source** (two terminals):

```bash
# Terminal 1
uv run agentevals serve --dev

# Terminal 2
cd ui && npm run dev
```

Open http://localhost:5173 to upload traces and eval sets, select metrics, and view results with interactive span trees and actual vs expected comparisons.

Push traces to the websocket endpoint and they'll appear live in the "Local Dev" tab. Select eval sets and metrics to evaluate the received traces, with results grouped by session ID for easy comparison across runs.

## MCP Server

agentevals exposes its evaluation capabilities as an MCP server. With it active, MCP clients can list sessions, run evaluations, and inspect traces directly from a conversation — no manual HTTP calls or file management needed.

A `.mcp.json` is included at the project root so clients, e.g. Claude Code picks it up automatically when you open this directory. Reload with `/mcp` to verify the server is active.

### Available tools

| Tool | Requires `serve` | Description |
|------|:---:|-------------|
| `list_metrics` | yes | List all available metrics with descriptions and requirements |
| `evaluate_traces` | no | Evaluate local trace files (OTLP or Jaeger) by file path |
| `list_sessions` | yes | List active and completed streaming sessions |
| `summarize_session` | yes | Get a structured summary of a session's invocations and tool calls |
| `evaluate_sessions` | yes | Evaluate all completed sessions against a golden reference |

`evaluate_traces` works standalone — it imports the evaluation engine directly without needing the server running. All other tools require `agentevals serve --dev`.

### Usage

```
# In a Claude Code conversation (with agentevals serve --dev running):

"List my sessions"
→ calls list_sessions, shows session IDs and completion status

"What did session langchain-session-abc123 do?"
→ calls summarize_session, returns invocations, messages, and tool calls

"Use session langchain-session-abc123 as the golden and evaluate the rest"
→ calls evaluate_sessions, returns per-session scores with a top-level passed/failed

"Evaluate /path/to/trace.otlp.jsonl using tool_trajectory_avg_score"
→ calls evaluate_traces, no server needed
```

The React UI and Claude Code share the same in-memory session state — running both simultaneously works fine.

### Custom server URL

```bash
AGENTEVALS_SERVER_URL=http://localhost:9000 uv run agentevals mcp
# or
uv run agentevals mcp --server-url http://localhost:9000
```

## Claude Code Skills

`.claude/skills/` contains two slash-command workflows that orchestrate the MCP tools into guided conversations. They're available automatically when you open this repo in Claude Code (the `.mcp.json` registers the server; the skills register from `.claude/skills/`).

| Skill | Trigger phrases | Requires `serve --dev` |
|-------|----------------|:---:|
| `/eval` | "eval this trace", "did my agent regress", "compare runs", "score session X" | for sessions |
| `/inspect` | "show me what my agent did", "inspect session", "walk me through the last run" | yes |

### `/eval` — Score agent behavior

Evaluates both local trace files and live streaming sessions:

- **Trace files**: detects format from extension (`.jsonl` → OTLP, `.json` → Jaeger), calls `evaluate_traces`, presents a score table with interpretation
- **Session regression testing**: lists sessions, identifies the golden reference, calls `evaluate_sessions`, shows a comparison table with per-session deltas and explains which tool calls diverged

### `/inspect` — Understand what an agent did

Presents a readable turn-by-turn narrative of a live session:

```
Turn 1:
  User: [what the user asked]
  Tools: tool_name(arg=val, ...) → [what this achieves]
  Response: [response text]
```

Flags anomalies (missing tool calls, repeated calls, abrupt stops) and suggests `/eval` if you want to score the session against a golden reference.

## Local Development

```bash
uv run pytest              # run tests
uv run agentevals serve --dev  # start backend in dev mode
cd ui && npm run dev           # start frontend (separate terminal)
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for build tiers, Makefile targets, Nix setup, and release instructions. If you'd like to contribute, check out [CONTRIBUTING.md](CONTRIBUTING.md).

## FAQ

**How does this compare to ADK's evaluations?**
Unlike ADK's LocalEvalService, which couples agent execution with evaluation, agentevals only handles scoring: it takes pre-recorded traces and compares them against expected behavior using metrics like tool trajectory matching, response quality, and LLM-based judgments.

However, if you're iterating on your agents locally, you can point your agents to agentevals and you will see rich runtime information in your browser. For more details, use the bundled wheel and explore the Local Development option in the UI.

**How does this compare to Bedrock AgentCore's evaluation?**
AgentCore's evaluation integration (via `strands-agents-evals`) also couples agent execution with evaluation. Itre-invokes the agent for each test case, converts the resulting OTel spans to AWS's ADOT format, and scores them against 4 built-in evaluators (Helpfulness, Accuracy, Harmfulness, Relevance) via a cloud API call. This means you need an AWS account, valid credentials, and network access for every evaluation.

agentevals takes a different approach: it scores pre-recorded traces locally without re-running anything. It works with standard Jaeger JSON and OTLP formats from any framework, supports open-ended metrics (tool trajectory matching, LLM-based judges, custom scorers), and ships with a CLI, web UI, and MCP server — no cloud dependency required; however we do include all ADK's GCP-based evals as of now.
