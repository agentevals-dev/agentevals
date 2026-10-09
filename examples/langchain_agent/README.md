# LangChain agent with live evaluation

A LangChain dice agent (roll a die, check primes) on OpenAI, streamed to agentevals with an AgentEvals SDK session.

## Run it

```bash
pip install -e .
pip install -r examples/langchain_agent/requirements.txt
export OPENAI_API_KEY="sk-..."

agentevals serve --dev                      # terminal 1
cd ui && npm run dev                        # terminal 2 (optional), http://localhost:5173
python examples/langchain_agent/main.py     # terminal 3
```

`python examples/langchain_agent/test_streaming.py` checks that this machine can reach the receiver.

## How it works

```python
app = AgentEvals(eval_set_id="langchain_agent_eval")

with app.session(session_name=session_name):
    ...  # run the agent
```

* The SDK instruments the OpenAI client (`opentelemetry-instrumentation-openai-v2`) and sets up a log provider, because this instrumentation puts message content in log events. Content capture is turned on for you when you have not set it: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, or `SPAN_ONLY` when `OTEL_SEMCONV_STABILITY_OPT_IN` includes `gen_ai_latest_experimental`.
* Spans and GenAI log events produced inside the session go to agentevals on port 4318. Leaving the block flushes them.
* LangChain only instruments the model client here, so each model call is its own trace. agentevals joins a call that answers a tool result to the turn before it, so the UI shows three turns.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ConnectionError` when the session starts | Start `agentevals serve --dev`, or pass `AgentEvals(endpoint=...)` |
| Turns have no text | Content capture is off, or logs are not exported |
| No session appears | The OpenAI client was created before instrumentation; create the agent inside the session |

Without the SDK, the same agent works with plain OTLP export: see [zero-code-examples/langchain](../zero-code-examples/langchain/).
