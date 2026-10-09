# OTLP fixture corpus

Real and hand built telemetry with the turns agentevals must extract from it. `tests/genai/test_corpus.py` checks every fixture two ways: loaded from the file, and sent through the live store (spans first and logs first). Both must match the truth file.

## Layout

| Path | What |
|---|---|
| `adk_py/<mode>/<scenario>.json` | Google ADK (Python 2.10) recordings, traces and logs in one OTLP/JSON document. One directory per telemetry mode (`v1_*` / `v2_*` schema, span or event content, content off, `_contrib` with the OpenAI v2 instrumentation underneath, `v2proj_*` the projected upstream shape). Scenarios S1 to S6b: single turn, tools, multi turn, AgentTool delegate, transfer, failures. |
| `samples/*.json` | kagent Jaeger exports (`helm*`, `k8s`) and a Tempo export, as captured. |
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
      "external_evaluations": [{"name": "...", "score_value": 1.0, "score_label": "pass"}]
    }
  ],
  "tokens": {"input": 0, "output": 0}
}
```

* A turn is one user exchange. Delegated agents and transfers stay inside the turn.
* Tokens count each logical model call once (a wrapper span and the provider span inside it are one call).
* Truth reflects what the telemetry carries. With content off, texts, arguments and results are `null`, but tool names, errors and tokens are still expected.
* Tool results compare after JSON parsing and after unwrapping ADK's `{"result": X}`.
* `extra_turns_allowed: true` compares only the listed turns (matched by user text).

## Adding a fixture

1. Save the telemetry as one OTLP/JSON document (`resourceSpans` and `resourceLogs`), or a Jaeger export.
2. Write the truth by reading the telemetry, not by running agentevals on it.
3. Run `uv run pytest tests/genai/test_corpus.py`.
