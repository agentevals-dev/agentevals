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

`EvaluateTraces` lists the judged turns of each group in `turn_refs`. `evaluated_trace_id` and `evaluated_span_id` name the span the turn's results belong to, the same span agentevals attaches its own [evaluation result records](opentelemetry-pipeline.md#send-evaluation-results-from-agentevals) to. Use it when you attach results to telemetry yourself.

`EvaluateTraces` takes an optional `run_id` (a UUID). Every result record of the call carries it as `agentevals.eval.run.id`, and with run storage the evaluation is recorded under it. An id that is already recorded is rejected with `ALREADY_EXISTS`. Without `run_id`, a call recorded in run storage stamps its records with the recorded run's id.

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
| `--api-grpc-only` or `AGENTEVALS_API_GRPC_ONLY=true` | Serve only the gRPC API on `--host`, see below |
| `--api-grpc-auth` or `AGENTEVALS_API_GRPC_AUTH` | `none` (default) or `kubernetes`, see [Security](#security) |
| `--api-grpc-auth-audience` or `AGENTEVALS_API_GRPC_AUTH_AUDIENCE` | Token audience, default `agentevals` |
| `--api-grpc-auth-allowed-service-account` or `AGENTEVALS_API_GRPC_AUTH_ALLOWED_SERVICE_ACCOUNTS` | Allowed ServiceAccount as `namespace/name`; repeat the flag, or comma separate the env var |
| `AGENTEVALS_GRPC_REFLECTION=true` | Turn on server reflection |
| `AGENTEVALS_API_TRACES=true` | Export traces of the API server itself (see below) |

Messages can be up to 16 MiB. Uploaded trace documents are limited to 10 MiB each, as in the REST API.

The Helm chart has the same switches under `grpcApi`: `enabled`, `reflection`, and `grpcReadinessProbe` to check readiness with the gRPC health service. The Service exposes the port as `grpc-api`.

### Serving only the gRPC API

When one program is the only caller, run the server with `--api-grpc-only`. The gRPC API listens on `--host`. The web UI and REST API, both OTLP receivers and MCP listen on `127.0.0.1` only, so nothing else is reachable from the network:

```bash
uv run agentevals serve --api-grpc-only
```

Check health with `grpc.health.v1` on the gRPC API port; `/api/health` is only reachable from inside the host or pod. `kubectl port-forward` still reaches the web UI, because it connects to the pod's loopback address.

## Tracing the API server

agentevals receives telemetry, so it exports none of its own by default. With `AGENTEVALS_API_TRACES=true` it traces every API call with the OpenTelemetry gRPC instrumentation, using the standard exporter variables:

```bash
AGENTEVALS_API_TRACES=true OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 uv run agentevals serve
AGENTEVALS_API_TRACES=true OTEL_TRACES_EXPORTER=console uv run agentevals serve
```

`OTEL_SDK_DISABLED=true` or `OTEL_TRACES_EXPORTER=none` turn it off again. Health checks and reflection are not traced. If these traces are sent to agentevals itself, it drops them, so they never show up as sessions.

## Security

By default the API has no authentication. Any caller that can reach the port can read sessions, run evaluations with the server's credentials, and submit runs. Expose port 8002 only to trusted callers.

On a laptop `agentevals serve` listens on all interfaces. Pass `--host 127.0.0.1` to keep every port local.

### Kubernetes ServiceAccount authentication

Inside a cluster, the server can admit only the ServiceAccounts you name:

```bash
agentevals serve --api-grpc-only \
  --api-grpc-auth kubernetes \
  --api-grpc-auth-allowed-service-account evals/controller
```

Callers send a ServiceAccount token minted for the audience as `authorization: Bearer <token>` metadata, for example a [projected token](https://kubernetes.io/docs/concepts/storage/projected-volumes/#serviceaccounttoken) with `audience: agentevals`. The server checks it with a TokenReview, so its own ServiceAccount needs `create` on `tokenreviews.authentication.k8s.io`.

| Result | Status |
|--------|--------|
| No token, a malformed or expired token, a token for another audience, or a user that is not a ServiceAccount | `UNAUTHENTICATED` |
| A valid ServiceAccount that is not on the list | `PERMISSION_DENIED` |
| The TokenReview fails, for example because the Kubernetes API is down | `UNAVAILABLE` |

The list is required: any pod can mint a token for any audience, so the audience alone proves nothing. The server refuses to start with an empty list, and never lets a call through when a check fails. A successful check is cached for up to 30 seconds and never past the token's expiry. Health checks need no token. Reflection, when turned on, needs one like every other call: `grpcurl -H "authorization: Bearer $TOKEN" ...`.

Authentication covers only the gRPC API. Combine it with `--api-grpc-only` so the REST API, the OTLP receivers and MCP stay on `127.0.0.1`; without it the server prints a warning.
