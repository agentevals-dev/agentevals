"""A turn that pauses for an approval or a question and resumes as a new agent span is one turn."""

from __future__ import annotations

import pytest

from agentevals.genai.extract import extract_conversation
from agentevals.genai.grouping import group_traces
from agentevals.genai.messages import text_of
from agentevals.genai.model import GAP_POSSIBLE_CONTINUATION
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from agentevals.runner import evaluation_groups
from otel.builders import assistant, chat, request, sid, span, tid, user

DELETE = ("call_1", "delete_pod", {"name": "web-1"})


def _traces(*spans: dict):
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    return traces


def _agent(trace, span_id, start, end, inputs=None, outputs=None, links=(), name="assistant"):
    attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": "ctx-1"}
    if name:
        attrs["gen_ai.agent.name"] = name
    if inputs:
        attrs["gen_ai.input.messages"] = inputs
    if outputs:
        attrs["gen_ai.output.messages"] = outputs
    return span(trace, span_id, None, "invoke_agent", attrs, start, end, links=links)


def _tool(trace, span_id, parent, start, end, error=None, name="delete_pod", arguments=None):
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": "call_1"}
    if error:
        attrs["error.type"] = error
    if arguments:
        attrs["gen_ai.tool.call.arguments"] = arguments
    return span(trace, span_id, parent, f"execute_tool {name}", attrs, start, end)


def _initial(trace="a", span_id="a1", start=1_000, question="Delete pod web-1", name="assistant"):
    return [
        _agent(trace, span_id, start, start + 2_000, inputs=[user(question)], name=name),
        chat(
            trace=trace,
            span_id=f"{span_id}c",
            parent=span_id,
            start=start + 100,
            end=start + 1_000,
            inputs=[user(question)],
            outputs=[assistant(tool_calls=[DELETE])],
            finish="tool_call",
        ),
    ]


def _resumed(trace="b", span_id="a2", start=60_000, origin=("a", "a1"), answer="Deleted web-1", name="assistant"):
    return [
        _agent(trace, span_id, start, start + 3_000, outputs=[assistant(answer)], links=[origin], name=name),
        _tool(trace, f"{span_id}t", span_id, start + 100, start + 1_000),
        chat(trace=trace, span_id=f"{span_id}c", parent=span_id, start=start + 1_100, end=start + 2_000, content=False),
    ]


def test_a_resumed_segment_joins_the_turn_it_continues():
    conversation = extract_conversation(_traces(*_initial(), *_resumed()), "ctx-1")
    [turn] = conversation.turns
    assert (turn.ref.trace_id, turn.ref.span_id) == (tid("a"), sid("a1"))
    assert text_of(turn.user_input) == "Delete pod web-1"
    assert text_of(turn.final_output) == "Deleted web-1"
    assert [(t.name, t.ref.span_id) for t in turn.tool_calls] == [("delete_pod", sid("a2t"))]
    assert len(turn.llm_calls) == 2
    assert conversation.trace_ids == [tid("a"), tid("b")]


def test_a_tool_open_at_the_pause_counts_once_as_the_finished_call():
    initial = [*_initial(), _tool("a", "a1t", "a1", 1_200, 3_000, error="unfinished")]
    [turn] = extract_conversation(_traces(*initial, *_resumed()), "ctx-1").turns
    assert [(t.ref.span_id, t.is_error) for t in turn.tool_calls] == [(sid("a2t"), False)]


def test_a_tool_left_unfinished_without_a_later_finish_stays_as_recorded():
    initial = [*_initial(), _tool("a", "a1t", "a1", 1_200, 3_000, error="unfinished")]
    resumed = _resumed()
    resumed.pop(1)
    [turn] = extract_conversation(_traces(*initial, *resumed), "ctx-1").turns
    assert [(t.ref.span_id, t.error_type) for t in turn.tool_calls] == [(sid("a1t"), "unfinished")]


def test_the_finished_call_keeps_arguments_only_the_paused_span_recorded():
    initial = [
        _agent("a", "a1", 1_000, 3_000, inputs=[user("Delete pod web-1")]),
        chat(trace="a", span_id="a1c", parent="a1", start=1_100, end=2_000, content=False, finish="tool_call"),
        _tool("a", "a1t", "a1", 1_200, 3_000, error="unfinished", arguments={"name": "web-1"}),
    ]
    [turn] = extract_conversation(_traces(*initial, *_resumed()), "ctx-1").turns
    assert [(t.ref.span_id, t.arguments, t.is_error) for t in turn.tool_calls] == [
        (sid("a2t"), {"name": "web-1"}, False)
    ]


def test_calls_sharing_an_id_inside_one_part_stay_apart():
    resumed = [*_resumed(), _tool("b", "a2d", "a2", 60_200, 60_900, name="describe_pod")]
    [turn] = extract_conversation(_traces(*_initial(), *resumed), "ctx-1").turns
    assert sorted(t.name for t in turn.tool_calls) == ["delete_pod", "describe_pod"]


def test_every_resumed_segment_joins_the_same_turn():
    second = _resumed(trace="c", span_id="a3", start=90_000, answer="Done, and cleaned up")
    [turn] = extract_conversation(_traces(*_initial(), *_resumed(), *second), "ctx-1").turns
    assert text_of(turn.final_output) == "Done, and cleaned up"
    assert len(turn.llm_calls) == 3


def test_it_joins_the_linked_turn_when_another_turn_comes_between():
    between = _initial(trace="m", span_id="m1", start=30_000, question="List pods")
    turns = extract_conversation(_traces(*_initial(), *between, *_resumed()), "ctx-1").turns
    assert [(text_of(t.user_input), text_of(t.final_output)) for t in turns] == [
        ("Delete pod web-1", "Deleted web-1"),
        ("List pods", None),
    ]


def test_a_linked_segment_with_its_own_input_is_a_new_turn():
    resumed = _resumed()
    resumed[0] = _agent("b", "a2", 60_000, 63_000, inputs=[user("And web-2?")], links=[("a", "a1")])
    assert len(extract_conversation(_traces(*_initial(), *resumed), "ctx-1").turns) == 2


def test_a_link_to_another_agent_is_not_a_resume():
    resumed = _resumed(name="other")
    assert len(extract_conversation(_traces(*_initial(), *resumed), "ctx-1").turns) == 2


def test_a_link_between_unnamed_agents_is_not_a_resume():
    traces = _traces(*_initial(name=None), *_resumed(name=None))
    assert len(extract_conversation(traces, "ctx-1").turns) == 2
    assert len(group_traces(traces, by_conversation=False)) == 2


def test_a_resumed_segment_without_its_origin_keeps_a_warning():
    [turn] = extract_conversation(_traces(*_resumed()), "ctx-1").turns
    assert text_of(turn.final_output) == "Deleted web-1"
    assert [w.code for w in turn.warnings] == [GAP_POSSIBLE_CONTINUATION]


@pytest.mark.parametrize("by_conversation", [False, True])
def test_linked_traces_form_one_group_in_every_mode(by_conversation):
    unrelated = _initial(trace="z", span_id="z1", start=100_000, question="Hi")
    groups = group_traces(_traces(*_resumed(), *_initial(), *unrelated), by_conversation)
    if by_conversation:
        assert [[t.trace_id for t in members] for _, members in groups] == [[tid("a"), tid("b"), tid("z")]]
    else:
        assert [(key, [t.trace_id for t in members]) for key, members in groups] == [
            (tid("a"), [tid("a"), tid("b")]),
            (tid("z"), [tid("z")]),
        ]


def test_default_grouping_evaluates_the_paused_turn_whole():
    groups = evaluation_groups(_traces(*_initial(), *_resumed()), "auto")
    assert [[t.trace_id for t in members] for _, members in groups] == [[tid("a"), tid("b")]]
