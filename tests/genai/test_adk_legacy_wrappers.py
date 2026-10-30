"""Synthetic nested ADK/OpenLLMetry rounds; no recorded tasks, prompts or results."""

from __future__ import annotations

from copy import deepcopy

from agentevals.genai.extract import extract_conversation
from agentevals.genai.messages import text_of
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from otel.builders import assistant, request, sid, span, user


def synthetic_rounds(mode: str = "valid") -> list:
    spans, history = [], [{"role": "user", "parts": [{"text": "Read four synthetic items."}]}]
    for index in range(3):
        start, outer, leaf = index * 3_000 + 1_000, f"adk-{index}", f"provider-{index}"
        calls = (
            [{"id": f"adk-{index}-{n}", "name": "read_item", "args": {"item": index * 2 + n}} for n in range(2)]
            if index < 2
            else []
        )
        output = {
            "role": "model",
            "parts": [{"function_call": call} for call in calls] if calls else [{"text": "Four items read."}],
        }
        contents = deepcopy(history)
        if mode == "missing_ids":
            for message in contents:
                for part in message["parts"]:
                    if "function_response" in part:
                        part["function_response"].pop("id", None)
        if mode == "conflicting_ids":
            for message in contents:
                for part in message["parts"]:
                    if "function_response" in part:
                        part["function_response"]["id"] = "unrequested-" + part["function_response"]["id"]
        if mode == "conflicting_transition" and index == 1:
            for message in contents:
                for part in message["parts"]:
                    for key in ("function_call", "function_response"):
                        if key in part:
                            part[key]["id"] = "other-" + part[key]["id"]
        if mode == "missing_call_ids":
            output = deepcopy(output)
            for message in [*contents, output]:
                for part in message["parts"]:
                    if "function_call" in part:
                        part["function_call"].pop("id", None)
        attrs = {
            "gcp.vertex.agent.invocation_id": "synthetic-invocation",
            "gcp.vertex.agent.session_id": "synthetic-session",
            "gcp.vertex.agent.llm_request": {"contents": contents},
            "gcp.vertex.agent.llm_response": {"content": output},
        }
        if mode == "partial_adk_pair" and index == 1:
            attrs.pop("gcp.vertex.agent.llm_response")
        if mode == "non_adk_outer":
            attrs = {
                "gen_ai.operation.name": "generate_content",
                "gen_ai.input.messages": [user("Outer unrelated content")],
                "gen_ai.output.messages": [assistant("Outer unrelated reply")],
            }
        spans.append(span(span_id=outer, name="call_llm", attrs=attrs, start=start, end=start + 2_000))
        provider = {
            "gen_ai.request.model": "synthetic-model",
            "gen_ai.system": "openai",
            "gen_ai.prompt.0.role": "user",
            "gen_ai.prompt.0.content": "Read four synthetic items.",
            "gen_ai.completion.0.role": "assistant",
            "gen_ai.completion.0.content": "" if calls else "Four items read.",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 2,
        }
        for n, call in enumerate(calls):
            for key, value in {"id": f"provider-{index}-{n}", "name": call["name"], "arguments": call["args"]}.items():
                provider[f"gen_ai.completion.0.tool_calls.{n}.{key}"] = value
        if mode == "canonical_leaf":
            provider["gen_ai.input.messages"] = [user("Canonical leaf input")]
            provider["gen_ai.output.messages"] = (
                [
                    assistant(
                        tool_calls=[
                            (f"provider-{index}-{n}", call["name"], call["args"]) for n, call in enumerate(calls)
                        ]
                    )
                ]
                if calls
                else [assistant("Canonical leaf answer")]
            )
        spans.append(
            span(span_id=leaf, parent=outer, name="openai.chat", attrs=provider, start=start + 100, end=start + 500)
        )
        for n, call in enumerate(calls):
            spans.append(
                span(
                    span_id=f"tool-{index}-{n}",
                    parent=outer,
                    name="execute_tool read_item",
                    attrs={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": "read_item",
                        "gen_ai.tool.call.id": call["id"],
                        "gen_ai.tool.call.arguments": call["args"],
                        "gen_ai.tool.call.result": {"value": index * 2 + n},
                    },
                    start=start + 600 + n * 100,
                    end=start + 650 + n * 100,
                )
            )
        history.extend(
            [
                output,
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "id": call["id"],
                                "name": call["name"],
                                "response": {"value": index * 2 + n},
                            }
                        }
                        for n, call in enumerate(calls)
                    ],
                },
            ]
        )
    decoded = decode_json_document(request(spans), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    return traces


def test_coherent_adk_rounds_are_one_complete_turn():
    turns = extract_conversation(synthetic_rounds()).turns
    assert len(turns) == 1
    turn = turns[0]
    assert len(turn.llm_calls) == 3 and len(turn.tool_calls) == 4
    assert [call.ref.span_id for call in turn.llm_calls] == [sid(f"provider-{i}") for i in range(3)]
    assert [call.wrapper_ref.span_id for call in turn.llm_calls] == [sid(f"adk-{i}") for i in range(3)]
    assert turn.usage.input_tokens == 30 and turn.usage.output_tokens == 6
    assert [tool.call_id for tool in turn.tool_calls] == [f"adk-{i}-{j}" for i in range(2) for j in range(2)]
    assert [tool.result for tool in turn.tool_calls] == [{"value": i} for i in range(4)]
    assert text_of(turn.final_output) == "Four items read." and not turn.warnings


def test_missing_or_conflicting_adk_ids_do_not_collapse_rounds():
    for mode in ("missing_ids", "missing_call_ids", "conflicting_ids"):
        assert len(extract_conversation(synthetic_rounds(mode)).turns) == 3, mode


def test_internally_coherent_different_ids_still_fail_transition_correlation():
    assert len(extract_conversation(synthetic_rounds("conflicting_transition")).turns) > 1


def test_canonical_leaf_non_adk_and_partial_pairs_preserve_turns():
    for mode in ("canonical_leaf", "non_adk_outer", "partial_adk_pair"):
        assert len(extract_conversation(synthetic_rounds(mode)).turns) == 3, mode
