# Sending telemetry to agentevals

agentevals reads standard OpenTelemetry GenAI telemetry: spans, plus log events for message content. Any producer that follows the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) works. This page says what agentevals needs, how to set up common producers, and what the receiver accepts.

## What agentevals needs

| To get | Send |
|---|---|
| Turns | An `invoke_agent` or `invoke_workflow` span per user turn. Without one, each root span with model calls becomes a turn. |
| User input and final answer | `gen_ai.input.messages` / `gen_ai.output.messages` on the agent span or the model call spans (as attributes or as log events). |
| Tool trajectory | `execute_tool` spans with `gen_ai.tool.name`, ideally `gen_ai.tool.call.id`. |
| Tool arguments and results | `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`, or `tool_call` / `tool_call_response` message parts. |
| Tokens and model | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.request.model` on model call spans. |
| Errors | Span status ERROR and `error.type`. |
| Sessions | One of the keys in [Sessions](#sessions). |

Message content is optional. Without it you still get tools, tokens, errors and latency, but text based metrics have nothing to score.

## Producer setup

| Producer | Set |
|---|---|
| Google ADK (Python) | Nothing for the defaults. For the latest conventions: `ADK_TELEMETRY_SCHEMA_VERSION_OPT_IN=2` and `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, with `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` (or `EVENT_ONLY` plus a log exporter). |
| adk-go | Nothing. The root `invoke_agent` span marks the turn. |
| Official OpenTelemetry GenAI packages (`opentelemetry-instrumentation-genai-*`, 1.2b0+) | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT` for content. They always use the latest conventions. |
| `opentelemetry-instrumentation-openai-v2` (LangChain, plain OpenAI) | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, and export logs too: content arrives as log events. |
| Strands | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`. |
| OpenAI Agents SDK | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_and_event` and `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`. Tool spans carry no call id, so tools match by name. |
| kagent (OpenLLMetry `gen_ai.prompt.N.*` attributes) | Nothing. These are read as a legacy fallback. |

Working setups for each are in [examples/zero-code-examples](../examples/zero-code-examples/).

If a framework only instruments the model client (each model call is its own trace), agentevals joins a call that answers a tool result to the turn before it. This needs message content.

## Sessions

Live traces are grouped into sessions by the first key present:

1. resource `agentevals.session_name`
2. `gen_ai.conversation.id` (span, then resource)
3. `session.id` (span, then resource)

Traces with none of these become a session of their own when they contain GenAI spans. Traces with no GenAI span at all are held back for two minutes and shown only if a GenAI span or a key arrives for them.

Reruns with the same `agentevals.session_name` become `name-2`, `name-3`. A rerun is recognized by a different `service.instance.id`, so set one per process:

```bash
export OTEL_RESOURCE_ATTRIBUTES="agentevals.session_name=my-agent,service.instance.id=$(uuidgen)"
```

Without `service.instance.id`, a new trace counts as a rerun once the previous session has been finished for 30 seconds. Sessions keyed by a conversation or session id never split.

A trace is finished 3 seconds after its root span arrives (30 seconds if no root ever arrives), and a session when all its traces are. New spans reopen a finished session; logs that arrive late update it without reopening. Finished sessions stay in memory for 2 hours.

Anyone who can reach the receiver can add traces to a session by reusing its key, and the server has no authentication. Bind it to 127.0.0.1 (`agentevals serve --host 127.0.0.1`) on shared machines, and do not expose it to untrusted networks.

### Evaluate live sessions

In the UI, pick a golden session and evaluate the others against it; each session is scored as one conversation. From scripts:

| Endpoint | Use |
|---|---|
| `GET /api/streaming/sessions` | List sessions with their turns |
| `GET /api/streaming/sessions/{id}/otlp` | The session as an OTLP/JSON document; `agentevals run` reads it back |
| `POST /api/streaming/create-eval-set` | Build an eval set from a session |
| `POST /api/streaming/evaluate-sessions` | Score every finished session against a golden session |

## Sampling

agentevals needs whole traces. Keep the SDK sampler at its default (always on). If you sample in a Collector, sample whole traces (tail sampling) and keep the logs of the traces you keep. A trace with missing spans gives missing turns or tools.

## What agentevals ignores

* Span names. Spans are classified by `gen_ai.operation.name`.
* `schema_url`.
* Log records that are not `gen_ai.*` events, and logs without trace context.
* Its own `gen_ai.evaluation.result` events, so a pipeline that loops back does not feed results in again.

Vendor attributes (`gcp.vertex.agent.*`) are read as a fallback, never required.

## Receiver

| Port | Protocol |
|---|---|
| 4318 | OTLP/HTTP: `/v1/traces`, `/v1/logs`, JSON or protobuf, gzip or no compression |
| 4317 | OTLP/gRPC: trace and log export, gzip |

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# or, for gRPC
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

Trace and span ids must be valid hex (32 and 16 characters). Records with bad ids are rejected one by one.

### Responses

| Situation | Response |
|---|---|
| Everything accepted | `200`, no `partialSuccess` |
| Some records refused (bad ids, a trace or session limit, full log buffer) | `200` with `partialSuccess` counts and a reason. Do not retry. |
| Everything refused because the server is full (memory or session slots) | `503` with `Retry-After` (gRPC `UNAVAILABLE`). Retry later. |
| Body cannot be decoded | `400` |
| Unsupported `Content-Type` or `Content-Encoding` | `415`. Use `compression: gzip` or `none`. |
| Body over 64 MiB, compressed body over 8 MiB, or more than 32 gzip members | `413` / `400` |

Error bodies are `google.rpc.Status` messages in the request's content type.

### Limits

| Limit | Value |
|---|---|
| Spans per trace / per session | 10,000 / 50,000 |
| Logs per trace / per session | 5,000 / 20,000 |
| Traces per session | 1,000 |
| Sessions | 100 (the oldest finished session is evicted first) |
| Memory | 1 GiB of decoded telemetry (`AGENTEVALS_LIVE_MAX_BYTES`) |
| Finished session kept for | 2 hours |
| Logs that arrive before their spans | kept for 5 minutes |

A Collector sending large batches should cap batch size so a request stays under 64 MiB.
