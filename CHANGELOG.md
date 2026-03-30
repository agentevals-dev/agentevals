# Changelog

All notable changes to this project will be documented in this file.

## [0.5.3] - 2026-03-29

### Added
- **OpenAI Agents SDK zero-code example** (`examples/zero-code-examples/openai-agents/`): a self-contained dice-rolling agent that demonstrates zero-code OTLP integration with the OpenAI Agents SDK (`openai-agents>=0.3.3`) via `opentelemetry-instrumentation-openai-agents-v2`. Includes `run.py`, `requirements.txt`, and a golden `eval_set.json` with a multi-turn conversation case.
- **E2E integration tests** for the OpenAI Agents SDK example (`TestOpenAIAgentsZeroCode` in `tests/integration/test_live_agents.py`): verifies session creation, span emission, invocation extraction, and API visibility.

### Fixed
- Conversation context threading in `run.py` now uses `result.to_input_list()` (the SDK-idiomatic pattern) instead of manually appending raw role/content dicts, ensuring tool call history is preserved across turns.
- `force_flush()` is now called in a `try/finally` block, guaranteeing spans are sent to the OTLP receiver even when an API error occurs mid-conversation.
