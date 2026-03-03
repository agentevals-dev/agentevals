# LangChain Agent Example with Live Evaluation

This example demonstrates streaming traces and logs from a LangChain agent to agentevals for real-time evaluation using **OpenTelemetry GenAI semantic conventions**.

## What This Does

A LangChain agent with:
- OpenAI GPT models via LangChain
- Dice rolling and prime number checking tools
- Real-time trace streaming to agentevals dev server
- Live conversation display and evaluation

**Framework-Agnostic:** Uses standard OpenTelemetry GenAI semantic conventions that work with any framework (LangChain, LlamaIndex, Haystack, etc.).

## Quick Start

### 1. Install Dependencies

```bash
cd /path/to/agentevals
pip install -e .
pip install -r examples/langchain_agent/requirements.txt
```

### 2. Set API Key

```bash
export OPENAI_API_KEY="sk-..."
```

### 3. Run the System

**Terminal 1 - Dev Server:**
```bash
agentevals serve --dev --port 8001
```

**Terminal 2 - UI (Optional):**
```bash
cd ui && npm run dev
# Open http://localhost:5173
```

**Terminal 3 - Run Agent:**
```bash
python examples/langchain_agent/main.py
```

## How It Works

### Integration Setup

The key setup function in `main.py`:

```python
def setup_otel_streaming(ws_url: str, session_id: str, eval_set_id: str | None = None):
    # 1. Enable message content capture (CRITICAL!)
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    # 2. Create TracerProvider (for spans - metadata)
    tracer_provider = TracerProvider()
    trace.set_tracer_provider(tracer_provider)

    # 3. Create LoggerProvider (for logs - message content)
    logger_provider = LoggerProvider()
    set_logger_provider(logger_provider)

    # 4. Create streaming processor with background event loop
    processor = AgentEvalsStreamingProcessor(...)
    # ... connect to server ...

    # 5. Add span processor
    tracer_provider.add_span_processor(processor)

    # 6. Add log processor (shares same WebSocket connection)
    log_processor = AgentEvalsLogStreamingProcessor(processor)
    logger_provider.add_log_record_processor(log_processor)

    # 7. Instrument OpenAI SDK
    OpenAIInstrumentor().instrument()
```

### Why Both Spans AND Logs?

OpenTelemetry GenAI instrumentation captures two types of data:

**Spans (Metadata):**
- `gen_ai.request.model` - Model name
- `gen_ai.usage.input_tokens` - Token counts
- `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons`

**Logs (Content):**
- `gen_ai.user.message` - User input text
- `gen_ai.choice` - Agent response text
- `gen_ai.assistant.message` - Tool calls

Message content is stored in logs (not span attributes) for privacy, security, and size considerations.

### Data Flow

```
LangChain Agent
  ↓
OpenAI SDK (instrumented)
  ↓
Spans (metadata) + Logs (message content)
  ↓
AgentEvalsStreamingProcessor + AgentEvalsLogStreamingProcessor
  ↓
WebSocket → agentevals dev server
  ↓
IncrementalInvocationExtractor
  ├─ Processes spans → token updates
  └─ Processes logs → user/agent messages, tool calls
      ↓
Real-time UI via SSE
```

## Files

- **`main.py`** - Entry point with streaming setup
  - `setup_otel_streaming()` - Configures OTEL for streaming
  - Shows complete integration pattern

- **`agent.py`** - LangChain agent with tools
  - `roll_die(sides)` - Dice rolling tool
  - `check_prime(nums)` - Prime checking tool

- **`eval_set.json`** - Golden evaluation cases
- **`test_streaming.py`** - Connection test
- **`requirements.txt`** - Dependencies

## Critical Configuration

### 1. Enable Message Content Capture

```python
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
```

**Without this:** Only metadata (tokens, model name) is captured. No conversation text appears in UI.

### 2. Create Both Providers

```python
tracer_provider = TracerProvider()  # For spans
logger_provider = LoggerProvider()   # For logs
```

**Both are required** - spans provide metadata, logs provide message content.

### 3. Add Both Processors

```python
tracer_provider.add_span_processor(processor)
logger_provider.add_log_record_processor(log_processor)
```

The log processor shares the WebSocket connection from the span processor.

### 4. Background Event Loop

```python
loop = asyncio.new_event_loop()

def run_loop_in_background():
    asyncio.set_event_loop(loop)
    loop.run_forever()

thread = threading.Thread(target=run_loop_in_background, daemon=True)
thread.start()
```

**Required** because OTEL processors run in sync context but need async WebSocket connection.

## Adapting to Your Agent

To add agentevals streaming to your LangChain agent:

1. Copy the `setup_otel_streaming()` function
2. Call it before your agent runs
3. Ensure these dependencies are installed:
   - `opentelemetry-sdk`
   - `opentelemetry-instrumentation-openai-v2`
   - `websockets`

```python
from agentevals.streaming.processor import (
    AgentEvalsStreamingProcessor,
    AgentEvalsLogStreamingProcessor,
)

# Your existing agent code
llm = ChatOpenAI(model="gpt-4")

# Add agentevals streaming
setup_otel_streaming(
    ws_url="ws://localhost:8001/ws/traces",
    session_id=f"my-agent-{datetime.now().isoformat()}",
    eval_set_id="my_eval_set",
)

# Run your agent as normal
response = llm.invoke(...)
```

## Troubleshooting

**"No conversation text in UI (only tokens)"**
- Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`
- Check that LoggerProvider and log processor are configured

**"Connection refused"**
- Ensure dev server is running: `agentevals serve --dev --port 8001`

**"Duplicate messages in UI"**
- Fixed in incremental processor - uses deduplication by content hash

**"No traces appearing"**
- Run `test_streaming.py` to verify WebSocket connectivity
- Check dev server logs for connection messages

## Extending to Other Frameworks

This pattern works with any framework using GenAI semantic conventions:

- **LlamaIndex** - Enable OTEL, use same processors
- **Haystack** - Enable OTEL instrumentation
- **Custom agents** - Use `opentelemetry-instrumentation-openai-v2`

