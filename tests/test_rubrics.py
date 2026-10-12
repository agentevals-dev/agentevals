"""The public path for rubric_based_* metrics: rubrics in the eval config,
on the matched eval case and on its invocations, resolved the way ADK's own
eval service resolves them, with per-rubric scores in the result."""

from __future__ import annotations

import asyncio
import json
import textwrap

import pytest
from google.adk.evaluation.eval_case import EvalCase, Invocation
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent, RubricScore
from google.adk.evaluation.eval_set import EvalSet
from google.adk.evaluation.evaluator import EvalStatus, EvaluationResult, PerInvocationResult
from google.genai import types as genai_types

from agentevals import builtin_metrics
from agentevals.adk_bridge import criterion_rubrics, expected_from_case
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
from agentevals.genai.extract import extract_conversation
from agentevals.runner import EvalRunConfig, run_evaluation
from test_runner import _make_eval_set_json, _make_tool_trace

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
        assert criterion_rubrics(metric.rubrics)[0].type is None

    @pytest.mark.parametrize(
        ("rubrics", "message"),
        [
            ([{"id": "a", "text": "one"}, {"id": "a", "text": "two"}], "unique"),
            (["  "], "blank"),
            ("not a list", "must be a list"),
            ([{"id": "a"}], "text"),
            ([{"id": "a", "text": "one", "weight": 2}], "weight"),
            ([{"id": "a", "text": "one", "type": "FINAL_RESPONSE_QUALITY"}], "type"),
            ([{"id": "  ", "text": "one"}], "blank"),
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
    def test_case_rubrics_reach_every_invocation_without_defaulting_type(self):
        actual = [_invocation("a"), _invocation("b")]
        attached, warnings = attach_case_rubrics(METRIC, actual, None, [_rubric("case")])
        for inv in attached:
            (rubric,) = inv.rubrics
            assert (rubric.rubric_id, rubric.type) == ("case", None)
        assert warnings == ["eval case rubric 'case' ignored: type None; metric requires 'FINAL_RESPONSE_QUALITY'"]
        # The originals are untouched.
        assert all(inv.rubrics is None for inv in actual)

    def test_invocation_rubrics_apply_at_their_position_and_keep_their_type(self):
        actual = [_invocation("a"), _invocation("b")]
        expected = [_invocation("a", [_rubric("first", rubric_type="CUSTOM")]), _invocation("b")]
        attached, warnings = attach_case_rubrics(METRIC, actual, expected, None)
        assert [(r.rubric_id, r.type) for r in attached[0].rubrics] == [("first", "CUSTOM")]
        assert attached[1].rubrics == []
        assert warnings == []

    def test_a_duplicate_id_between_case_and_invocation_is_an_error(self):
        actual = [_invocation("a")]
        expected = [_invocation("a", [_rubric("dup", rubric_type="FINAL_RESPONSE_QUALITY")])]
        with pytest.raises(ValueError, match="distinct"):
            attach_case_rubrics(METRIC, actual, expected, [_rubric("dup", rubric_type="FINAL_RESPONSE_QUALITY")])

    @pytest.mark.parametrize("rubric_type", [None, "TOOL_USE_QUALITY"])
    def test_dropped_rubrics_are_reported_before_judge_construction(self, monkeypatch, rubric_type):
        def unexpected(*args):
            pytest.fail("Judge must not be constructed without an applicable rubric")

        monkeypatch.setattr(builtin_metrics, "get_evaluator", unexpected)
        result = asyncio.run(
            evaluate_builtin_metric(
                METRIC,
                [_invocation("a")],
                None,
                "gemini-2.5-flash",
                0.5,
                case_rubrics=[_rubric("dropped", rubric_type=rubric_type)],
            )
        )
        assert result.score is None and result.eval_status == "NOT_EVALUATED"
        assert "requires rubrics" in result.error
        if rubric_type is None:
            assert "eval case rubric 'dropped' ignored" in result.details["warnings"][0]
        else:
            assert result.details is None

    def test_missing_later_turn_rubrics_prevents_partial_judging(self, monkeypatch):
        monkeypatch.setattr(builtin_metrics, "get_evaluator", lambda *args: pytest.fail("Partial judge call"))
        result = asyncio.run(
            evaluate_builtin_metric(
                METRIC,
                [_invocation("a"), _invocation("b")],
                [_invocation("a", [_rubric("first", rubric_type="FINAL_RESPONSE_QUALITY")]), _invocation("b")],
                "gemini-2.5-flash",
                0.5,
            )
        )
        assert result.score is None and "invocations [1]" in result.error

    def test_duplicate_between_criterion_and_case_is_rejected(self):
        with pytest.raises(ValueError, match="distinct"):
            attach_case_rubrics(
                METRIC,
                [_invocation()],
                None,
                [_rubric("same", rubric_type="FINAL_RESPONSE_QUALITY")],
                [_rubric("same")],
            )

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
        actual = extract_conversation([_make_tool_trace([])])
        asyncio.run(evaluate_custom_evaluator(metric, actual, expected_from_case(case)))
        assert [r.rubric_id for r in seen["rubrics"]] == ["cfg"]
        assert seen["rubrics"][0].type is None
        assert [r.rubric_id for r in seen["case_rubrics"]] == ["case"]


class TestPublicPath:
    @pytest.mark.parametrize(
        ("metric_name", "rubric_type"),
        [(METRIC, "FINAL_RESPONSE_QUALITY"), ("rubric_based_tool_use_quality_v1", "TOOL_USE_QUALITY")],
    )
    @pytest.mark.parametrize("score", [0.0, 1.0, None])
    def test_authored_properties_survive_matching_and_serialization(
        self, tmp_path, monkeypatch, metric_name, rubric_type, score
    ):
        class Evaluator:
            async def evaluate_invocations(self, actual_invocations, expected_invocations):
                actual = actual_invocations[0]
                assert actual.app_details.agent_details
                assert [(r.rubric_id, r.type) for r in actual.rubrics] == [
                    ("case", rubric_type),
                    ("ignored", None),
                    ("turn", rubric_type),
                ]
                status = (
                    EvalStatus.NOT_EVALUATED if score is None else EvalStatus.PASSED if score else EvalStatus.FAILED
                )
                scores = [
                    RubricScore(rubric_id=key, score=score, rationale="observed synthetic evidence")
                    for key in ("cfg", "case", "turn")
                ]
                return EvaluationResult(
                    overall_score=score,
                    overall_eval_status=status,
                    per_invocation_results=[
                        PerInvocationResult(
                            actual_invocation=actual, score=score, eval_status=status, rubric_scores=scores
                        )
                    ],
                    overall_rubric_scores=scores,
                )

        def factory(metric):
            assert [r.rubric_id for r in metric.criterion.rubrics] == ["cfg"]
            return Evaluator()

        monkeypatch.setattr(builtin_metrics, "get_evaluator", factory)
        from agentevals.otel.encode import encode_traces

        trace_path = tmp_path / "trace.json"
        trace_path.write_text(json.dumps(encode_traces([_make_tool_trace(["lookup"])])))
        golden = _make_eval_set_json(["lookup"])
        case = golden["eval_cases"][0]
        case["eval_id"] = "matched-case"
        case["rubrics"] = [_rubric("case", rubric_type=rubric_type).model_dump(), _rubric("ignored").model_dump()]
        case["conversation"][0]["rubrics"] = [_rubric("turn", rubric_type=rubric_type).model_dump()]
        unrelated = json.loads(json.dumps(case))
        unrelated["eval_id"] = "unrelated"
        unrelated["conversation"][0]["user_content"]["parts"][0]["text"] = "other question"
        golden["eval_cases"].insert(0, unrelated)
        golden_path = tmp_path / "golden.json"
        golden_path.write_text(json.dumps(golden))
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"evaluators:\n  - name: {metric_name}\n    type: builtin\n    rubrics:\n      - id: cfg\n        text: Uses captured evidence.\n"
        )
        config = load_eval_config(config_path)
        result = asyncio.run(
            run_evaluation(
                EvalRunConfig(
                    trace_files=[str(trace_path)], eval_set_file=str(golden_path), evaluators=config.evaluators
                )
            )
        )
        payload = json.loads(result.model_dump_json(by_alias=True))
        trace = payload["traceResults"][0]
        metric = trace["metricResults"][0]
        assert trace["evalCaseId"] == "matched-case"
        assert metric["score"] == score
        assert metric["evalStatus"] == ("NOT_EVALUATED" if score is None else "PASSED" if score else "FAILED")
        details = metric["details"]
        assert details["per_invocation"][0]["invocation_id"] == trace["turnRefs"][0]["spanId"]
        assert [
            (r["rubric_id"], r["score"], r["rationale"]) for r in details["per_invocation"][0]["rubric_scores"]
        ] == [(key, score, "observed synthetic evidence") for key in ("cfg", "case", "turn")]
        assert "eval case rubric 'ignored' ignored" in details["warnings"][0]


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
