# OpenTelemetry Compatibility

agentevals consumes OpenTelemetry traces to evaluate AI agents. This document covers which OTel conventions we support, how we handle the ongoing migration from span events to log-based events, and guidance for instrumenting your own agents.

## Supported Semantic Conventions

### OTel GenAI Semantic Conventions (recommended)

The [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) define standard span attributes for LLM interactions. agentevals auto-detects this format when spans contain `gen_ai.request.model` or `gen_ai.input.messages`.

This format works with LangChain, Strands, OpenAI instrumentation, Anthropic instrumentation, and any framework that follows the GenAI semantic conventions.

#### Core attributes

| Attribute | Description |
|-----------|-------------|
| `gen_ai.request.model` | Model name (e.g. `gpt-4o`, `claude-sonnet-4-6`) |
| `gen_ai.input.messages` | JSON array of input messages |
| `gen_ai.output.messages` | JSON array of output messages |
| `gen_ai.response.finish_reasons` | Why the model stopped generating |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |

#### Provider and response metadata (v1.37.0+)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.provider.name` | LLM provider (e.g. `openai`, `anthropic`). Replaces the deprecated `gen_ai.system`. |
| `gen_ai.response.model` | Model name returned in the response |
| `gen_ai.response.id` | Unique response identifier |

#### Request parameters (v1.40.0)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.request.temperature` | Temperature sampling parameter |
| `gen_ai.request.max_tokens` | Maximum output tokens limit |
| `gen_ai.request.top_p` | Top-P (nucleus) sampling parameter |
| `gen_ai.request.top_k` | Top-K sampling parameter |

#### Cache token usage

| Attribute | Description |
|-----------|-------------|
| `gen_ai.usage.cache_creation.input_tokens` | Tokens spent creating a prompt cache entry |
| `gen_ai.usage.cache_read.input_tokens` | Tokens served from an existing cache entry |

These are relevant for providers that support prompt caching (Anthropic, OpenAI). agentevals aggregates these across LLM spans and displays them in the performance summary.

#### Agent and tool metadata (v1.31.0+)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.agent.id` | Unique agent identifier |
| `gen_ai.agent.description` | Agent description |
| `gen_ai.tool.description` | Tool description |
| `gen_ai.tool.type` | Tool type classification |

#### Opt-in attributes (v1.37.0+)

These may contain large payloads and are typically gated behind instrumentation flags:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.system_instructions` | System prompt text |
| `gen_ai.tool.definitions` | Tool schema definitions (JSON) |
| `gen_ai.output.type` | Classification of output content |

### Google ADK (framework-native)

Google ADK emits spans under the `gcp.vertex.agent` OTel scope with proprietary attributes (`gcp.vertex.agent.llm_request`, `gcp.vertex.agent.llm_response`, etc.). agentevals has a dedicated converter that auto-detects this format. No GenAI semconv configuration is needed.

### Format Detection

Format detection is automatic. When a trace contains both ADK and GenAI attributes, ADK takes priority because it provides richer structured data. The detection logic lives in `src/agentevals/converter.py` (`get_extractor()`).

## Message Formats

GenAI message content (`gen_ai.input.messages`, `gen_ai.output.messages`) can use two JSON schemas. agentevals supports both and normalizes them internally.

### Content-based format

Used by OpenAI and LangChain instrumentors (v2):

```json
{"role": "user", "content": "Hello"}
{"role": "assistant", "content": "...", "tool_calls": [{"type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"NYC\"}"}}]}
```

### Parts-based format (v1.36.0+)

Used by newer instrumentors that follow the GenAI semconv parts schema:

```json
{"role": "user", "parts": [{"type": "text", "content": "Hello"}]}
{"role": "assistant", "parts": [{"type": "tool_call", "name": "get_weather", "arguments": {"city": "NYC"}}]}
```

Both formats are auto-detected per message. Tool calls are normalized to `{name, id, arguments}` regardless of source format.

## Message Content Delivery

GenAI message content can arrive through three mechanisms. agentevals supports all of them:

### 1. Span attributes (simplest)

Message content is stored directly as span attributes. This is the most straightforward approach and requires no special handling.

### 2. Log records (recommended for new instrumentation)

Message content is emitted as OTel log records correlated with spans via trace context. This is the pattern used by `opentelemetry-instrumentation-openai-v2` and LangChain's GenAI instrumentation.

Requires both `OTLPSpanExporter` and `OTLPLogExporter` (or their streaming equivalents). Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` to enable content capture.

### 3. Span events (deprecated, supported for backward compatibility)

Message content is emitted as attributes on span events. agentevals promotes these to span-level attributes during normalization so downstream processing sees a uniform shape.

This promotion happens in three processing layers:
- `streaming/processor.py` for live WebSocket spans
- `api/otlp_routes.py` for OTLP HTTP reception
- `loader/otlp.py` for loading OTLP JSON files

## Span Events Deprecation

The OTel community is [deprecating the Span Event API](https://opentelemetry.io/blog/2026/deprecating-span-events/) (`Span.AddEvent`, `Span.RecordException`) in favor of emitting events as log records via the Logs API. The core idea: "events are logs with names," correlated with traces through context.

### What this means for agentevals users

**No immediate action required.** Existing instrumentation continues to work. The deprecation is about providing a single recommended path for new code, not about removing support for existing span event data.

**For new instrumentation**, prefer the logs-based pattern. Configure both `OTLPSpanExporter` and `OTLPLogExporter`, and use instrumentation libraries that emit message content as log records.

**For existing span-event instrumentation** (e.g. Strands with `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`), everything continues to work. When your framework releases a version that migrates to log-based events, update your exporter configuration to include `OTLPLogExporter` and follow the logs-based pattern.

### What this means for agentevals internals

agentevals already supports both content delivery mechanisms. The span event promotion logic will remain for backward compatibility with older instrumentation versions. As frameworks migrate, the log-based path (already fully supported) will become the primary path.

### Migration checklist for framework authors

If you maintain an OTel-instrumented agent framework and want to align with the deprecation:

1. Emit `gen_ai.input.messages` and `gen_ai.output.messages` as log records instead of span events
2. Correlate logs with spans via trace context (the OTel SDK handles this automatically)
3. Document that users need both `OTLPSpanExporter` and `OTLPLogExporter`
4. Consider an opt-in flag (similar to `OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN`) during the transition

## OTLP Receiver

agentevals runs two OTLP receivers:

- **gRPC** on port 4317 (standard OTLP gRPC port, configurable via `--otlp-grpc-port`)
- **HTTP** on port 4318 (standard OTLP HTTP port)

Both accept traces and logs and feed into the same session manager.

### OTLP HTTP

| Endpoint | Content Types | Request compression |
|----------|--------------|---------------------|
| `/v1/traces` | `application/json`, `application/x-protobuf` | `gzip`, `identity` |
| `/v1/logs` | `application/json`, `application/x-protobuf` | `gzip`, `identity` |

Responses mirror the request's `Content-Type`, and are gzip-encoded when the client sends
`Accept-Encoding: gzip`. A stock OTel Collector needs no configuration: its OTLP exporter
gzip-compresses by default.

#### Conformance

| Condition | Response |
|-----------|----------|
| Payload undecodable (bad JSON, bad protobuf, corrupt or truncated gzip) | `400` — permanent, clients must not retry |
| Payload decodable but structurally invalid (e.g. `resourceSpans` is not a list of objects) | `400` |
| Unsupported `Content-Encoding` (`br`, `deflate`, `zstd`, …) | `400` — only `gzip` and `identity` are supported |
| Body exceeds 64 MiB on the wire | `413` |
| Body exceeds 64 MiB after decompression | `413` |
| gzip body exceeds 8 MiB compressed | `413` |
| gzip body has more than 32 concatenated members | `400` |
| Unrecognized `Content-Type` | `415` (a missing header is treated as JSON) |
| Success, nothing dropped | `200` with `partial_success` unset |
| Success, some records dropped | `200` with `partial_success` populated |

Request bodies are bounded on both paths and in both directions: the wire body at 64 MiB
(compressed or not) and the decompressed output at 64 MiB, with a tighter 8 MiB cap on compressed
input. Concatenated gzip members are counted as well, because decoding cost scales with member
count rather than output — a body of 20-byte empty members costs CPU in proportion to its size while
producing nothing, so a member cap is what actually bounds the work. Real exporters emit a single
member; 32 is generous headroom. Decompression runs off the event loop so a CPU-heavy body cannot
stall the API, UI or gRPC receiver, which share the process. Note that the wire-body check happens
after the body is read, so it bounds what is parsed rather than what is received; a body-limiting
proxy in front remains the way to bound the socket.

Every `4xx`/`5xx` body is a `google.rpc.Status` message (`Status.code` is omitted). Its encoding
mirrors the request, so the response's own `Content-Type` stays honest — the same choice the
reference Go OTLP receiver makes. The specification's literal wording ("the response body for all
`HTTP 4xx` and `HTTP 5xx` responses MUST be a Protobuf-encoded `Status` message") carries no JSON
exemption; mirroring is the reading that does not also violate the Content-Type rule.

#### Partial success

When records are dropped the receiver still returns `200`, but populates `partial_success` with
`rejected_spans` / `rejected_log_records` and an English `error_message`. Per the specification,
clients must not retry such a response.

Counted as rejected:

- Spans and logs dropped at the per-session caps (10,000 spans / 5,000 logs — see
  [streaming.md](streaming.md))
- Records with no `trace_id`, which cannot be routed to a session

Not counted as rejected:

- Log records that are not `gen_ai.*` events. These are filtered by design: agentevals ingests
  GenAI-semconv telemetry, and an application instrumented with many libraries emits far more
  non-GenAI logs than GenAI ones. Reporting them would attach a permanent warning to every export
  with no action a user could take.
- Log records buffered for orphan replay. Those are deferred, not rejected — they may still be
  attached to a session, or expire.

### OTLP gRPC

Implements the standard `TraceService/Export` and `LogsService/Export` RPCs. Configuration:

| Setting | Default |
|---------|---------|
| Max message size | 8 MB |
| Max concurrent RPCs | 32 |
| Compression | gzip |
| TLS | off (insecure) |

Dropped records are reported through `partial_success` exactly as over OTLP HTTP — see
[Partial success](#partial-success) above.

### Client configuration

For HTTP exporters:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

For gRPC exporters:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

Traces and logs stream into agentevals automatically. See [examples/README.md](../examples/README.md) for zero-code setup instructions.
