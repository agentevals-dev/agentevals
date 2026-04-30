# Live Streaming Dev Server

The live streaming dev server enables real-time trace evaluation for agent development. Agents stream OpenTelemetry spans via WebSocket as they execute, and results appear instantly in both terminal and browser.

## Quick Start

### 1. Start the dev server

```bash
agentevals serve --dev --port 8001
```

This starts:
- WebSocket server at `ws://localhost:8001/ws/traces`
- API at `http://localhost:8001/api`
- SSE endpoint at `http://localhost:8001/stream/ui-updates`

### 2. Start the UI (in another terminal)

```bash
cd agentevals/ui
npm run dev
```

Navigate to `http://localhost:5173` and select "I am developing an agent".

### 3. Enable streaming in your agent code

The recommended approach uses the async `enable_streaming` context manager:

```python
from agentevals.streaming import enable_streaming

async with enable_streaming(
    ws_url="ws://localhost:8001/ws/traces",
    eval_set_id="my-eval-set",
) as session_id:
    # Your agent code runs normally — all OTel spans stream automatically
    agent = Agent(name="my-agent")
    result = agent.invoke("Do something")
```

For more control over the streaming lifecycle, use `AgentEvalsStreamingProcessor` directly. See [examples/README.md](../examples/README.md) for instrumentation patterns tailored to different frameworks (LangChain, Strands, Google ADK).

### 4. See results in real-time

- **Terminal:** Session status prints when agent finishes
- **Browser:** Live span tree builds as agent executes
- **Evaluation:** Triggered from the UI after session completes

## Architecture

```
Agent (any OTel-instrumented framework)
    ↓ WebSocket (OTLP/JSON spans + logs)
agentevals dev server
    ↓ SSE (real-time updates)
Browser UI
```

Agents emit OTel spans (and optionally logs for GenAI message content). The dev server receives them over WebSocket, incrementally extracts invocations, and pushes updates to the browser via Server-Sent Events.

See [examples/README.md](../examples/README.md) for details on supported instrumentation approaches (OTel GenAI semantic conventions, Google ADK, etc.).

## Key Features

### OTLP/JSON Support

Native OpenTelemetry format. The CLI auto-detects Jaeger vs OTLP from
file contents, so `.json` and `.jsonl` exports from Tempo, Jaeger, or
the OTel collector all work without a `--format` flag:

```bash
# Load any trace file directly; format is auto-detected
agentevals run trace.otlp.json --eval-set eval.json
```

Pass `--format otlp-json` (or `jaeger-json`) only as an override when
auto-detection fails on a non-standard export.

### Real-time Span Streaming

The `AgentEvalsStreamingProcessor` is an OTel `SpanProcessor` that streams spans over WebSocket as they complete:

```python
from agentevals.streaming.processor import AgentEvalsStreamingProcessor

processor = AgentEvalsStreamingProcessor(
    ws_url="ws://localhost:8001/ws/traces",
    session_id="my-session",
    trace_id="abc123",
)
await processor.connect(eval_set_id="my-eval")

# Register with your TracerProvider
tracer_provider.add_span_processor(processor)

# When done:
await processor.shutdown_async()
```

For GenAI agents that emit message content as OTel Logs, use `AgentEvalsLogStreamingProcessor` alongside it — see the [langchain_agent example](../examples/langchain_agent/).

### Session Lifecycle

When a session ends (`session_end` message), the server:
1. Enriches spans with log-based message content (if any)
2. Extracts invocations (user/agent messages, tool calls, model info)
3. Broadcasts `session_complete` to the browser UI via SSE
4. Sends `session_complete` back to the agent via WebSocket

Evaluation is triggered separately from the UI or API.

## GenAI Message Content: Span Events vs. Logs

agentevals supports two mechanisms for receiving GenAI message content (`gen_ai.input.messages`, `gen_ai.output.messages`):

**Log records (recommended).** Instrumentation libraries like `opentelemetry-instrumentation-openai-v2` emit message content as OTel log records correlated with spans via trace context. The server merges these back into spans during session completion (see `log_enrichment.py`).

**Span events (legacy, supported for backward compatibility).** Some frameworks (notably Strands with `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`) currently emit message content as span event attributes. The `AgentEvalsStreamingProcessor` promotes these attributes to span-level attributes so downstream converters see a uniform shape. This promotion happens in three places:

- `streaming/processor.py` for live WebSocket spans
- `api/otlp_routes.py` for OTLP HTTP reception
- `loader/otlp.py` for loading OTLP JSON files

The OTel community is [deprecating span events](https://opentelemetry.io/blog/2026/deprecating-span-events/) in favor of log-based events emitted via the Logs API. As frameworks migrate, the log-based path will become the standard. The span event promotion logic will remain for backward compatibility with older instrumentation versions.

For a full overview of supported OTel conventions and migration guidance, see [otel-compatibility.md](./otel-compatibility.md).

## WebSocket Protocol

### Endpoint: `/ws/traces`

#### Agent → Server Messages

**Session start:**
```json
{
  "type": "session_start",
  "session_id": "session-abc123",
  "trace_id": "3e289017...",
  "eval_set_id": "my-eval",
  "metadata": {}
}
```

**Span (OTLP/JSON format):**
```json
{
  "type": "span",
  "session_id": "session-abc123",
  "span": {
    "traceId": "3e289017...",
    "spanId": "1f9762ca...",
    "name": "chat gpt-4o",
    "startTimeUnixNano": "1771237534577907000",
    "endTimeUnixNano": "1771237535012345000",
    "attributes": [
      {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}}
    ]
  }
}
```

**Log (GenAI message content):**
```json
{
  "type": "log",
  "session_id": "session-abc123",
  "log": {
    "traceId": "3e289017...",
    "spanId": "1f9762ca...",
    "body": {"stringValue": "gen_ai.user.message"},
    "attributes": [
      {"key": "gen_ai.user.message", "value": {"stringValue": "{\"role\": \"user\", \"content\": \"Hello\"}"}}
    ]
  }
}
```

**Session end:**
```json
{
  "type": "session_end",
  "session_id": "session-abc123"
}
```

#### Server → Agent Messages

**Session complete (with extracted invocations):**
```json
{
  "type": "session_complete",
  "invocations": [
    {
      "invocationId": "inv-1",
      "userText": "Roll a 20-sided die",
      "agentText": "I rolled a 20-sided die and got 13",
      "toolCalls": [{"name": "roll_die", "args": {"sides": 20}}],
      "modelInfo": {"model": "gpt-4o"}
    }
  ]
}
```

**Error (limit exceeded):**
```json
{
  "type": "error",
  "message": "Session has reached maximum span limit (10000)"
}
```

### SSE: `/stream/ui-updates`

Real-time events for the browser UI:

```javascript
const eventSource = new EventSource('http://localhost:8001/stream/ui-updates');

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'session_started':
      // New session with metadata
      break;
    case 'span_received':
      // New span — build trace tree incrementally
      break;
    case 'session_complete':
      // Session ended — invocations extracted
      break;
  }
};
```

## Session Management

- Sessions are stored **in-memory only** (no database)
- Completed sessions expire after **2 hours** (configurable)
- Maximum **100 sessions** kept at a time
- Per-session limits: **10,000 spans** and **5,000 logs**

## CLI Options

```bash
agentevals serve --dev \
  --port 8001 \
  --host 0.0.0.0 \
  --eval-sets ./eval_sets/ \
  --headless \
  -v
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dev` | off | Enable WebSocket and live streaming |
| `--port`, `-p` | 8001 | Server port |
| `--host` | 0.0.0.0 | Host to bind |
| `--eval-sets` | — | Directory with eval set JSON files to pre-load |
| `--headless` | off | Run without browser (WebSocket only) |
| `-v`, `--verbose` | off | Increase verbosity (`-v` for INFO, `-vv` for DEBUG) |

## Development Workflow

```bash
# Terminal 1: Start dev server
agentevals serve --dev --port 8001

# Terminal 2: Start UI
cd ui && npm run dev

# Terminal 3: Run an example agent
python examples/dice_agent/main.py      # Google ADK
python examples/langchain_agent/main.py  # LangChain + GenAI semconv
python examples/strands_agent/main.py    # Strands + GenAI semconv
```

**Result:**
- Agent executes normally
- Spans (and logs) stream to server in real-time
- UI shows live trace tree building with incremental invocation extraction
- Evaluation can be triggered from the UI after session completes
- Compare multiple sessions side-by-side

See [examples/README.md](../examples/README.md) for instrumentation setup for each framework.

## Dependencies

Streaming support requires the `streaming` extras:

```bash
pip install "agentevals[streaming]"
```

This installs `opentelemetry-sdk>=1.20.0`. Agent code also needs `websockets` for the WebSocket connection.

## Key Files

### Backend
- `src/agentevals/streaming/processor.py` — OTel `SpanProcessor` + `LogProcessor` for WebSocket streaming
- `src/agentevals/streaming/ws_server.py` — WebSocket handler + session management (`StreamingTraceManager`)
- `src/agentevals/streaming/session.py` — Session tracking (`TraceSession`)
- `src/agentevals/streaming/incremental_processor.py` — Incremental invocation extraction from spans/logs
- `src/agentevals/streaming/__init__.py` — `enable_streaming()` convenience function
- `src/agentevals/loader/otlp.py` — OTLP/JSON trace loader
- `src/agentevals/utils/log_enrichment.py` — Merges GenAI log content back into spans

### Frontend
- `ui/src/components/streaming/LiveStreamingView.tsx` — Live sessions UI
- `ui/src/components/streaming/SessionCard.tsx` — Individual session display
- `ui/src/components/streaming/LiveConversationPanel.tsx` — Real-time conversation view

## Compatibility

All existing workflows continue to work:
- Trace files (Jaeger or OTLP, including Tempo exports) auto-detect by
  content: `agentevals run trace.json --eval-set ...`
- Pass `--format` only to override detection on non-standard exports.
- Web UI upload flow unchanged.
