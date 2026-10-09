"""Tool trajectory: layered tool spans, message only tool calls and result pairing."""

from __future__ import annotations

import json

import pytest

from agentevals.genai.extract import extract_conversation
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from otel.builders import agent, assistant, chat, request, sid, span, tool_result, user


def _turn(*spans: dict):
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    turns = extract_conversation(traces, "s").turns
    assert len(turns) == 1
    return turns[0]


def tool(span_id: str, parent: str, name: str, start: int, call_id: str | None = None, **attrs) -> dict:
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, **attrs}
    if call_id:
        attrs["gen_ai.tool.call.id"] = call_id
    return span(span_id=span_id, parent=parent, name=f"execute_tool {name}", attrs=attrs, start=start, end=start + 500)


ROOT = agent(span_id="a", start=0, end=10_000)


class TestToolLayers:
    def test_mcp_server_span_with_its_own_request_id_joins_the_call(self):
        turn = _turn(
            ROOT,
            tool("fw", "a", "search", 1_000, call_id="x1"),
            tool("client", "fw", "search", 1_010, call_id="x1", **{"mcp.method.name": "tools/call"}),
            tool("server", "client", "search", 1_020, call_id="rpc-7", **{"gen_ai.tool.call.result": '{"hits": 3}'}),
        )
        assert [(t.name, t.call_id, t.result) for t in turn.tool_calls] == [("search", "x1", {"hits": 3})]
        call = turn.tool_calls[0]
        assert call.ref.span_id == sid("fw")
        assert [r.span_id for r in call.inner_refs] == [sid("client"), sid("server")]

    def test_a_server_span_that_starts_before_its_client_still_joins(self):
        turn = _turn(ROOT, tool("client", "a", "search", 2_000), tool("server", "client", "search", 1_000))
        assert len(turn.tool_calls) == 1

    def test_an_error_on_an_inner_layer_fails_the_call(self):
        failed = tool("client", "fw", "search", 1_010)
        failed["status"] = {"code": 2}
        turn = _turn(ROOT, tool("fw", "a", "search", 1_000), failed)
        assert [(t.is_error, t.error_type) for t in turn.tool_calls] == [(True, "_OTHER")]

    @pytest.mark.parametrize(
        "spans",
        [
            pytest.param([tool("t1", "a", "search", 1_000), tool("t2", "a", "search", 2_000)], id="siblings"),
            pytest.param([tool("t1", "a", "outer", 1_000), tool("t2", "t1", "inner", 1_010)], id="different-names"),
            pytest.param(
                [
                    tool("t1", "a", "lookup", 1_000),
                    agent(span_id="sub", parent="t1", name="sub", start=1_005, end=1_400),
                    tool("t2", "sub", "lookup", 1_010),
                ],
                id="agent-between",
            ),
            pytest.param(
                [
                    tool("t1", "a", "lookup", 1_000),
                    chat(span_id="c", parent="t1", start=1_005, end=1_400),
                    tool("t2", "c", "lookup", 1_010),
                ],
                id="model-call-between",
            ),
        ],
    )
    def test_separate_calls_stay_separate(self, spans):
        assert len(_turn(ROOT, *spans).tool_calls) == 2


class TestMessageParts:
    def test_a_tool_call_without_a_span_keeps_its_place_in_the_trajectory(self):
        request_call = chat(
            span_id="c1",
            parent="a",
            start=100,
            end=200,
            inputs=[user("go")],
            outputs=[
                assistant(tool_calls=[("x1", "first", {"n": 1}), ("x2", "second", {"n": 2}), ("x3", "third", {"n": 3})])
            ],
        )
        turn = _turn(
            ROOT, request_call, tool("s1", "a", "first", 300, call_id="x1"), tool("s3", "a", "third", 400, call_id="x3")
        )
        assert [(t.name, t.arguments) for t in turn.tool_calls] == [
            ("first", {"n": 1}),
            ("second", {"n": 2}),
            ("third", {"n": 3}),
        ]
        assert any("second appears only in message parts" in w for w in turn.warnings)

    def test_a_result_already_in_a_calls_input_does_not_answer_its_new_request(self):
        call = chat(
            span_id="c1",
            inputs=[
                user("roll twice"),
                assistant(tool_calls=[("call_1", "roll", {})]),
                tool_result("call_1", "roll", 4),
            ],
            outputs=[assistant(tool_calls=[("call_2", "roll", {})])],
        )
        turn = _turn(call)
        assert [(t.call_id, t.result) for t in turn.tool_calls] == [("call_2", None)]
        assert not [w for w in turn.warnings if "by order" in w]

    def test_results_still_pair_by_order_across_calls(self):
        first = chat(
            span_id="c1", parent="a", start=100, end=200, outputs=[assistant(tool_calls=[("call_1", "roll", {})])]
        )
        second = chat(
            span_id="c2",
            parent="a",
            start=300,
            end=400,
            inputs=[user("roll"), assistant(tool_calls=[("call_1", "roll", {})]), tool_result("other-id", "roll", 6)],
            outputs=[assistant("6")],
        )
        turn = _turn(ROOT, first, second)
        assert [(t.call_id, t.result) for t in turn.tool_calls] == [("call_1", 6)]


def test_tool_call_arguments_survive_as_json():
    turn = _turn(ROOT, tool("t1", "a", "search", 1_000, **{"gen_ai.tool.call.arguments": json.dumps({"q": "otel"})}))
    assert turn.tool_calls[0].arguments == {"q": "otel"}
