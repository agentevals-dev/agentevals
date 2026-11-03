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

from google.adk.evaluation.eval_case import (
    EvalCase,
    IntermediateData,
    Invocation,
    InvocationEvent,
    InvocationEvents,
    get_all_tool_calls,
    get_all_tool_responses,
)
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types as genai_types

from .config import RubricDef
from .genai.matching import ExpectedConversation, ExpectedTurn
from .genai.messages import text_of
from .genai.model import Turn, Usage


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
        calls = tuple({"id": c.id, "name": c.name, "arguments": c.args} for c in get_all_tool_calls(data))
        responses = tuple({"id": r.id, "name": r.name, "response": r.response} for r in get_all_tool_responses(data))
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


def criterion_rubrics(definitions: list[RubricDef] | None) -> list[Rubric]:
    return [Rubric(rubric_id=r.id, rubric_content=RubricContent(text_property=r.text)) for r in definitions or []]


def expected_rubrics(expected: ExpectedConversation | None) -> list[Rubric]:
    if expected is None or not isinstance(expected.raw, EvalCase):
        return []
    return list(expected.raw.rubrics or [])


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
    events = []
    for tool in turn.tool_calls:
        author = tool.agent_name or turn.agent_name or "agent"
        events.append(
            InvocationEvent(
                author=author,
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(
                                id=tool.call_id, name=tool.name, args=_as_dict(tool.arguments, "value")
                            )
                        )
                    ],
                ),
            )
        )
        if tool.result is not None:
            events.append(
                InvocationEvent(
                    author=author,
                    content=genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part(
                                function_response=genai_types.FunctionResponse(
                                    id=tool.call_id, name=tool.name, response=_as_dict(tool.result, "result")
                                )
                            )
                        ],
                    ),
                )
            )
    events.extend(
        InvocationEvent(
            author=turn.agent_name or "agent",
            content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
        )
        for text in (text_of([m]) for m in turn.intermediate_outputs)
        if text
    )
    return Invocation(
        invocation_id=turn.ref.span_id,
        user_content=genai_types.Content(role="user", parts=[genai_types.Part(text=user_text or "")]),
        final_response=genai_types.Content(role="model", parts=[genai_types.Part(text=final_text)])
        if final_text
        else None,
        intermediate_data=InvocationEvents(invocation_events=events),
        creation_timestamp=turn.start_ns / 1e9,
        **_durations(turn),
    )


def to_adk_invocations(turns: Sequence[Turn]) -> list[Invocation]:
    return [to_adk_invocation(t) for t in turns]


Interval = tuple[int, int]


def _union(intervals: Sequence[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _length(intervals: Sequence[Interval]) -> int:
    return sum(end - start for start, end in intervals)


def _overlap(a: Sequence[Interval], b: Sequence[Interval]) -> int:
    return sum(max(0, min(e1, e2) - max(s1, s2)) for s1, e1 in a for s2, e2 in b)


def _clipped(spans: Sequence[tuple[int | None, int | None]], turn: Interval) -> list[Interval] | None:
    """The spans' intervals clipped to the turn, or None when any span was not timed."""
    out = []
    for start, end in spans:
        if not start or not end or end < start:
            return None
        out.append((max(start, turn[0]), min(end, turn[1])))
    return _union([(s, e) for s, e in out if e > s])


def _durations(turn: Turn) -> dict[str, float | None]:
    """ADK's turn timing: the total, the time a model call was in flight, and the time inside a
    tool not awaiting a model. A part with an untimed span is None, which ADK reports as n/a."""
    if not turn.start_ns or turn.end_ns <= turn.start_ns:
        return {}
    bounds = (turn.start_ns, turn.end_ns)
    model = _clipped([(c.leaf_start_ns, c.leaf_end_ns) for c in turn.llm_calls], bounds)
    tool = _clipped([(t.start_ns, t.end_ns) for t in turn.tool_calls], bounds)
    tool_ns = None if tool is None or model is None else _length(tool) - _overlap(tool, model)
    return {
        "duration": (turn.end_ns - turn.start_ns) / 1e9,
        "model_duration": None if model is None else _length(model) / 1e9,
        "tool_duration": None if tool_ns is None else tool_ns / 1e9,
    }


def _usage_metadata(usage: Usage | None) -> genai_types.GenerateContentResponseUsageMetadata | None:
    """Semconv usage in Gemini's shape. Semconv output tokens include reasoning, while Gemini
    reports candidates and thoughts apart, so reasoning is taken back out of the output count."""
    if usage is None or all(v is None for v in usage.model_dump().values()):
        return None
    output, reasoning = usage.output_tokens, usage.reasoning_output_tokens
    candidates = output - reasoning if output is not None and reasoning is not None and reasoning <= output else output
    return genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=usage.input_tokens,
        candidates_token_count=candidates,
        thoughts_token_count=reasoning,
        cached_content_token_count=usage.cache_read_input_tokens,
    )


def model_call_events(turn: Turn) -> list[InvocationEvent]:
    """One content-less event per model call, the shape ADK's token and inference call counts
    read. ADK counts an event as a model call when it has usage or a model version."""
    return [
        InvocationEvent(
            author=call.agent_name or turn.agent_name or "agent",
            usage_metadata=_usage_metadata(call.usage),
            model_version=call.response_model or call.request_model or "",
        )
        for call in turn.llm_calls
    ]


def invocation_to_dict(inv: Invocation) -> dict[str, Any]:
    """ADK invocation as snake_case JSON, the shape eval set cases and ``/api/convert`` carry."""
    out: dict[str, Any] = {"invocation_id": inv.invocation_id}
    if inv.user_content is not None:
        out["user_content"] = inv.user_content.model_dump(mode="json", exclude_none=True)
    if inv.final_response is not None:
        out["final_response"] = inv.final_response.model_dump(mode="json", exclude_none=True)
    if inv.intermediate_data is not None:
        data = inv.intermediate_data
        if isinstance(data, InvocationEvents):
            # Export keeps the existing flat JSON; scoring retains the source event owners.
            data = IntermediateData(
                tool_uses=get_all_tool_calls(data),
                tool_responses=get_all_tool_responses(data),
                intermediate_responses=[
                    (event.author, parts)
                    for event in data.invocation_events
                    if event.content
                    and (
                        parts := [
                            part
                            for part in event.content.parts or []
                            if not part.function_call and not part.function_response
                        ]
                    )
                ],
            )
        out["intermediate_data"] = data.model_dump(mode="json", exclude_none=True)
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
