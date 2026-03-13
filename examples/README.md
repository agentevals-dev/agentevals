# Instrumenting Agents for agentevals

agentevals evaluates AI agents by consuming their [OpenTelemetry](https://opentelemetry.io/) traces. Any agent that emits OTel spans can be evaluated — you just need a `TracerProvider` with the `AgentEvalsStreamingProcessor` registered.

This guide covers the instrumentation patterns agentevals supports, with a recommendation for new projects. Each example in this directory is a working agent you can run and modify.

## SDK (Quick Start)

The `AgentEvals` SDK wraps all OTel boilerplate into a single context manager. Use this for the simplest setup:

```python
from agentevals import AgentEvals

app = AgentEvals()

with app.session(eval_set_id="my-eval"):
    # Your agent code here — any framework, unchanged
    result = my_agent.invoke("Hello!")
```

Works with LangChain, Strands, Google ADK, and any OTel-instrumented agent. For frameworks that create their own `TracerProvider` (like Strands), pass it explicitly:

```python
telemetry = StrandsTelemetry()

with app.session(eval_set_id="strands-eval", tracer_provider=telemetry.tracer_provider):
    agent("Roll a die")
```

For simple prompt→response agents, there's also a decorator shorthand:

```python
app = AgentEvals(eval_set_id="my-eval")

@app.agent
def my_agent(prompt):
    return llm.invoke(prompt).content

app.run(["Hello!", "Tell me a joke"])
```

To keep the SDK wired up in your code but skip streaming when the dev server isn't running, set `streaming=False`:

```python
app = AgentEvals(streaming=os.getenv("AGENTEVALS_STREAM", "1") == "1")
```

When disabled, `session()` and `session_async()` become no-ops — your agent code runs normally without any WebSocket connection, OTel setup, or background threads.

See [sdk_example/](./sdk_example/) for complete working examples.

## Advanced: Manual OTel Setup

> [!TIP]
> **Prefer OTel GenAI semantic conventions** for new agents. They are framework-agnostic,
> interoperable across observability tools, and benefit from the growing OTel ecosystem.

## Supported Instrumentation Approaches

agentevals supports two categories of trace instrumentation:

- **OTel GenAI Semantic Conventions (recommended)** — Standard `gen_ai.*` span attributes defined by the [OpenTelemetry GenAI working group](https://opentelemetry.io/docs/specs/semconv/gen-ai/). Framework-agnostic and interoperable. Works with LangChain, LlamaIndex, Haystack, Strands, and any framework that supports the conventions.

- **Framework-Native OTel Tracing** — Some frameworks (like Google ADK) emit their own proprietary span attributes. agentevals has dedicated converters for these formats.

Trace format is **auto-detected** — agents don't need to declare which format they use. The detection checks for `gen_ai.request.model` / `gen_ai.input.messages` (GenAI semconv) or `otel.scope.name == "gcp.vertex.agent"` (ADK).

## Example Agents

| Example | Framework | LLM Provider | Instrumentation | Content Delivery | Key Env Vars |
|---------|-----------|-------------|-----------------|-----------------|--------------|
| [langchain_agent](./langchain_agent/) | LangChain | OpenAI | GenAI semconv (logs) | `LoggerProvider` logs | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` |
| [strands_agent](./strands_agent/) | Strands | OpenAI | GenAI semconv (events) | Span events (auto-promoted) | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` |
| [dice_agent](./dice_agent/) | Google ADK | Gemini | ADK built-in | Span attributes (proprietary) | — |

All three examples implement the same toy agent (dice rolling + prime checking) so you can compare instrumentation patterns directly.

## GenAI Semantic Convention Patterns

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

Without `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, only metadata is captured — no conversation text will appear.

See [langchain_agent/README.md](./langchain_agent/README.md) for the full walkthrough.

### Events-Based Content ([strands_agent](./strands_agent/))

Used by frameworks that emit message content as **span events** rather than separate log records. The `AgentEvalsStreamingProcessor` automatically promotes `gen_ai.input.messages` and `gen_ai.output.messages` from event attributes to span attributes, so downstream processing sees a uniform shape.

This pattern needs only a `TracerProvider` — no `LoggerProvider` or log processor:

```python
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"

telemetry = StrandsTelemetry()  # creates TracerProvider internally
processor = AgentEvalsStreamingProcessor(ws_url=..., session_id=..., trace_id=...)
telemetry.tracer_provider.add_span_processor(processor)
```

This is the simplest GenAI semconv integration — one provider, one processor, one env var.

### Which Pattern Should I Use?

- **Check your framework/library docs first.** They will tell you whether message content is emitted as logs or span events.
- If your instrumentation library requires a `LoggerProvider` (like `opentelemetry-instrumentation-openai-v2`), use the **logs-based** pattern.
- If your framework emits GenAI span events (like Strands with `StrandsTelemetry`), use the **events-based** pattern — it's simpler.
- If you're using **Google ADK**, skip GenAI semconv entirely — see the next section.

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

## Common Setup

All examples share the same core pattern:

1. **Create** (or obtain) a `TracerProvider`
2. **Create** an `AgentEvalsStreamingProcessor` with WebSocket URL, session ID, and trace ID
3. **Connect** to the dev server: `await processor.connect(eval_set_id=...)`
4. **Register** the processor: `provider.add_span_processor(processor)`
5. **Run** your agent normally — all OTel spans are streamed automatically
6. **Shutdown**: `await processor.shutdown_async()`

### Background Event Loop

Both `langchain_agent` and `strands_agent` use a background thread for the async WebSocket connection since OTel span processors run synchronously:

```python
loop = asyncio.new_event_loop()
thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
thread.start()

# Use asyncio.run_coroutine_threadsafe(coro, loop) for async calls
```

## Running the Examples

### 1. Start the Dev Server

```bash
agentevals serve --dev --port 8001
```

### 2. Start the UI (optional)

```bash
cd ui && npm run dev
# Open http://localhost:5173, select "I am developing an agent"
```

### 3. Run an Example Agent

```bash
# SDK examples (recommended starting point):
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
- [dice_agent/README.md](./dice_agent/README.md) — Google ADK + Gemini
- [langchain_agent/README.md](./langchain_agent/README.md) — LangChain + OpenAI
- [strands_agent/](./strands_agent/) — Strands + OpenAI

For details on the WebSocket streaming protocol, see [docs/streaming.md](../docs/streaming.md).
