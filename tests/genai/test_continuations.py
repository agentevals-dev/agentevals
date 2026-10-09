"""A model call that answers a tool result continues the turn before it (provider only telemetry)."""

from __future__ import annotations

from agentevals.genai.extract import conversation_from_turns, extract_conversation, extract_turns
from agentevals.genai.messages import text_of
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from otel.builders import agent, assistant, chat, request, sid, tool_result, user

ROLL = ("call_1", "roll_die", {"sides": 20})


def _traces(*spans: dict):
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    return traces


def _tool_round(content: bool = True):
    request_call = chat(
        trace="a",
        span_id="c1",
        inputs=[user("Roll a d20")],
        outputs=[assistant(tool_calls=[ROLL])],
        tokens=(10, 2),
        content=content,
        finish="tool_calls",
    )
    answer_call = chat(
        trace="b",
        span_id="c2",
        start=3_000,
        end=4_000,
        inputs=[user("Roll a d20"), assistant(tool_calls=[ROLL]), tool_result("call_1", "roll_die", 7)],
        outputs=[assistant("You rolled 7")],
        tokens=(20, 4),
        content=content,
        finish="stop",
    )
    return request_call, answer_call


def test_tool_round_across_traces_is_one_turn():
    conversation = extract_conversation(_traces(*_tool_round()), "s")
    assert len(conversation.turns) == 1
    turn = conversation.turns[0]
    assert turn.ref.span_id == sid("c1")
    assert text_of(turn.user_input) == "Roll a d20"
    assert text_of(turn.final_output) == "You rolled 7"
    assert [(t.name, t.arguments, t.result) for t in turn.tool_calls] == [("roll_die", {"sides": 20}, 7)]
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (30, 6)
    assert len(turn.llm_calls) == 2


def test_without_content_the_turns_stay_apart_with_a_warning():
    conversation = extract_conversation(_traces(*_tool_round(content=False)), "s")
    assert len(conversation.turns) == 2
    assert any("may continue" in w for w in conversation.turns[1].warnings)
    assert not any("may continue" in w for w in conversation.turns[0].warnings)


def test_a_new_question_is_not_joined():
    first = chat(trace="a", span_id="c1", inputs=[user("Hi")], outputs=[assistant("Hello")])
    second = chat(trace="b", span_id="c2", start=3_000, end=4_000, inputs=[user("Hi"), assistant("Hello"), user("Bye")])
    assert len(extract_conversation(_traces(first, second), "s").turns) == 2


def test_a_tool_answer_after_a_plain_reply_is_not_joined():
    first = chat(trace="a", span_id="c1", inputs=[user("Hi")], outputs=[assistant("Hello")])
    second = chat(
        trace="b",
        span_id="c2",
        start=3_000,
        end=4_000,
        inputs=[user("Hi"), tool_result("call_9", "x", 1)],
        outputs=[assistant("ok")],
    )
    assert len(extract_conversation(_traces(first, second), "s").turns) == 2


def test_continuation_joins_an_agent_turn():
    agent_turn = [
        agent(trace="a", span_id="a1", start=500, end=2_500),
        chat(trace="a", span_id="c1", parent="a1", inputs=[user("Roll a d20")], outputs=[assistant(tool_calls=[ROLL])]),
    ]
    answer = _tool_round()[1]
    conversation = extract_conversation(_traces(*agent_turn, answer), "s")
    assert len(conversation.turns) == 1
    assert conversation.turns[0].anchor_kind == "agent"
    assert text_of(conversation.turns[0].final_output) == "You rolled 7"


def test_joining_never_mutates_the_input_turns():
    traces = _traces(*_tool_round(content=False))
    per_trace = [extract_turns(t) for t in traces]
    before = [(t.index, list(t.warnings)) for turns in per_trace for t in turns]
    for _ in range(2):
        conversation_from_turns(traces, per_trace, "s")
    assert [(t.index, list(t.warnings)) for turns in per_trace for t in turns] == before


def test_a_tool_result_for_another_call_does_not_continue_the_turn():
    asks = chat(trace="a", span_id="c1", inputs=[user("Roll")], outputs=[assistant(tool_calls=[ROLL])])
    unrelated = chat(
        trace="b",
        span_id="c2",
        start=3_000,
        end=4_000,
        inputs=[
            user("Weather?"),
            assistant(tool_calls=[("call_9", "weather", {})]),
            tool_result("call_9", "weather", "sunny"),
        ],
        outputs=[assistant("sunny")],
    )
    conversation = extract_conversation(_traces(asks, unrelated), "s")
    assert [text_of(t.user_input) for t in conversation.turns] == ["Roll", "Weather?"]
    assert [t.name for t in conversation.turns[0].tool_calls] == ["roll_die"]
