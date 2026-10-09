# OTLP fixture corpus

Real and hand built telemetry with the turns agentevals must extract from it. `tests/genai/test_corpus.py` checks every fixture three ways: loaded from the file, sent through the live store all at once (spans first and logs first), and replayed in delivery order (see below). All must match the truth file.

## Layout

| Path | What |
|---|---|
| `adk_py/<mode>/<scenario>.json` | Google ADK (Python 2.10) recordings, traces and logs in one OTLP/JSON document. One directory per telemetry mode (`v1_*` / `v2_*` schema, span or event content, content off, `_contrib` with the OpenAI v2 instrumentation underneath, `v2proj_*` the projected upstream shape). Scenarios S1 to S6b: single turn, tools, multi turn, AgentTool delegate, transfer, failures. |
| `samples/*.json` | kagent Jaeger exports (`helm*`, `k8s`) and a Tempo export, as captured. |
| `kagent/<version>/*.json` | kagent recordings per release: Go ADK and the Claude harness, with and without the Collector recipe from `examples/kubernetes`. Each directory has a `README.md` with the commit, versions and prompts. |
| `hand/*.json` | Hand built shapes: cycles, duplicate ids, remote parents, content off, deprecated keys, the three event name forms for evaluation results, OpenAI v2 logs with and without span ids, Strands span events, multi trace conversations, upstream parts, the MCP client and server spans of one tool call. |
| `*.truth.json` | The expected turns for the fixture next to it. |

## Truth file

```json
{
  "schema": "otel_core.truth/v1",
  "fixture": "hand/adk_go_main",
  "content_captured": true,
  "extra_turns_allowed": false,
  "turns": [
    {
      "user_text": "...",
      "final_text": "...",
      "intermediate_texts": [],
      "agents": ["a"],
      "tool_calls": [{"name": "...", "args": {}, "result": {}, "is_error": false, "error_type": null}],
      "tokens": {"input": 0, "output": 0},
      "llm_calls": 2,
      "external_evaluations": [{"name": "...", "score_value": 1.0, "score_label": "pass"}]
    }
  ],
  "tokens": {"input": 0, "output": 0},
  "session_key": {"kind": "conversation", "value": "<gen_ai.conversation.id>"}
}
```

* A turn is one user exchange. Delegated agents and transfers stay inside the turn.
* Tokens count each logical model call once (a wrapper span and the provider span inside it are one call).
* Truth reflects what the telemetry carries. With content off, texts, arguments and results are `null`, but tool names, errors and tokens are still expected.
* Tool results compare after JSON parsing and after unwrapping ADK's `{"result": X}`.
* `extra_turns_allowed: true` compares only the listed turns (matched by user text).
* `llm_calls` (optional) is the number of logical model calls in the turn. Set it whenever spans could be mistaken for model calls: a wrong count often leaves tokens unchanged.
* `session_key` (optional) is the key the live store must give the session (`conversation`, `session_id` or `session_name`, and its value). With it, the replay also checks that the fixture becomes exactly one session.

## Ordered replay

`test_ordered_replay` delivers each fixture the way exporters would and ticks the store's clock between deliveries, under two schedules:

* `per_span`: one span or log record at a time, in end time order.
* `bsp_5s`: the SDK batch processors of each process (resource): spans every 5 s or once 512 are waiting, logs every 1 s, with a fixed phase per process. These are the `OTEL_BSP_*` and `OTEL_BLRP_*` defaults.

For each schedule the replay checks that no session completes before the top span of every trace in it has arrived, that no session reopens after its final completion, the `session_key`, and that the extracted turns match. The schedules catch different ordering bugs, so both run. Fixtures where every span ends at the same time are skipped.

## Adding a fixture

1. Save the telemetry as one OTLP/JSON document (`resourceSpans` and `resourceLogs`), or a Jaeger export.
2. Write the truth by reading the telemetry, not by running agentevals on it.
3. Run `uv run pytest tests/genai/test_corpus.py`.

## Adding a kagent version

1. Record each scenario of `kagent/v1.0.0-alpha9/README.md` on a cluster running that release, and download each session from `/api/streaming/sessions/<id>/otlp`.
2. Remove the `agentevals.*` resource attributes the download adds, since `agentevals.session_name` overrides session keying.
3. Trim only long string values (tool outputs, repeated prompts). Never change ids, timestamps, parents or resources: the replay depends on them. Check that extraction and both schedules give the same result before and after.
4. Write the truths from the spans, with `llm_calls` and `session_key`.
5. Add a new `kagent/<version>/` directory and keep the older ones.
