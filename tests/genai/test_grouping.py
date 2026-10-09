"""Conversation keys do not depend on the order spans arrive in."""

from __future__ import annotations

import pytest

from agentevals.genai.grouping import conversation_key
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from otel.builders import request, span


def _agent(span_id: str, conversation: str, start: int, parent: str | None = None) -> dict:
    attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conversation}
    return span(span_id=span_id, parent=parent, attrs=attrs, start=start, end=9_000)


@pytest.mark.parametrize("delegate_first", [False, True])
def test_the_outermost_span_names_the_conversation(delegate_first):
    root = _agent("r", "conv-parent", 1_000)
    delegate = _agent("d", "conv-delegate", 2_000, parent="r")
    spans = [delegate, root] if delegate_first else [root, delegate]
    traces, _ = build_traces(decode_json_document(request(spans), strict=True).spans)
    assert conversation_key(traces[0]) == ("conversation", "conv-parent")
