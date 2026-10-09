# kagent v1.0.0-alpha9

Recorded on 2026-10-08 from a kind cluster running kagent `46fdd3d7`, which is `v1.0.0-alpha9` plus one UI only commit (same `telemetry/resolved.yaml`). `v1alpha3` agents on Substrate, Claude Code 2.1.285, `claude-haiku-4-5` for both agents, content capture `SPAN_ONLY`. Downloaded from agentevals at `/api/streaming/sessions/<id>/otlp`.

Prompts:

1. "Use your tools to list the namespaces in this cluster, then tell me how many pods are running in the kagent namespace. Answer in two short sentences."
2. "Now use your tools to list the services in the agentevals namespace. One sentence."

| File | Runtime | Turns | What it covers |
|---|---|---|---|
| `adk_go_1turn.json` | Go ADK | 1 | Sent straight to agentevals. Two tool calls with arguments. |
| `adk_go_2turn.json` | Go ADK | 2 | Sent through the Collector. kagent's TaskStore gRPC spans arrive before the root when batched per process. |
| `claude_native_1turn_subagent.json` | Claude harness | 1 | Claude Code native spans only, with an `Agent` subagent. |
| `claude_native_2turn.json` | Claude harness | 2 | Claude Code native spans only. Spans with `session.id` export before kagent's `invoke_agent`. |
| `claude_recipe_2turn.json` | Claude harness | 2 | `transform/claude_code` from `examples/kubernetes/otel-collector.yaml` applied: tool calls by name without arguments, model calls with usage. |

Changes from the download:

* The `agentevals.*` resource attributes the download endpoint stamps are removed, since `agentevals.session_name` would override session keying.
* Written as compact JSON. Claude Code `tool.output` span event outputs are cut to 500 characters.
* `claude_recipe_2turn` was recorded with an earlier recipe that left out cache tokens. Its `claude_code.*` attributes are the output of today's recipe on Collector contrib 0.162.0. Ids, timestamps, parents and resources are as recorded.

Content: cluster namespaces, pod and service names, the agent system prompt and the answers. No credentials.
