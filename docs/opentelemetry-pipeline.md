# OpenTelemetry pipelines

How to put agentevals behind an OpenTelemetry Collector, and how to get evaluation results back into your observability backend. Both configs below were checked with `otelcol-contrib validate` (Collector contrib 0.162.0).

## Forward agent telemetry to agentevals

Send traces and logs from your agents to the Collector, and from the Collector to agentevals:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
exporters:
  otlphttp/agentevals:
    endpoint: http://agentevals:4318
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp/agentevals]
    logs:
      receivers: [otlp]
      exporters: [otlphttp/agentevals]
```

The `otlphttp` exporter uses gzip by default, which agentevals accepts. Send logs as well as traces: several instrumentations put message content in log events.

Send to one agentevals instance. Live sessions are kept in the memory of the process that receives them, so behind a load balancer with several replicas a session is split between them. The Helm chart keeps `replicaCount: 1` for this reason.

## Production: backend and agentevals side by side

One Collector, two branches:

* **To your backend**, with prompt and response text removed, so content is not stored long term.
* **To agentevals**, with content, so it can be evaluated.

Evaluation results come back from agentevals as log events. They go to your backend next to the agent's traces, and a score histogram metric is derived from them.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20
  transform/drop_genai_content:
    trace_statements:
      - delete_key(span.attributes, "gen_ai.input.messages")
      - delete_key(span.attributes, "gen_ai.output.messages")
      - delete_key(span.attributes, "gen_ai.system_instructions")
  filter/not_from_agentevals:
    logs:
      log_record:
        - resource.attributes["service.name"] == "agentevals"
connectors:
  signaltometrics:
    logs:
      - name: agentevals.evaluation.score
        description: Scores from gen_ai.evaluation.result events
        unit: "1"
        conditions:
          - log.event_name == "gen_ai.evaluation.result" and log.attributes["gen_ai.evaluation.score.value"] != nil
        attributes:
          - key: gen_ai.evaluation.name
          - key: gen_ai.evaluation.score.label
            optional: true
        histogram:
          buckets: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
          value: Double(log.attributes["gen_ai.evaluation.score.value"])
exporters:
  otlp/backend:
    endpoint: ${env:BACKEND_OTLP_ENDPOINT}
    sending_queue:
      enabled: true
  otlphttp/agentevals:
    endpoint: http://agentevals:4318
    sending_queue:
      enabled: true
service:
  pipelines:
    traces/backend:
      receivers: [otlp]
      processors: [memory_limiter, transform/drop_genai_content]
      exporters: [otlp/backend]
    traces/agentevals:
      receivers: [otlp]
      processors: [memory_limiter]
      exporters: [otlphttp/agentevals]
    logs/backend:
      receivers: [otlp]
      processors: [memory_limiter]
      exporters: [otlp/backend, signaltometrics]
    logs/agentevals:
      receivers: [otlp]
      processors: [memory_limiter, filter/not_from_agentevals]
      exporters: [otlphttp/agentevals]
    metrics:
      receivers: [signaltometrics]
      exporters: [otlp/backend]
```

Notes:

* `filter/not_from_agentevals` keeps agentevals' own results out of the agentevals branch. agentevals also drops them itself.
* If you sample, sample whole traces and only on the backend branch. agentevals needs complete traces.
* For producers that use OpenInference or OpenLLMetry attribute names, the contrib `gen_ai_normalizer` processor (alpha) can map them to GenAI conventions on the agentevals branch.
* For Claude Code under kagent, the [Kubernetes example](../examples/kubernetes/README.md#claude-code-workaround) has a `transform` recipe that maps its spans to tool and model calls.

## Send evaluation results from agentevals

Turn it on with `--emit-otel` (`agentevals run` and `agentevals serve`) or `AGENTEVALS_EVALUATION_EVENTS=true`, and point the standard exporter variables at your Collector:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
agentevals run traces.json -e eval_set.json -m tool_trajectory_avg_score --emit-otel
```

`OTEL_EXPORTER_OTLP_PROTOCOL=grpc` (with port 4317) switches to gRPC. `OTEL_SDK_DISABLED=true` or `OTEL_LOGS_EXPORTER=none` turns emitting off even when it is enabled. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` work as usual.

Each result is a `gen_ai.evaluation.result` log event attached to the span it scores (the turn's agent span, or its last model call). Metrics that score each turn give one event per turn; others give one event on the last turn.

| Attribute | Value |
|---|---|
| `gen_ai.evaluation.name` | Evaluator name |
| `gen_ai.evaluation.score.value` | Score, when there is one |
| `gen_ai.evaluation.score.label` | `pass`, `fail` or `not_evaluated` |
| `error.type` | Exception class when the evaluator failed (no score or label then) |
| `gen_ai.evaluation.explanation` | Only with `AGENTEVALS_EVALUATION_EVENTS_EXPLANATION=true` |
| `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.response.id` | Copied from the evaluated span when present |
| `agentevals.evaluator.type` | `llm_judge` or `deterministic` for built in metrics |
| `agentevals.eval.run.id`, `agentevals.eval.case.id`, `agentevals.eval_set.id` | When known |

Events never contain prompts, responses or tool arguments. The explanation can, which is why it is off by default.
