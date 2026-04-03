# OpenTelemetry Compatibility

agentevals consumes OpenTelemetry traces to evaluate AI agents. This document covers which OTel conventions we support, how we handle the ongoing migration from span events to log-based events, and guidance for instrumenting your own agents.

## Supported Semantic Conventions

### OTel GenAI Semantic Conventions (recommended)

The [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) define standard span attributes for LLM interactions. agentevals auto-detects this format when spans contain `gen_ai.request.model` or `gen_ai.input.messages`.

Supported attributes:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.request.model` | Model name (e.g. `gpt-4o`, `claude-sonnet-4-6`) |
| `gen_ai.input.messages` | JSON array of input messages |
| `gen_ai.output.messages` | JSON array of output messages |
| `gen_ai.response.finish_reasons` | Why the model stopped generating |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |
| `gen_ai.system` | AI system identifier (e.g. `openai`, `anthropic`) |

This format works with LangChain, Strands, OpenAI instrumentation, Anthropic instrumentation, and any framework that follows the GenAI semantic conventions.

### Google ADK (framework-native)

Google ADK emits spans under the `gcp.vertex.agent` OTel scope with proprietary attributes (`gcp.vertex.agent.llm_request`, `gcp.vertex.agent.llm_response`, etc.). agentevals has a dedicated converter that auto-detects this format. No GenAI semconv configuration is needed.

### Format Detection

Format detection is automatic. When a trace contains both ADK and GenAI attributes, ADK takes priority because it provides richer structured data. The detection logic lives in `src/agentevals/converter.py` (`get_extractor()`).

## Message Content Delivery

GenAI message content (`gen_ai.input.messages`, `gen_ai.output.messages`) can arrive through three mechanisms. agentevals supports all of them:

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

agentevals runs:

- OTLP HTTP receiver on port 4318 (standard OTLP HTTP port)
- OTLP gRPC receiver on port 4317 (standard OTLP gRPC port).

OTLP HTTP accepts:

| Endpoint | Content Types |
|----------|--------------|
| `/v1/traces` | `application/json`, `application/x-protobuf` |
| `/v1/logs` | `application/json`, `application/x-protobuf` |

Point OTLP/HTTP exporters at `http://localhost:4318`.
Point OTLP/gRPC exporters at `localhost:4317` with `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`.
Traces and logs stream into agentevals automatically. See [examples/README.md](../examples/README.md) for zero-code setup instructions.
