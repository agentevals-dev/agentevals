# gRPC API

`agentevals serve` serves a gRPC API on port 8002, next to the web UI and REST API on 8001. It is the API for programs: other services, scripts and CI. It speaks native gRPC over HTTP/2, so browsers cannot call it; the web UI uses the REST API and an SSE stream that carry the same messages as JSON.

The contract lives in [`proto/agentevals/api/v1alpha1`](../proto/agentevals/api/v1alpha1). `v1alpha1` can still change; CI checks every change for compatibility.

## Services

| Service | Methods |
|---------|---------|
| `SessionService` | `ListSessions`, `GetSession`, `WatchSessions` (every current session, then each change) |
| `EvaluationService` | `ListMetrics`, `EvaluateTraces` (streams progress and results) |
| `RunService` | `SubmitRun`, `GetRun`, `ListRuns`, `CancelRun`, `ListRunResults`, `WatchRun`. Needs the Postgres backend, see [Run History](run-history.md) |
| `ConversionService` | `ConvertTraces` (trace files to conversations and a golden eval set) |
| `SystemService` | `GetVersion`, `GetConfig` |

The server also implements the standard `grpc.health.v1.Health` service, so Kubernetes `grpc` probes and load balancers can check it.

Sessions and conversations come back as views: turns with their user text, tool calls paired with their results, model calls, token usage and status, already worked out by the server. Each field names the OpenTelemetry attribute it comes from. A token count that was not recorded is left unset, never reported as 0.

Traces go in as OTLP protobuf (`TracesData` and `LogsData`) or as files (OTLP/JSON, OTLP/JSONL or Jaeger JSON), the same formats the REST API takes.

## Calling it

With reflection on, `grpcurl` can list and call everything:

```bash
AGENTEVALS_GRPC_REFLECTION=true uv run agentevals serve --dev
grpcurl -plaintext localhost:8002 list
grpcurl -plaintext localhost:8002 agentevals.api.v1alpha1.SessionService/ListSessions
grpcurl -plaintext -d '{"includeTurns": true}' localhost:8002 agentevals.api.v1alpha1.SessionService/WatchSessions
```

Reflection is off by default. Without it, pass the protos with `-import-path` and `-proto`.

Generated clients:

* **Go:** `go get github.com/agentevals-dev/agentevals/gen/go@vX.Y.Z`, then import `github.com/agentevals-dev/agentevals/gen/go/agentevals/api/v1alpha1`.
* **Python:** the `agentevals-cli` package ships the stubs as `agentevals.api.v1alpha1`.
* **TypeScript:** generate from the protos with `buf` and `protoc-gen-es`, as `ui/src/generated` does.

Errors use standard gRPC status codes with a `google.rpc.ErrorInfo` detail whose `reason` is stable, for example `STORAGE_NOT_CONFIGURED` or `RUN_SPEC_CONFLICT`. Internal errors return `INTERNAL` with no details; the server log has them.

## Configuration

| Setting | Purpose |
|---------|---------|
| `--api-grpc-port` or `AGENTEVALS_API_GRPC_PORT` | Port, default `8002`; `0` turns the API off |
| `AGENTEVALS_GRPC_REFLECTION=true` | Turn on server reflection |
| `AGENTEVALS_API_TRACES=true` | Export traces of the API server itself (see below) |

Messages can be up to 16 MiB. Uploaded trace documents are limited to 10 MiB each, as in the REST API.

The Helm chart has the same switches under `grpcApi`: `enabled`, `reflection`, and `grpcReadinessProbe` to check readiness with the gRPC health service. The Service exposes the port as `grpc-api`.

## Tracing the API server

agentevals receives telemetry, so it exports none of its own by default. With `AGENTEVALS_API_TRACES=true` it traces every API call with the OpenTelemetry gRPC instrumentation, using the standard exporter variables:

```bash
AGENTEVALS_API_TRACES=true OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 uv run agentevals serve
AGENTEVALS_API_TRACES=true OTEL_TRACES_EXPORTER=console uv run agentevals serve
```

`OTEL_SDK_DISABLED=true` or `OTEL_TRACES_EXPORTER=none` turn it off again. Health checks and reflection are not traced. If these traces are sent to agentevals itself, it drops them, so they never show up as sessions.

## Security

The API has no authentication. Any caller that can reach the port can read sessions, run evaluations with the server's credentials, and submit runs. Expose port 8002 only to trusted callers, for example inside a cluster behind a NetworkPolicy, and put authentication in front of it when you need it.

On a laptop `agentevals serve` listens on all interfaces. Pass `--host 127.0.0.1` to keep every port local.
