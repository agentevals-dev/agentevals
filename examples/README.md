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

When disabled, `session()` and `session_async()` become no-ops and your agent runs normally without any WebSocket connection or OTel setup.

Requires the `[streaming]` extra: `pip install "agentevals[streaming]"`. See [sdk_example/](./sdk_example/) for complete working examples.

## Supported Instrumentation Formats

Trace format is **auto-detected**. Agents don't need to declare which format they use.

- **OTel GenAI Semantic Conventions** (recommended for new agents). Standard `gen_ai.*` span attributes defined by the [OpenTelemetry GenAI working group](https://opentelemetry.io/docs/specs/semconv/gen-ai/). Framework-agnostic and interoperable. Works with LangChain, Strands, and any framework that supports the conventions.

- **Framework-Native OTel Tracing**. Some frameworks (like Google ADK) emit their own proprietary span attributes. agentevals has dedicated converters for these formats.

Detection checks for `gen_ai.request.model` / `gen_ai.input.messages` (GenAI semconv) or `otel.scope.name == "gcp.vertex.agent"` (ADK).

## Example Agents

| Example | Framework | LLM Provider | Instrumentation | Content Delivery |
|---------|-----------|-------------|-----------------|-----------------|
| [zero-code-examples/langchain/](./zero-code-examples/langchain/) | LangChain | OpenAI | GenAI semconv (logs) | Standard OTLP export |
| [zero-code-examples/ollama/](./zero-code-examples/ollama/) | LangChain | Ollama | GenAI semconv (logs) | Standard OTLP export |
| [zero-code-examples/strands/](./zero-code-examples/strands/) | Strands | OpenAI | GenAI semconv (events*) | Standard OTLP export |
| [zero-code-examples/adk/](./zero-code-examples/adk/) | Google ADK | Gemini | ADK built-in | Standard OTLP export |
| [langchain_agent](./langchain_agent/) | LangChain | OpenAI | GenAI semconv (logs) | SDK WebSocket |
| [strands_agent](./strands_agent/) | Strands | OpenAI | GenAI semconv (events*) | SDK WebSocket |
| [dice_agent](./dice_agent/) | Google ADK | Gemini | ADK built-in | SDK WebSocket |

*\*Span events are [being deprecated](https://opentelemetry.io/blog/2026/deprecating-span-events/) in favor of log-based events. agentevals supports both. See [docs/otel-compatibility.md](../docs/otel-compatibility.md) for details.*

The zero-code and SDK examples implement the same toy agent (dice rolling + prime checking) so you can compare the two approaches directly.

## Kubernetes

| Example | Description |
|---------|-------------|
| [kubernetes/](./kubernetes/) | Deploy agentevals with kagent on Kubernetes using native OTLP gRPC ingestion (or optionally an OTel Collector). Includes a walkthrough for comparing two kagent agents (different models) and evaluating them with tool trajectory and response match scores. |

## Advanced: GenAI Semantic Convention Patterns

> [!TIP]
> The sections below apply to the **SDK WebSocket** examples (`langchain_agent`, `strands_agent`, `dice_agent`).
> For the zero-code OTLP examples, none of this manual wiring is needed.

The OTel GenAI semantic conventions define _what_ data is captured (`gen_ai.request.model`, `gen_ai.input.messages`, `gen_ai.output.messages`, token counts, etc.) but allow flexibility in _how_ message content is delivered. agentevals supports both approaches:

### Logs-Based Content ([langchain_agent](./langchain_agent/))

Used by auto-instrumentation libraries like [`opentelemetry-instrumentation-openai-v2`](https://pypi.org/project/opentelemetry-instrumentation-openai-v2/). Spans carry metadata (model, tokens, finish reasons), while message content is emitted as separate OTel Log Records.

This pattern requires **both** a `TracerProvider` and a `LoggerProvider`, with matching processors:

```python
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

tracer_provider = TracerProvider()
logger_provider = LoggerProvider()

processor = AgentEvalsStreamingProcessor(ws_url=..., session_id=..., trace_id=...)
tracer_provider.add_span_processor(processor)

log_processor = AgentEvalsLogStreamingProcessor(processor)  # shares WebSocket connection
logger_provider.add_log_record_processor(log_processor)

OpenAIInstrumentor().instrument()  # auto-instruments the OpenAI SDK
```

Without `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, only metadata is captured and no conversation text will appear.

See [langchain_agent/README.md](./langchain_agent/README.md) for the full walkthrough.

### Events-Based Content ([strands_agent](./strands_agent/))

> [!NOTE]
> The OTel community is [deprecating span events](https://opentelemetry.io/blog/2026/deprecating-span-events/) in favor of log-based events emitted via the Logs API. Frameworks currently using span events (like Strands) are expected to migrate to log-based events in future versions. agentevals supports both patterns and will continue to handle span events for backward compatibility.

Used by frameworks that emit message content as **span events** rather than separate log records. The `AgentEvalsStreamingProcessor` automatically promotes `gen_ai.input.messages` and `gen_ai.output.messages` from event attributes to span attributes, so downstream processing sees a uniform shape.

This pattern needs only a `TracerProvider`, no `LoggerProvider` or log processor:

```python
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"

telemetry = StrandsTelemetry()  # creates TracerProvider internally
processor = AgentEvalsStreamingProcessor(ws_url=..., session_id=..., trace_id=...)
telemetry.tracer_provider.add_span_processor(processor)
```

### Which Pattern Should I Use?

- **For new instrumentation, prefer the logs-based pattern.** The OTel community recommends emitting events as log records rather than span events going forward.
- **Check your framework/library docs first.** They will tell you whether message content is emitted as logs or span events.
- If your instrumentation library requires a `LoggerProvider` (like `opentelemetry-instrumentation-openai-v2`), use the **logs-based** pattern.
- If your framework currently emits GenAI span events (like Strands with `StrandsTelemetry`), the **events-based** pattern works today. When the framework migrates to log-based events, switch to the logs-based pattern.
- If you're using **Google ADK**, skip GenAI semconv entirely. See the next section.

For a detailed overview of OTel compatibility and the ongoing migration, see [docs/otel-compatibility.md](../docs/otel-compatibility.md).

## Framework-Native Tracing (Google ADK)

Google ADK instruments agents automatically under the `gcp.vertex.agent` OTel scope. It emits proprietary attributes (`gcp.vertex.agent.llm_request`, `gcp.vertex.agent.llm_response`, etc.) directly on spans. agentevals has a dedicated converter for this format.

No GenAI semconv environment variables or log providers are needed:

```python
provider = TracerProvider()
trace.set_tracer_provider(provider)

processor = AgentEvalsStreamingProcessor(ws_url=..., session_id=..., trace_id=...)
provider.add_span_processor(processor)
# ADK agents automatically emit spans through the global TracerProvider
```

See [dice_agent/README.md](./dice_agent/README.md) for a complete example.

## Running the Examples

### 1. Start the Dev Server

```bash
agentevals serve --dev
```

### 2. Start the UI (optional)

```bash
cd ui && npm run dev
# Open http://localhost:5173, select "I am developing an agent"
```

### 3. Run an Example Agent

```bash
# Zero-code OTLP (recommended):
python examples/zero-code-examples/langchain/run.py
python examples/zero-code-examples/ollama/run.py
python examples/zero-code-examples/strands/run.py
python examples/zero-code-examples/adk/run.py

# SDK examples:
python examples/sdk_example/context_manager_example.py
python examples/sdk_example/decorator_example.py
python examples/sdk_example/async_example.py

# Manual OTel setup examples:
python examples/dice_agent/main.py
python examples/langchain_agent/main.py
python examples/strands_agent/main.py
```

Traces stream to the dev server in real-time. Evaluation runs automatically when the session completes.

See each example's README for prerequisites and detailed instructions:
- [zero-code-examples/](./zero-code-examples/) (LangChain + Strands, standard OTLP)
- [dice_agent/README.md](./dice_agent/README.md) (Google ADK + Gemini)
- [langchain_agent/README.md](./langchain_agent/README.md) (LangChain + OpenAI, SDK)
- [strands_agent/](./strands_agent/) (Strands + OpenAI, SDK)

For details on the WebSocket streaming protocol, see [docs/streaming.md](../docs/streaming.md).
