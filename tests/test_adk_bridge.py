"""Captured tool declarations reach ADK judges without changing exported eval sets."""

from __future__ import annotations

import pytest

from agentevals.adk_bridge import invocation_to_dict, to_adk_invocation
from agentevals.builtin_metrics import _enrich_app_details
from agentevals.genai.model import LlmCall, ToolCall, Turn
from agentevals.otel.model import SpanRef

REF = SpanRef("trace", "turn")


def _call(definitions, agent_name=None):
    return LlmCall(ref=REF, operation="chat", agent_name=agent_name, tool_definitions=definitions)


@pytest.mark.parametrize("schema_type", ["object", "OBJECT"], ids=["json-schema", "adk-schema"])
def test_captured_tools_include_uncalled_tools_and_keep_first_definition(schema_type):
    schema = {"type": schema_type, "properties": {"query": {"type": "string"}}}
    definitions = [
        {"name": "search", "description": "Search the available evidence.", "parameters": schema},
        {"name": "lookup", "description": "Look up an item.", "parameters": {}},
    ]
    turn = Turn(
        ref=REF,
        agent_name="researcher",
        llm_calls=[
            _call(definitions),
            _call([{"name": "search", "description": "Changed", "parameters": {}}, {"name": "finish"}]),
        ],
        tool_calls=[ToolCall(name="search")],
    )

    invocation = to_adk_invocation(turn)

    declarations = invocation.app_details.agent_details["researcher"].tool_declarations[0].function_declarations
    assert [declaration.name for declaration in declarations] == ["search", "lookup", "finish"]
    assert declarations[0].description == definitions[0]["description"]
    assert declarations[0].parameters_json_schema == schema
    assert declarations[0].parameters is None
    assert declarations[1].description == definitions[1]["description"]
    assert declarations[1].parameters_json_schema == {}
    assert declarations[2].description is None
    assert declarations[2].parameters_json_schema is None
    assert _enrich_app_details([invocation])[0] is invocation


@pytest.mark.parametrize(
    "call_agent,turn_agent,expected", [("worker", "root", "worker"), (None, "root", "root"), (None, None, "agent")]
)
def test_tool_agent_name_falls_back_to_turn_then_agent(call_agent, turn_agent, expected):
    turn = Turn(ref=REF, agent_name=turn_agent, llm_calls=[_call([{"name": "search"}], call_agent)])

    assert list(to_adk_invocation(turn).app_details.agent_details) == [expected]


def test_agents_keep_separate_tools_and_do_not_include_system_instructions():
    first = _call([{"name": "lookup", "description": "Search documents."}], "researcher")
    first.system_instructions = [{"type": "text", "content": "Private instructions."}]
    second = _call([{"name": "lookup", "description": "Search billing records."}], "billing")

    invocation = to_adk_invocation(Turn(ref=REF, llm_calls=[first, second]))

    agents = invocation.app_details.agent_details
    assert list(agents) == ["researcher", "billing"]
    assert agents["researcher"].tool_declarations[0].function_declarations[0].description == "Search documents."
    assert agents["billing"].tool_declarations[0].function_declarations[0].description == "Search billing records."
    assert all(details.instructions == "" for details in agents.values())
    assert "app_details" not in invocation_to_dict(invocation)


@pytest.mark.parametrize("calls", [[], [_call([])], [_call([{}, {"name": ""}, {"name": None}, {"name": 7}])]])
def test_missing_or_unusable_definitions_leave_names_only_fallback(calls):
    turn = Turn(ref=REF, llm_calls=calls, tool_calls=[ToolCall(name="search")])
    invocation = to_adk_invocation(turn)

    assert invocation.app_details is None
    (enriched,) = _enrich_app_details([invocation])
    (declaration,) = enriched.app_details.agent_details["agent"].tool_declarations[0].function_declarations
    assert declaration.name == "search"
    assert declaration.description is None
    assert declaration.parameters_json_schema is None
    assert invocation.app_details is None


def test_malformed_description_does_not_break_trace_conversion():
    turn = Turn(ref=REF, llm_calls=[_call([{"name": "search", "description": {"unexpected": "object"}}])])

    (declaration,) = (
        to_adk_invocation(turn).app_details.agent_details["agent"].tool_declarations[0].function_declarations
    )

    assert declaration.name == "search"
    assert declaration.description is None
