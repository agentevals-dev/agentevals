"""The public path for rubric_based_* metrics: rubrics in the eval config,
on the matched eval case and on its invocations, resolved the way ADK's own
eval service resolves them, with per-rubric scores in the result."""

from __future__ import annotations

import asyncio
import textwrap

import pytest
from google.adk.evaluation.eval_case import EvalCase, Invocation
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent, RubricScore
from google.adk.evaluation.eval_set import EvalSet
from google.adk.evaluation.evaluator import EvalStatus, EvaluationResult, PerInvocationResult
from google.genai import types as genai_types

from agentevals import builtin_metrics
from agentevals.builtin_metrics import (
    attach_case_rubrics,
    build_eval_metric,
    evaluate_builtin_metric,
    extract_rubric_details,
    to_rubric_objects,
)
from agentevals.config import BuiltinMetricDef, RubricDef, apply_builtin_overrides
from agentevals.custom_evaluators import evaluate_custom_evaluator
from agentevals.eval_config_loader import load_eval_config
from agentevals.runner import _find_eval_case, _find_expected_invocations

METRIC = "rubric_based_final_response_quality_v1"


def _rubric(rubric_id: str, text: str = "a statement", rubric_type: str | None = None) -> Rubric:
    return Rubric(rubric_id=rubric_id, rubric_content=RubricContent(text_property=text), type=rubric_type)


def _invocation(text: str = "hi", rubrics: list[Rubric] | None = None) -> Invocation:
    return Invocation(
        invocation_id=text,
        user_content=genai_types.Content(role="user", parts=[genai_types.Part(text=text)]),
        final_response=genai_types.Content(role="model", parts=[genai_types.Part(text="ok")]),
        rubrics=rubrics,
    )


class TestConfig:
    def test_rubrics_accept_mappings_and_strings(self):
        metric = BuiltinMetricDef.model_validate(
            {
                "name": METRIC,
                "type": "builtin",
                "rubrics": [{"id": "grounded", "text": "No invented cause."}, "Uses the latest correction."],
            }
        )
        assert [(r.id, r.text) for r in metric.rubrics] == [
            ("grounded", "No invented cause."),
            ("rubric_1", "Uses the latest correction."),
        ]
        assert metric.rubrics[0].to_adk("FINAL_RESPONSE_QUALITY").type == "FINAL_RESPONSE_QUALITY"
        assert RubricDef(id="x", text="t", type="TOOL_USE_QUALITY").to_adk("FINAL_RESPONSE_QUALITY").type == (
            "TOOL_USE_QUALITY"
        )

    @pytest.mark.parametrize(
        ("rubrics", "message"),
        [
            ([{"id": "a", "text": "one"}, {"id": "a", "text": "two"}], "unique"),
            (["  "], "empty"),
            ("not a list", "must be a list"),
            ([{"id": "a"}], "text"),
            ([{"id": "a", "text": "one", "weight": 2}], "weight"),
        ],
    )
    def test_malformed_rubrics_are_refused(self, rubrics, message):
        with pytest.raises(ValueError, match=message):
            BuiltinMetricDef.model_validate({"name": METRIC, "type": "builtin", "rubrics": rubrics})

    def test_rubrics_survive_run_level_overrides(self):
        metric = BuiltinMetricDef(name=METRIC, rubrics=[RubricDef(id="a", text="t")])
        (updated,) = apply_builtin_overrides([metric], judge_model="gemini-2.5-flash", threshold=0.9)
        assert updated.judge_model == "gemini-2.5-flash"
        assert [r.id for r in updated.rubrics] == ["a"]

    def test_loader_reads_rubrics_from_yaml(self, tmp_path):
        config_file = tmp_path / "eval_config.yaml"
        config_file.write_text(
            textwrap.dedent(
                f"""
                evaluators:
                  - name: {METRIC}
                    type: builtin
                    judge_model: gemini-2.5-flash
                    rubrics:
                      - id: corrected_value_wins
                        text: The response uses the operator's latest correction.
                      - The response states no root cause the conversation did not establish.
                """
            ),
            encoding="utf-8",
        )
        (metric,) = load_eval_config(config_file).evaluators
        assert isinstance(metric, BuiltinMetricDef)
        assert [r.id for r in metric.rubrics] == ["corrected_value_wins", "rubric_1"]


class TestCriterion:
    def test_strings_and_objects_both_reach_the_criterion(self):
        metric = build_eval_metric(METRIC, "gemini-2.5-flash", 0.5, rubrics=["plain", _rubric("named", "typed")])
        rubrics = metric.criterion.rubrics
        assert [(r.rubric_id, r.rubric_content.text_property) for r in rubrics] == [
            ("rubric_0", "plain"),
            ("named", "typed"),
        ]
        assert to_rubric_objects(None) == []


class TestCaseRubrics:
    def test_case_rubrics_reach_every_invocation_and_get_the_metrics_type(self):
        actual = [_invocation("a"), _invocation("b")]
        attached = attach_case_rubrics(METRIC, actual, None, [_rubric("case")])
        for inv in attached:
            (rubric,) = inv.rubrics
            assert (rubric.rubric_id, rubric.type) == ("case", "FINAL_RESPONSE_QUALITY")
        # The originals are untouched.
        assert all(inv.rubrics is None for inv in actual)

    def test_invocation_rubrics_apply_at_their_position_and_keep_their_type(self):
        actual = [_invocation("a"), _invocation("b")]
        expected = [_invocation("a", [_rubric("first", rubric_type="CUSTOM")]), _invocation("b")]
        attached = attach_case_rubrics(METRIC, actual, expected, None)
        assert [(r.rubric_id, r.type) for r in attached[0].rubrics] == [("first", "CUSTOM")]
        assert attached[1].rubrics is None

    def test_a_duplicate_id_between_case_and_invocation_is_an_error(self):
        actual = [_invocation("a")]
        expected = [_invocation("a", [_rubric("dup")])]
        with pytest.raises(ValueError, match="defined more than once"):
            attach_case_rubrics(METRIC, actual, expected, [_rubric("dup")])

    def test_a_rubric_metric_without_any_rubric_is_an_error_before_any_judge_call(self):
        result = asyncio.run(
            evaluate_builtin_metric(
                METRIC,
                [_invocation("a")],
                None,
                judge_model="gemini-2.5-flash",
                threshold=0.5,
            )
        )
        assert result.score is None
        assert "requires rubrics" in result.error

    def test_the_dispatch_passes_config_and_case_rubrics(self, monkeypatch):
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            from agentevals.runner import MetricResult

            return MetricResult(metric_name=kwargs["metric_name"], score=1.0, eval_status="PASSED")

        monkeypatch.setattr(builtin_metrics, "evaluate_builtin_metric", fake)
        case = EvalCase(eval_id="c", conversation=[_invocation("a")], rubrics=[_rubric("case")])
        metric = BuiltinMetricDef(name=METRIC, rubrics=[RubricDef(id="cfg", text="t")])
        asyncio.run(evaluate_custom_evaluator(metric, [_invocation("a")], case.conversation, eval_case=case))
        assert [r.rubric_id for r in seen["rubrics"]] == ["cfg"]
        assert seen["rubrics"][0].type == "FINAL_RESPONSE_QUALITY"
        assert [r.rubric_id for r in seen["case_rubrics"]] == ["case"]


class TestMatching:
    def test_the_matched_case_carries_its_rubrics(self):
        eval_set = EvalSet(
            eval_set_id="s",
            eval_cases=[
                EvalCase(eval_id="one", conversation=[_invocation("first question")]),
                EvalCase(eval_id="two", conversation=[_invocation("second question")], rubrics=[_rubric("r")]),
            ],
        )
        case = _find_eval_case([_invocation("second question")], eval_set)
        assert case.eval_id == "two" and [r.rubric_id for r in case.rubrics] == ["r"]
        assert _find_expected_invocations([_invocation("second question")], eval_set) is case.conversation
        assert _find_eval_case([], EvalSet(eval_set_id="e", eval_cases=[])) is None


class TestDetails:
    def test_per_rubric_scores_are_reported_per_invocation_and_overall(self):
        actual = _invocation("a")
        result = EvaluationResult(
            overall_score=0.5,
            overall_eval_status=EvalStatus.FAILED,
            per_invocation_results=[
                PerInvocationResult(
                    actual_invocation=actual,
                    score=0.5,
                    eval_status=EvalStatus.FAILED,
                    rubric_scores=[
                        RubricScore(rubric_id="grounded", score=1.0, rationale="no cause was invented"),
                        RubricScore(rubric_id="corrected", score=0.0, rationale="the stale value is current"),
                    ],
                )
            ],
            overall_rubric_scores=[
                RubricScore(rubric_id="grounded", score=1.0),
                RubricScore(rubric_id="corrected", score=0.0),
            ],
        )
        details = extract_rubric_details(result)
        assert details["per_invocation"] == [
            {
                "invocation_id": "a",
                "rubric_scores": [
                    {"rubric_id": "grounded", "score": 1.0, "rationale": "no cause was invented"},
                    {"rubric_id": "corrected", "score": 0.0, "rationale": "the stale value is current"},
                ],
            }
        ]
        assert [r["rubric_id"] for r in details["overall_rubric_scores"]] == ["grounded", "corrected"]
