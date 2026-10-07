"""The single seam between agentevals' canonical model and Google ADK evaluation types.

Golden eval sets stay ADK ``EvalSet`` JSON, and the built-in metrics are ADK evaluators. This
module turns canonical :class:`~agentevals.genai.model.Turn` objects into ADK ``Invocation``
objects for those metrics and for the ``/api/convert`` wire shape, and turns ADK eval cases
into :class:`~agentevals.genai.matching.ExpectedConversation` for everything else. Outside the
metric registry listings, no other module imports ``google.adk`` or ``google.genai``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from google.adk.evaluation.eval_case import EvalCase, IntermediateData, Invocation
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types as genai_types

from .genai.matching import ExpectedConversation, ExpectedTurn
from .genai.messages import text_of
from .genai.model import Turn


def load_eval_set(path: str) -> EvalSet:
    with open(path, encoding="utf-8") as f:
        return EvalSet.model_validate(json.load(f))


def load_eval_set_from_dict(data: dict) -> EvalSet:
    return EvalSet.model_validate(data)


def _content_text(content: genai_types.Content | None) -> str | None:
    if content is None or not content.parts:
        return None
    text = "".join(p.text for p in content.parts if p.text)
    return text or None


def expected_from_case(case: EvalCase) -> ExpectedConversation:
    turns = []
    for inv in case.conversation or []:
        data = inv.intermediate_data
        calls: tuple[dict[str, Any], ...] = ()
        responses: tuple[dict[str, Any], ...] = ()
        if isinstance(data, IntermediateData):
            calls = tuple({"id": c.id, "name": c.name, "arguments": c.args} for c in data.tool_uses)
            responses = tuple({"id": r.id, "name": r.name, "response": r.response} for r in data.tool_responses)
        turns.append(
            ExpectedTurn(
                user_text=_content_text(inv.user_content),
                final_text=_content_text(inv.final_response),
                tool_calls=calls,
                tool_responses=responses,
                invocation_id=inv.invocation_id,
            )
        )
    return ExpectedConversation(case_id=case.eval_id, turns=turns, raw=case)


def expected_conversations(eval_set: EvalSet | None) -> list[ExpectedConversation]:
    if eval_set is None:
        return []
    return [expected_from_case(case) for case in eval_set.eval_cases if case.conversation]


def expected_invocations(expected: ExpectedConversation | None) -> list[Invocation] | None:
    if expected is None or not isinstance(expected.raw, EvalCase):
        return None
    return list(expected.raw.conversation or [])


def multi_turn_cases(eval_set: EvalSet | None) -> bool:
    return any(len(case.conversation or []) > 1 for case in (eval_set.eval_cases if eval_set else []))


def _as_dict(value: Any, key: str) -> dict[str, Any]:
    """ADK function call args and responses are dicts; other JSON values are wrapped."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {key: value}


def to_adk_invocation(turn: Turn) -> Invocation:
    user_text = text_of(turn.user_input)
    final_text = text_of(turn.final_output)
    tool_uses = [
        genai_types.FunctionCall(id=t.call_id, name=t.name, args=_as_dict(t.arguments, "value"))
        for t in turn.tool_calls
    ]
    tool_responses = [
        genai_types.FunctionResponse(id=t.call_id, name=t.name, response=_as_dict(t.result, "result"))
        for t in turn.tool_calls
        if t.result is not None
    ]
    intermediate = [
        (turn.agent_name or "agent", [genai_types.Part(text=text)])
        for text in (text_of([m]) for m in turn.intermediate_outputs)
        if text
    ]
    return Invocation(
        invocation_id=turn.ref.span_id,
        user_content=genai_types.Content(role="user", parts=[genai_types.Part(text=user_text or "")]),
        final_response=genai_types.Content(role="model", parts=[genai_types.Part(text=final_text)])
        if final_text
        else None,
        intermediate_data=IntermediateData(
            tool_uses=tool_uses,
            tool_responses=tool_responses,
            intermediate_responses=intermediate,
        ),
        creation_timestamp=turn.start_ns / 1e9,
    )


def to_adk_invocations(turns: Sequence[Turn]) -> list[Invocation]:
    return [to_adk_invocation(t) for t in turns]


def invocation_to_dict(inv: Invocation) -> dict[str, Any]:
    """ADK invocation as snake_case JSON, the shape eval set cases and ``/api/convert`` carry."""
    out: dict[str, Any] = {"invocation_id": inv.invocation_id}
    if inv.user_content is not None:
        out["user_content"] = inv.user_content.model_dump(mode="json", exclude_none=True)
    if inv.final_response is not None:
        out["final_response"] = inv.final_response.model_dump(mode="json", exclude_none=True)
    if inv.intermediate_data is not None:
        out["intermediate_data"] = inv.intermediate_data.model_dump(mode="json", exclude_none=True)
    if inv.creation_timestamp is not None:
        out["creation_timestamp"] = inv.creation_timestamp
    return out


def eval_set_from_turns(eval_set_id: str, turns: Sequence[Turn], case_id: str = "case_1") -> dict[str, Any]:
    """A one case ADK EvalSet built from recorded turns, as JSON."""
    return {
        "eval_set_id": eval_set_id,
        "eval_cases": [
            {"eval_id": case_id, "conversation": [invocation_to_dict(i) for i in to_adk_invocations(turns)]}
        ],
    }
