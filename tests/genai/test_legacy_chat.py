"""Spans without ``gen_ai.operation.name`` count as model calls only when they record a call."""

from __future__ import annotations

import pytest

from agentevals.genai.extract import extract_conversation
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from otel.builders import agent, chat, request, span

ROOT = agent(span_id="a", start=0, end=10_000)
IDENTITY = {"gen_ai.request.model": "claude-haiku-4-5", "gen_ai.provider.name": "anthropic"}


def _llm_calls(*spans: dict) -> int:
    decoded = decode_json_document(request([ROOT, *spans]), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    (turn,) = extract_conversation(traces, "s").turns
    return len(turn.llm_calls)


def test_identity_copied_onto_every_span_is_not_a_model_call():
    harness_spans = [
        span(span_id=name, parent="a", name=name, attrs={**IDENTITY, "tool_name": "Bash"}, start=1_000, end=2_000)
        for name in ("claude_code.tool", "claude_code.tool.execution", "claude_code.tool.blocked_on_user")
    ]
    assert _llm_calls(chat(span_id="real", parent="a", start=500, end=900), *harness_spans) == 1


@pytest.mark.parametrize(
    "record",
    [
        {"gen_ai.usage.prompt_tokens": 12, "gen_ai.usage.completion_tokens": 3},
        {"gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "hello"},
    ],
    ids=["usage", "indexed_prompt"],
)
def test_openllmetry_span_recording_a_call_is_a_model_call(record):
    legacy = span(
        span_id="c",
        parent="a",
        name="openai.chat",
        attrs={"gen_ai.request.model": "gpt-test", "gen_ai.system": "openai", **record},
        start=1_000,
        end=2_000,
    )
    assert _llm_calls(legacy) == 1
