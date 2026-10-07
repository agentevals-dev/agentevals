# Instrumenting Agents for agentevals

agentevals evaluates AI agents by consuming their [OpenTelemetry](https://opentelemetry.io/) traces. Any agent that emits OTel spans can be evaluated.

This guide covers the instrumentation patterns agentevals supports, with a recommendation for new projects. Each example in this directory is a working agent you can run and modify.

## Zero-Code OTLP (Recommended)

The simplest way to connect any agent to agentevals. Point your standard OTel OTLP exporter at the agentevals receiver and you're done. No agentevals dependency needed in your agent code.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_RESOURCE_ATTRIBUTES="agentevals.session_name=my-agent,agentevals.eval_set_id=my-eval"
python your_agent.py
```

For OTLP/gRPC exporters, use:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

agentevals accepts OTLP/HTTP on port 4318 (`http/protobuf` and `http/json`) and OTLP/gRPC on port 4317. Sessions are auto-created from incoming traces and grouped by `agentevals.session_name`.

| Example | Framework | LLM Provider |
|---------|-----------|-------------|
| [zero-code-examples/langchain/](./zero-code-examples/langchain/) | LangChain | OpenAI |
| [zero-code-examples/ollama/](./zero-code-examples/ollama/) | LangChain | Ollama |
| [zero-code-examples/strands/](./zero-code-examples/strands/) | Strands | OpenAI |
| [zero-code-examples/adk/](./zero-code-examples/adk/) | Google ADK | Gemini |
| [zero-code-examples/pydantic-ai/](./zero-code-examples/pydantic-ai/) | Pydantic AI | OpenAI |

This approach works with any framework that has OTel instrumentation: LangChain, Strands, Google ADK, etc. If your framework already emits OTel spans, you only need to add `OTLPSpanExporter` (and `OTLPLogExporter` if it uses GenAI log-based content delivery).

### Resource attributes

| Attribute | Required | Description |
|-----------|----------|-------------|
| `agentevals.session_name` | no | Groups spans into a named session. Without it, sessions are named `otlp-<traceId prefix>`. |
| `agentevals.eval_set_id` | no | Associates the session with an eval set for scoring. |

Set them via `OTEL_RESOURCE_ATTRIBUTES` (env var) or `Resource.create()` in code.

## SDK Integration

For tighter control over session lifecycle, or if you prefer a Python API over environment variables, the `AgentEvals` SDK wraps all OTel boilerplate into a context manager:

```python
from agentevals import AgentEvals

app = AgentEvals()

with app.session(eval_set_id="my-eval"):
    result = my_agent.invoke("Hello!")
```

Works with LangChain, Strands, Google ADK, and any OTel-instrumented agent. For frameworks that create their own `TracerProvider` (like Strands), pass it explicitly:

```python
telemetry = StrandsTelemetry()

with app.session(eval_set_id="strands-eval", tracer_provider=telemetry.tracer_provider):
    agent("Roll a die")
```

For simple prompt-to-response agents, there's also a decorator shorthand:

```python
app = AgentEvals(eval_set_id="my-eval")


@app.agent
def my_agent(prompt):
    return llm.invoke(prompt).content


app.run(["Hello!", "Tell me a joke"])
```

To skip streaming when the dev server isn't running, set `streaming=False`:

```python
app = AgentEvals(streaming=os.getenv("AGENTEVALS_STREAM", "1") == "1")
```

When disabled, `session()` and `session_async()` do nothing and your agent runs without exporting anything to agentevals.

See [sdk_example/](./sdk_example/) for complete working examples.

## What agentevals reads

Standard OpenTelemetry GenAI telemetry. Message content can be on span attributes, span events or log events; agentevals reads all three. Google ADK's own `gcp.vertex.agent.*` attributes are read as well. Per framework settings (content capture variables and so on) are in [docs/otel-compatibility.md](../docs/otel-compatibility.md#producer-setup).

## Example Agents

| Example | Framework | LLM Provider | How it sends telemetry |
|---------|-----------|-------------|------------------------|
| [zero-code-examples/langchain/](./zero-code-examples/langchain/) | LangChain | OpenAI | Standard OTLP export (traces and logs) |
| [zero-code-examples/ollama/](./zero-code-examples/ollama/) | LangChain | Ollama | Standard OTLP export (traces and logs) |
| [zero-code-examples/strands/](./zero-code-examples/strands/) | Strands | OpenAI | Standard OTLP export |
| [zero-code-examples/adk/](./zero-code-examples/adk/) | Google ADK | Gemini | Standard OTLP export |
| [zero-code-examples/openai-agents/](./zero-code-examples/openai-agents/) | OpenAI Agents SDK | OpenAI | Standard OTLP export |
| [zero-code-examples/pydantic-ai/](./zero-code-examples/pydantic-ai/) | Pydantic AI | OpenAI | Standard OTLP export |
| [langchain_agent](./langchain_agent/) | LangChain | OpenAI | AgentEvals SDK session |
| [strands_agent](./strands_agent/) | Strands | OpenAI | AgentEvals SDK session |
| [dice_agent](./dice_agent/) | Google ADK | Gemini | AgentEvals SDK session |

All of them implement the same toy agent (dice rolling and prime checking), so you can compare the approaches directly.

## Kubernetes

| Example | Description |
|---------|-------------|
| [kubernetes/](./kubernetes/) | Deploy agentevals with kagent on Kubernetes using native OTLP gRPC ingestion (or optionally an OTel Collector). Includes a walkthrough for comparing two kagent agents (different models) and evaluating them with tool trajectory and response match scores. |

## Custom result sinks

Plugins can deliver run results (partial metrics, final summary, errors) to arbitrary backends alongside the database. Install a package that declares `[project.entry-points."agentevals.sinks"]`, restart agentevals, then reference the plugin's `kind` in `spec.sinks` on `POST /api/runs`.

See [custom_sink/README.md](./custom_sink/README.md) for a minimal setuptools plugin and configuration examples.

## Running the Examples

```bash
agentevals serve --dev                 # terminal 1
cd ui && npm run dev                   # terminal 2, then open http://localhost:5173

# terminal 3, any of:
python examples/zero-code-examples/langchain/run.py
python examples/zero-code-examples/adk/run.py
python examples/sdk_example/context_manager_example.py
python examples/dice_agent/main.py
```

Each zero code example sets its own `agentevals.session_name` and a per process `service.instance.id`, so running it twice gives two sessions. The framework SDKs come from the `e2e` dependency group: `uv sync --all-extras --group e2e`.

See [docs/otel-compatibility.md](../docs/otel-compatibility.md#sessions) for how sessions work and how to evaluate them.
