"""Native event ownership survives conversion without changing exported JSON."""

import json

import pytest
from google.adk.evaluation.app_details import AgentDetails, AppDetails
from google.adk.evaluation.eval_case import EvalCase, IntermediateData, InvocationEvents
from google.genai import types

from agentevals.adk_bridge import expected_from_case, invocation_to_dict, to_adk_invocation
from agentevals.builtin_metrics import _enrich_app_details, _to_invocation_events, build_eval_metric, get_evaluator
from agentevals.genai.model import ToolCall, Turn
from agentevals.otel.model import SpanRef


def source_turn():
    return Turn(
        ref=SpanRef("synthetic-trace", "turn"),
        agent_name="routing-agent",
        user_input=[{"parts": [{"type": "text", "content": "Which field is missing?"}]}],
        final_output=[{"parts": [{"type": "text", "content": "Which severity?"}]}],
        intermediate_outputs=[{"parts": [{"type": "text", "content": "Checking the request."}]}],
        tool_calls=[
            ToolCall(agent_name="routing_agent", name="lookup", call_id="one", arguments={"id": 1}, result={"v": 1}),
            ToolCall(agent_name="specialist", name="lookup", call_id="two", arguments=[2], result=[3]),
            ToolCall(agent_name="specialist", name="pending", call_id="three"),
        ],
    )


def test_source_tool_owners_are_preserved_before_flattening():
    turn = source_turn()
    before = turn.model_dump(mode="json")
    invocation = to_adk_invocation(turn)
    assert isinstance(invocation.intermediate_data, InvocationEvents)
    events = invocation.intermediate_data.invocation_events
    assert [event.author for event in events] == [
        "routing_agent",
        "routing_agent",
        "specialist",
        "specialist",
        "specialist",
        "routing-agent",
    ]
    assert [event.content.role for event in events] == ["model", "user", "model", "user", "model", "model"]
    assert events[0].content.parts[0].function_call.id == events[1].content.parts[0].function_response.id == "one"
    assert events[2].content.parts[0].function_call.id == events[3].content.parts[0].function_response.id == "two"
    assert turn.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "tool_owner,turn_owner,expected", [("worker", "root", "worker"), (None, "root", "root"), (None, None, "agent")]
)
def test_missing_tool_owner_uses_turn_then_agent(tool_owner, turn_owner, expected):
    turn = Turn(
        ref=SpanRef("trace", "turn"),
        agent_name=turn_owner,
        tool_calls=[ToolCall(agent_name=tool_owner, name="lookup", result={})],
    )
    assert [event.author for event in to_adk_invocation(turn).intermediate_data.invocation_events] == [
        expected,
        expected,
    ]


def test_export_keeps_existing_flat_json_exactly():
    invocation = to_adk_invocation(source_turn())
    expected = {
        "invocation_id": "turn",
        "user_content": {"parts": [{"text": "Which field is missing?"}], "role": "user"},
        "final_response": {"parts": [{"text": "Which severity?"}], "role": "model"},
        "intermediate_data": {
            "tool_uses": [
                {"id": "one", "args": {"id": 1}, "name": "lookup"},
                {"id": "two", "args": {"value": [2]}, "name": "lookup"},
                {"id": "three", "args": {}, "name": "pending"},
            ],
            "tool_responses": [
                {"id": "one", "name": "lookup", "response": {"v": 1}},
                {"id": "two", "name": "lookup", "response": {"result": [3]}},
            ],
            "intermediate_responses": [["routing-agent", [{"text": "Checking the request."}]]],
        },
        "creation_timestamp": 0.0,
    }
    assert json.dumps(invocation_to_dict(invocation)) == json.dumps(expected)


@pytest.mark.parametrize("native", [False, True])
def test_expected_tools_accept_both_native_variants(native):
    invocation = to_adk_invocation(source_turn())
    if not native:
        invocation = invocation.model_validate(invocation_to_dict(invocation))
    expected = expected_from_case(EvalCase(eval_id="synthetic", conversation=[invocation])).turns[0]
    assert [call["id"] for call in expected.tool_calls] == ["one", "two", "three"]
    assert [response["id"] for response in expected.tool_responses] == ["one", "two"]


def test_named_agent_reaches_native_final_prompt_with_captured_metadata():
    turn = source_turn()
    turn.tool_calls = turn.tool_calls[:1]
    turn.intermediate_outputs = []
    invocation = to_adk_invocation(turn)
    invocation.app_details = AppDetails(agent_details={"routing_agent": AgentDetails(name="routing_agent")})
    prepared = _to_invocation_events(invocation)
    metric = build_eval_metric(
        "rubric_based_final_response_quality_v1", None, 1.0, rubrics=["Asks for the missing field."]
    )
    prompt = get_evaluator(metric).format_auto_rater_prompt(prepared, None)
    assert "Which severity?" in prompt and "lookup" in prompt


def test_legacy_single_owner_is_used_without_overwriting_native_authors():
    invocation = to_adk_invocation(source_turn())
    invocation = invocation.model_validate(invocation_to_dict(invocation))
    assert isinstance(invocation.intermediate_data, IntermediateData)
    invocation.app_details = AppDetails(agent_details={"named": AgentDetails(name="named")})
    events = _to_invocation_events(invocation).intermediate_data.invocation_events
    assert all(event.author == "named" for event in events[:-1])
    assert events[-1].author == "routing-agent"
    native = to_adk_invocation(source_turn())
    native.app_details = invocation.app_details
    assert _to_invocation_events(native) is native


def test_legacy_multiple_owners_fail_before_native_judging():
    invocation = to_adk_invocation(source_turn())
    invocation = invocation.model_validate(invocation_to_dict(invocation))
    invocation.app_details = AppDetails(
        agent_details={name: AgentDetails(name=name) for name in ("agent", "specialist")}
    )
    before = invocation.model_dump(mode="json")
    with pytest.raises(ValueError, match="multiple agent owners"):
        _to_invocation_events(invocation)
    assert invocation.model_dump(mode="json") == before


def test_names_only_fallback_groups_owners_and_preserves_captured_declarations():
    invocation = to_adk_invocation(source_turn())
    declaration = types.Tool(
        function_declarations=[types.FunctionDeclaration(name="uncalled", description="Captured.")]
    )
    invocation.app_details = AppDetails(
        agent_details={"routing_agent": AgentDetails(name="routing_agent", tool_declarations=[declaration])}
    )
    before = invocation.model_dump(mode="json")
    (enriched,) = _enrich_app_details([invocation])
    agents = enriched.app_details.agent_details
    assert agents["routing_agent"].tool_declarations == [declaration]
    assert [item.name for item in agents["specialist"].tool_declarations[0].function_declarations] == [
        "lookup",
        "pending",
    ]
    assert invocation.model_dump(mode="json") == before


def test_native_accounting_is_appended_once_without_changing_tool_events():
    from agentevals.adk_bridge import model_call_events
    from agentevals.genai.model import LlmCall, Usage

    turn = source_turn()
    turn.llm_calls = [
        LlmCall(
            ref=turn.ref,
            operation="chat",
            agent_name="routing_agent",
            request_model="synthetic",
            usage=Usage(input_tokens=5),
        )
    ]
    invocation = to_adk_invocation(turn)
    original = invocation.model_dump(mode="json")
    accounting = model_call_events(turn)
    adapted = _to_invocation_events(invocation, accounting)
    assert adapted.intermediate_data.invocation_events[:-1] == invocation.intermediate_data.invocation_events
    assert adapted.intermediate_data.invocation_events[-1] == accounting[0]
    assert _to_invocation_events(adapted, accounting) == adapted
    assert invocation.model_dump(mode="json") == original
