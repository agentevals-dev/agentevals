# Evaluating kagent agents on Kubernetes

Let's run two [kagent](https://github.com/kagent-dev/kagent) agents with the same prompt and tools, one on the Go ADK runtime and one on the Claude Code harness, and score them with agentevals. Telemetry goes through a Collector, which also prints the scores agentevals sends back.

```
kagent agents --> OTel Collector --> agentevals (UI on :8001)
                        ^                 |
                        +---- scores -----+
```

Tested with kagent `46fdd3d7` (`v1.0.0-alpha9` plus a UI only commit, `v1alpha3` agents on Substrate), Claude Code 2.1.285, Collector contrib 0.162.0, and `claude-haiku-4-5` for both agents.

What you get depends on the kagent version:

| kagent | Go ADK | Claude Code harness | Codex harness |
|---|---|---|---|
| `v1.0.0-alpha9` (tested) | Model and tool calls with arguments, results and tokens | Native Claude Code spans only. The `transform/claude_code` recipe turns them into tool names and model calls with tokens | Not tested |
| With runtime GenAI spans ([kagent#3121](https://github.com/kagent-dev/kagent/pull/3121), not released yet) | Same | The runtime writes `chat` and `execute_tool` spans with tool names, call ids and tokens, but no tool arguments. The recipe steps aside by itself | Runtime spans, not tested yet |

## Set it up

You need a cluster with kagent 1.0 already running.

Install agentevals and the Collector:

```bash
kubectl create namespace agentevals
kubectl apply -f otel-collector.yaml
helm install agentevals oci://ghcr.io/agentevals-dev/agentevals/helm/agentevals \
  -n agentevals -f agentevals-values.yaml --wait
```

Point kagent at the Collector:

```bash
helm upgrade kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent -n kagent --reuse-values \
  --set otel.exporter.otlp.endpoint=http://otel-collector.agentevals.svc.cluster.local:4317 \
  --set otel.traces.enabled=true --set otel.logs.enabled=true \
  --set otel.capture.messageContent=true --wait
```

Heads up: `messageContent` puts prompts and responses on spans. agentevals needs them, but they can be sensitive.

Create the agents, using the images of your kagent release:

```bash
export KAGENT_ADK_GO_IMAGE=<golang-adk image> KAGENT_CLAUDE_IMAGE=<claude-harness image>
envsubst < agents.yaml | kubectl apply -f -
```

kagent bakes the telemetry settings into each agent revision, so wait until `DESIRED` and `LATEST` match before chatting:

```bash
kubectl get agents -n kagent -o custom-columns=NAME:.metadata.name,DESIRED:.status.desiredRevision,LATEST:.status.latestSuccessfulRevision
```

## Chat with both agents

```bash
kubectl port-forward -n kagent svc/kagent-ui 8080:8080
kubectl port-forward -n agentevals svc/agentevals 8001:8001
```

Open the kagent UI at http://localhost:8080. For `adk-go-test`, and then `claude-test`, start a new chat and send:

1. *Use your tools to list the namespaces in this cluster, then tell me how many pods are running in the kagent namespace. Answer in two short sentences.*
2. *Now use your tools to list the services in the agentevals namespace. One sentence.*

Use a new chat after the agents got their new revision. Older chats stay on the old one and send nothing.

### Or from the command line

To repeat a run without clicking through the UI, talk to the kagent controller:

```bash
kubectl port-forward -n kagent svc/kagent-controller 8083:8083
```

With the kagent CLI, which uses `http://localhost:8083` by default:

```bash
for agent in adk-go-test claude-test; do
  session=$(kagent agent session create --agent "$agent" -o json | jq -r .session.id)
  kagent agent invoke --session "$session" -t "Use your tools to list the namespaces in this cluster, then tell me how many pods are running in the kagent namespace. Answer in two short sentences."
  kagent agent invoke --session "$session" -t "Now use your tools to list the services in the agentevals namespace. One sentence."
done
```

Without the CLI, send the same prompts over A2A. The first message starts a session; pass its `contextId` to continue it:

```bash
send() {
  curl -sS --max-time 300 "http://localhost:8083/agents/kagent/$1" \
    -H 'Content-Type: application/json' -H 'X-User-Id: admin@kagent.dev' \
    -d "$(jq -n --arg t "$2" --arg c "${3:-}" --arg id "$(uuidgen)" \
      '{jsonrpc: "2.0", id: "1", method: "SendMessage",
        params: {message: ({messageId: $id, role: "ROLE_USER", parts: [{text: $t}]}
                           + (if $c == "" then {} else {contextId: $c} end))}}')"
}

ctx=$(send adk-go-test "Use your tools to list the namespaces in this cluster, then tell me how many pods are running in the kagent namespace. Answer in two short sentences." | jq -r .result.task.contextId)
send adk-go-test "Now use your tools to list the services in the agentevals namespace. One sentence." "$ctx"
```

Each session (one `contextId`) shows up in agentevals as one session.

Now open agentevals at http://localhost:8001 and click **Local Development** in the sidebar. Each chat shows up as one session with two turns, and is marked complete a few seconds after the last answer. If the page stops loading after agentevals restarts, restart its port forward.

## Score them

The idea: pick one session you're happy with as the golden run, and agentevals scores every other session turn by turn against it.

1. On the `adk-go-test` session card, click **Set as EvalSet**. Its turns become the expected tool calls and answers.
2. Click **Continue to Evaluation**.
3. Pick the metrics and hit **Run Evaluation**:
   * `tool_trajectory_avg_score` checks that each turn called the same tools with the same arguments. Set the match type to `ANY_ORDER` if the order doesn't matter.
   * `response_match_score` compares each answer with the golden one (ROUGE 1 text overlap).

Clicking a session in the results shows each turn's expected and actual tool calls side by side.

What we got from one run:

| Session | Trajectory | Response match |
|---|---|---|
| Another `adk-go-test` chat | 1.0 | 0.93 |
| `claude-test` | 0.0 | 0.64 |

The second Go ADK chat made the same calls, so its trajectory matches and the answers are close. Claude Code fails the trajectory because its tool calls come without arguments (see below) and it uses extra tools like `ToolSearch` and `Bash`. To track Claude Code, use a Claude Code session as its golden run.

## Get the results as OTel events

agentevals can send every score as a `gen_ai.evaluation.result` log event, so it ends up next to the agent's traces in your observability backend. That's already on here, through `agentevals-values.yaml`:

```yaml
env:
  - name: AGENTEVALS_EVALUATION_EVENTS
    value: "true"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: http://otel-collector.agentevals.svc.cluster.local:4318
  - name: OTEL_SERVICE_NAME
    value: agentevals
```

The Collector prints them with its `debug` exporter. After a run, check:

```bash
kubectl logs -n agentevals deploy/otel-collector | grep -A12 "EventName: gen_ai.evaluation.result"
```

You get one event per turn and metric, like this one:

```
EventName: gen_ai.evaluation.result
Attributes:
     -> gen_ai.evaluation.name: Str(tool_trajectory_avg_score)
     -> gen_ai.evaluation.score.value: Double(1)
     -> gen_ai.evaluation.score.label: Str(pass)
     -> gen_ai.agent.name: Str(adk_go_test)
     -> gen_ai.conversation.id: Str(01a11b1c-41b6-794d-b0be-65a187d1fbcc)
     -> agentevals.evaluator.type: Str(deterministic)
     -> agentevals.eval_set.id: Str(manual-1)
Trace ID: bf5aed954bb31b9234b2a29b14224354
Span ID: 0b95fefded63d107
```

The trace and span id point at the turn's `invoke_agent` span, so your backend shows the score on the exact turn it belongs to. `gen_ai.conversation.id` is the kagent chat id, which is also the session id in agentevals.

Events carry no prompts or answers. Set `AGENTEVALS_EVALUATION_EVENTS_EXPLANATION=true` to add the evaluator's explanation, which can contain content. To forward events to a real backend and turn scores into a histogram metric, see [OpenTelemetry pipelines](../../docs/opentelemetry-pipeline.md). Outside Kubernetes, `agentevals run ... --emit-otel` and `agentevals serve --emit-otel` do the same.

## Long turns

kagent writes a TaskStore gRPC span for every streamed chunk, so a long turn can have thousands of them. agentevals keeps the first 10,000 spans of a trace, and a turn that passes that loses its `invoke_agent` span. The `filter/kagent_taskstore` processor in `otel-collector.yaml` drops those RPC spans before they reach agentevals. Turns, tool calls and tokens come out the same.

## Claude Code workaround

Claude Code exports its own `claude_code.*` spans instead of GenAI ones. kagent wraps each turn in a GenAI `invoke_agent` span, so out of the box you only get the message and the answer.

The `transform/claude_code` processor in `otel-collector.yaml` fills the gap for Claude Code 2.1.285:

* `claude_code.tool` becomes `execute_tool`, with `gen_ai.tool.name` taken from `tool_name` (minus the `mcp__<server>__` prefix)
* `claude_code.llm_request` becomes `chat`, with the model and token usage, cache reads and writes included

Recheck it when you upgrade the harness, since those names can change. The recipe is for kagent releases whose Claude runtime does not write its own GenAI spans, and it skips processes that do (their resource has `kagent.genai.producer`), so calls are never counted twice.

Still missing with this version:

* tool arguments, since Claude Code doesn't put them on spans
* a clean answer: the response is everything the harness streamed in the turn, subagent chatter included
* background calls, which aren't part of the turn's trace

## Clean up

```bash
envsubst < agents.yaml | kubectl delete -f -
helm uninstall agentevals -n agentevals
kubectl delete -f otel-collector.yaml
kubectl delete namespace agentevals
```
