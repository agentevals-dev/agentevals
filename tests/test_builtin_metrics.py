"""Tests for the IntermediateData -> InvocationEvents bridge in front of ADK evaluators.

Several ADK evaluators (``hallucinations_v1``, the multi-turn Vertex metrics) only read
tool evidence from ``InvocationEvents``. Trace converters produce ``IntermediateData``, so
without the bridge the hallucinations judge never sees tool outputs and labels grounded
claims as unsupported (agentevals-dev/agentevals#230).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator

import pytest
from google.adk.evaluation.eval_case import (
    IntermediateData,
    Invocation,
    InvocationEvents,
    get_all_tool_responses,
)
from google.adk.evaluation.eval_metrics import PrebuiltMetrics
from google.adk.evaluation.evaluator import EvaluationResult
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types

from agentevals import builtin_metrics
from agentevals.builtin_metrics import _to_invocation_events, evaluate_builtin_metric
from agentevals.converter import convert_traces
from agentevals.loader import load_traces

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")
HELM_TRACE = os.path.join(SAMPLES_DIR, "helm.json")


def _invocation(
    calls: list[tuple[str, str | None]],
    responses: list[tuple[str, str | None, str]],
    final_text: str = "done",
) -> Invocation:
    return Invocation(
        invocation_id="inv-1",
        user_content=genai_types.Content(role="user", parts=[genai_types.Part(text="question")]),
        final_response=genai_types.Content(role="model", parts=[genai_types.Part(text=final_text)]),
        intermediate_data=IntermediateData(
            tool_uses=[genai_types.FunctionCall(name=name, id=id_, args={}) for name, id_ in calls],
            tool_responses=[
                genai_types.FunctionResponse(name=name, id=id_, response={"value": value})
                for name, id_, value in responses
            ],
        ),
    )


def _event_sequence(inv: Invocation) -> list[str]:
    seq = []
    for event in inv.intermediate_data.invocation_events:
        part = event.content.parts[0]
        if part.function_call:
            seq.append(f"call:{part.function_call.name}")
        elif part.function_response:
            seq.append(f"resp:{part.function_response.name}={part.function_response.response['value']}")
        else:
            seq.append(f"text:{part.text}")
    return seq


PAIRING_CASES = [
    pytest.param(
        [("a", "1"), ("b", "2")],
        [("b", "2", "B"), ("a", "1", "A")],
        ["call:a", "resp:a=A", "call:b", "resp:b=B"],
        id="ids-match-out-of-order",
    ),
    pytest.param(
        [("get_weather", "get_weather_1")],
        [("get_weather", "adk-0f3c", "sunny")],
        ["call:get_weather", "resp:get_weather=sunny"],
        id="id-mismatch-falls-back-to-name",
    ),
    pytest.param(
        [("a", None), ("b", None)],
        [("b", None, "B")],
        ["call:a", "call:b", "resp:b=B"],
        id="no-ids-response-not-attached-to-wrong-call",
    ),
    pytest.param(
        [("a", None), ("a", None)],
        [("a", None, "first"), ("a", None, "second")],
        ["call:a", "resp:a=first", "call:a", "resp:a=second"],
        id="repeated-tool-keeps-order",
    ),
    pytest.param(
        [("a", "1")],
        [("a", "1", "A"), ("z", None, "Z")],
        ["call:a", "resp:a=A", "resp:z=Z"],
        id="orphan-response-appended",
    ),
    pytest.param(
        [("a", "1"), ("a", "2")],
        [("a", "2", "second")],
        ["call:a", "call:a", "resp:a=second"],
        id="response-owned-by-another-call-not-stolen-by-name",
    ),
    pytest.param(
        [],
        [("a", "1", "A")],
        ["resp:a=A"],
        id="responses-without-any-calls",
    ),
]


class TestToInvocationEvents:
    @pytest.mark.parametrize("calls,responses,expected", PAIRING_CASES)
    def test_pairing(self, calls, responses, expected):
        adapted = _to_invocation_events(_invocation(calls, responses))

        assert isinstance(adapted.intermediate_data, InvocationEvents)
        assert _event_sequence(adapted) == expected

    @pytest.mark.parametrize("calls,responses,expected", PAIRING_CASES)
    def test_never_drops_tool_responses(self, calls, responses, expected):
        inv = _invocation(calls, responses)

        adapted = _to_invocation_events(inv)

        before = get_all_tool_responses(inv.intermediate_data)
        after = get_all_tool_responses(adapted.intermediate_data)
        assert sorted(r.response["value"] for r in after) == sorted(r.response["value"] for r in before)

    def test_events_follow_adk_runtime_roles(self):
        adapted = _to_invocation_events(_invocation([("a", "1")], [("a", "1", "A")]))

        call_event, response_event = adapted.intermediate_data.invocation_events
        assert (call_event.author, call_event.content.role) == ("agent", "model")
        assert (response_event.author, response_event.content.role) == ("agent", "user")

    def test_intermediate_responses_preserved(self):
        inv = _invocation([("a", "1")], [("a", "1", "A")])
        inv.intermediate_data.intermediate_responses = [("planner", [genai_types.Part(text="thinking")])]

        adapted = _to_invocation_events(inv)

        assert _event_sequence(adapted) == ["call:a", "resp:a=A", "text:thinking"]
        assert adapted.intermediate_data.invocation_events[-1].author == "planner"

    def test_does_not_mutate_input(self):
        inv = _invocation([("a", "1")], [("a", "1", "A")])

        _to_invocation_events(inv)

        assert isinstance(inv.intermediate_data, IntermediateData)
        assert len(inv.intermediate_data.tool_responses) == 1

    def test_passthrough_for_invocation_events_and_none(self):
        already = _to_invocation_events(_invocation([("a", "1")], [("a", "1", "A")]))
        empty = _invocation([], []).model_copy(update={"intermediate_data": None})

        assert _to_invocation_events(already) is already
        assert _to_invocation_events(empty) is empty


_SENTENCE_BLOCK = re.compile(r"<sentence>.*?</sentence>", re.DOTALL)


class _GroundedFakeJudge(BaseLlm):
    """Deterministic stand-in for the hallucinations judge.

    Treats the whole response as one sentence and labels it ``supported`` only when
    ``evidence`` occurs in the validator prompt outside the sentence blocks, i.e. in the
    context ADK built from the invocation.
    """

    evidence: str
    validator_contexts: list[str] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        prompt = llm_request.contents[0].parts[0].text
        if "label:" not in prompt:
            text = "<sentence>The response.</sentence>"
        else:
            context = _SENTENCE_BLOCK.sub("", prompt)
            self.validator_contexts.append(context)
            label = "supported" if self.evidence in context else "unsupported"
            text = (
                f"sentence: The response.\nlabel: {label}\nrationale: fake\n"
                "supporting_excerpt: null\ncontradicting_excerpt: null"
            )
        yield LlmResponse(content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]))


@pytest.fixture
def fake_judge(monkeypatch):
    judges: list[_GroundedFakeJudge] = []
    real_get_evaluator = builtin_metrics.get_evaluator

    def install(evidence: str) -> list[_GroundedFakeJudge]:
        def get_evaluator(eval_metric):
            evaluator = real_get_evaluator(eval_metric)
            judge = _GroundedFakeJudge(model="fake-judge", evidence=evidence)
            evaluator._judge_model = judge
            judges.append(judge)
            return evaluator

        monkeypatch.setattr(builtin_metrics, "get_evaluator", get_evaluator)
        return judges

    return install


class TestHallucinationsSeesToolEvidence:
    async def test_claim_backed_only_by_tool_output_is_supported(self, fake_judge):
        judges = fake_judge(evidence="ROLL-RESULT-17")
        inv = _invocation(
            [("roll_die", "call_1")],
            [("roll_die", "call_1", "ROLL-RESULT-17")],
            final_text="You rolled ROLL-RESULT-17.",
        )

        result = await evaluate_builtin_metric("hallucinations_v1", [inv], None, None, 0.5)

        assert result.error is None
        assert result.score == 1.0
        assert result.eval_status == "PASSED"
        (context,) = judges[0].validator_contexts
        assert "tool_outputs:" in context
        assert "Agent has no tools." not in context

    async def test_claim_missing_from_tool_output_is_unsupported(self, fake_judge):
        fake_judge(evidence="ROLL-RESULT-17")
        inv = _invocation(
            [("roll_die", "call_1")],
            [("roll_die", "call_1", "ROLL-RESULT-4")],
            final_text="You rolled ROLL-RESULT-17.",
        )

        result = await evaluate_builtin_metric("hallucinations_v1", [inv], None, None, 0.5)

        assert result.score == 0.0
        assert result.eval_status == "FAILED"

    async def test_evidence_kept_when_call_and_response_ids_disagree(self, fake_judge):
        fake_judge(evidence="sunny-and-21C")
        inv = _invocation(
            [("get_weather", "get_weather_1")],
            [("get_weather", "adk-0f3c", "sunny-and-21C")],
        )

        result = await evaluate_builtin_metric("hallucinations_v1", [inv], None, None, 0.5)

        assert result.score == 1.0

    @pytest.mark.skipif(not os.path.exists(HELM_TRACE), reason="samples/helm.json not available")
    async def test_replayed_trace_tool_output_reaches_judge(self, fake_judge):
        judges = fake_judge(evidence="kagent-crds-0.7.14")
        (conversion,) = convert_traces(load_traces(HELM_TRACE))
        inv = conversion.invocations[0]
        assert isinstance(inv.intermediate_data, IntermediateData)
        assert "kagent-crds-0.7.14" in str(inv.intermediate_data.tool_responses[0].response)

        result = await evaluate_builtin_metric("hallucinations_v1", [inv], None, None, 0.5)

        assert result.score == 1.0
        assert "kagent-crds-0.7.14" in judges[0].validator_contexts[0]


class _RecordingEvaluator:
    def __init__(self):
        self.actual: list[Invocation] | None = None
        self.expected: list[Invocation] | None = None

    async def evaluate_invocations(self, actual_invocations, expected_invocations=None):
        self.actual = actual_invocations
        self.expected = expected_invocations
        return EvaluationResult()


@pytest.mark.parametrize("metric_name", [m.value for m in PrebuiltMetrics])
async def test_every_prebuilt_metric_receives_invocation_events(monkeypatch, metric_name):
    recorder = _RecordingEvaluator()
    monkeypatch.setattr(builtin_metrics, "get_evaluator", lambda _metric: recorder)
    actual = _invocation([("a", "1")], [("a", "1", "A")])
    expected = _invocation([("a", "1")], [])

    result = await evaluate_builtin_metric(metric_name, [actual], [expected], None, 0.5)

    assert result.error is None
    for inv in [*recorder.actual, *recorder.expected]:
        assert isinstance(inv.intermediate_data, InvocationEvents)
        assert inv.app_details is not None
