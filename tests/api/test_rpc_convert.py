"""Conversions between the v1alpha1 messages and the Pydantic models."""

from __future__ import annotations

import math
import typing
from datetime import UTC, datetime
from uuid import uuid4

import grpc
import pytest
from google.adk.evaluation.eval_metrics import ToolTrajectoryCriterion
from google.adk.evaluation.evaluator import EvalStatus

from agentevals.api.rpc import convert
from agentevals.api.rpc.policy import ApiError
from agentevals.api.v1alpha1 import common_pb2, evaluation_pb2, runs_pb2
from agentevals.config import BuiltinMetricDef, CodeEvaluatorDef, EvalParams, GroupBy, RemoteEvaluatorDef
from agentevals.runner import MetricResult
from agentevals.storage.models import Result, ResultStatus, Run, RunSpec, RunStatus, TraceTarget


def _proto_values(enum) -> set[int]:
    return {v.number for v in enum.DESCRIPTOR.values if not v.name.endswith("_UNSPECIFIED")}


class TestEnumMaps:
    def test_run_status(self):
        assert set(convert.RUN_STATUS) == set(RunStatus)
        assert set(convert.RUN_STATUS.values()) == _proto_values(runs_pb2.RunStatus)

    def test_result_status(self):
        assert set(convert.RESULT_STATUS) == set(ResultStatus)
        assert set(convert.RESULT_STATUS.values()) == _proto_values(runs_pb2.ResultStatus)

    def test_evaluator_type(self):
        assert set(convert.EVALUATOR_TYPE) == set(typing.get_args(Result.model_fields["evaluator_type"].annotation))
        assert set(convert.EVALUATOR_TYPE.values()) == _proto_values(runs_pb2.EvaluatorType)

    def test_eval_status(self):
        assert set(convert.EVAL_STATUS) == {s.name for s in EvalStatus}
        assert set(convert.EVAL_STATUS.values()) == _proto_values(evaluation_pb2.EvalStatus)

    def test_group_by(self):
        assert set(convert.GROUP_BY) == set(typing.get_args(GroupBy))
        assert set(convert.GROUP_BY.values()) == _proto_values(common_pb2.GroupBy)

    def test_trajectory_match_type(self):
        assert set(convert.TRAJECTORY_MATCH) == {m.name for m in ToolTrajectoryCriterion.MatchType}
        assert set(convert.TRAJECTORY_MATCH.values()) == _proto_values(evaluation_pb2.TrajectoryMatchType)

    def test_trace_format(self):
        [literal, _none] = typing.get_args(TraceTarget.model_fields["trace_format"].annotation)
        assert set(convert.TRACE_FORMAT) == set(typing.get_args(literal))
        assert set(convert.TRACE_FORMAT_FROM_PB) == {v.number for v in common_pb2.TraceFormat.DESCRIPTOR.values}


class TestStruct:
    def test_whole_numbers_come_back_as_ints(self):
        data = {"count": 3, "ratio": 0.25, "nested": [1, {"n": -2}], "flag": True, "none": None}
        back = convert.from_struct(convert.to_struct(data))
        assert back == data
        assert type(back["count"]) is int and type(back["nested"][1]["n"]) is int
        assert type(back["ratio"]) is float and type(back["flag"]) is bool

    def test_values_a_double_cannot_hold_exactly_become_text(self):
        back = convert.from_struct(convert.to_struct({"big": 2**60, "nan": math.nan, "raw": b"\x00\x01"}))
        assert back == {"big": str(2**60), "nan": None, "raw": "AAE="}


class TestEvalParams:
    def test_round_trip(self):
        params = EvalParams(
            evaluators=[
                BuiltinMetricDef(name="tool_trajectory_avg_score", threshold=0.8, trajectory_match_type="IN_ORDER"),
                BuiltinMetricDef(
                    name="final_response_match_v2",
                    judge_model="gemini-2.5-flash",
                    credential_ref="judge",
                    judge_base_url="https://llm.example.com/v1",
                ),
                CodeEvaluatorDef(name="length", path="evals/length.py", threshold=0.4, timeout=12, config={"max": 3}),
                RemoteEvaluatorDef(name="tone", source="github", ref="evaluators/tone/tone.py"),
            ],
            max_concurrent_traces=2,
            max_concurrent_evals=3,
            group_by="conversation",
        )
        back = convert.eval_params_from_pb(convert.eval_params_to_pb(params))
        assert back == params
        assert type(back.evaluators[2].config["max"]) is int

    def test_unset_fields_keep_their_defaults(self):
        assert convert.eval_params_from_pb(evaluation_pb2.EvalParams()) == EvalParams()

    def test_stored_parameters_with_unknown_keys_still_convert(self):
        stored = {**EvalParams().model_dump(), "trace_files": ["/tmp/x.json"]}
        assert convert.eval_params_from_pb(convert.eval_params_to_pb(stored)) == EvalParams()

    def test_evaluator_needs_a_kind(self):
        with pytest.raises(ApiError) as error:
            convert.eval_params_from_pb(evaluation_pb2.EvalParams(evaluators=[evaluation_pb2.Evaluator()]))
        assert error.value.code == grpc.StatusCode.INVALID_ARGUMENT


def _spec(target: TraceTarget) -> RunSpec:
    return RunSpec(
        target=target,
        eval_set={"eval_set_id": "golden", "eval_cases": [{"eval_id": "c1", "conversation": []}]},
        eval_config=EvalParams(max_concurrent_evals=2).model_dump(by_alias=False),
        sinks=[{"kind": "stdout"}, {"kind": "file", "path": "/var/out.ndjson"}],
        context={"headers": {"X-Tenant": "team-a"}},
        credential_refs={"judge": {"kind": "kubernetes", "name": "judge-key", "key": "api_key"}},
    )


class TestRunSpec:
    @pytest.mark.parametrize(
        "target",
        [
            TraceTarget(kind="inline", inline={"resourceSpans": []}, trace_format="otlp-json"),
            TraceTarget(
                kind="http",
                base_url="https://traces.example.com/api",
                trace_id="1dbd779ff94bf24c7b84678a5dc0346c",
                trace_format="jaeger-json",
            ),
        ],
    )
    def test_round_trip(self, target):
        spec = _spec(target)
        assert convert.run_spec_from_pb(convert.run_spec_to_pb(spec)) == spec

    def test_uploaded_target_is_output_only(self):
        spec = convert.run_spec_to_pb(_spec(TraceTarget(kind="uploaded", trace_count=1, trace_files=["a.json"])))
        assert spec.target.uploaded.trace_files == ["a.json"]
        with pytest.raises(ApiError, match="output only"):
            convert.run_spec_from_pb(spec)

    @pytest.mark.parametrize(("content", "message"), [(b"{", "one JSON document"), (b"[]", "JSON object")])
    def test_inline_target_must_be_a_json_object(self, content, message):
        spec = runs_pb2.RunSpec(target=runs_pb2.TraceTarget(inline=common_pb2.TraceDocument(content=content)))
        with pytest.raises(ApiError, match=message):
            convert.run_spec_from_pb(spec)

    def test_target_is_required(self):
        with pytest.raises(ApiError, match="target needs one of"):
            convert.run_spec_from_pb(runs_pb2.RunSpec())

    def test_run_to_pb_redacts(self):
        spec = _spec(TraceTarget(kind="inline", inline={}))
        spec.context["headers"]["Authorization"] = "Bearer qxmark"
        run = Run(run_id=uuid4(), status=RunStatus.QUEUED, spec=spec)
        out = convert.run_to_pb(run)
        assert out.spec.context.headers["Authorization"] == "REDACTED"
        assert b"qxmark" not in out.SerializeToString()
        assert run.spec.context["headers"]["Authorization"] == "Bearer qxmark"

    def test_naive_times_are_utc(self):
        run = Run(run_id=uuid4(), status=RunStatus.QUEUED, spec=_spec(TraceTarget(kind="inline", inline={})))
        run.created_at = datetime(2026, 3, 1, 12, 30)
        assert convert.run_to_pb(run).created_at.ToDatetime(tzinfo=UTC) == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)


class TestResults:
    def test_unscored_invocations_keep_their_position(self):
        result = Result(
            result_id="r1",
            run_id=uuid4(),
            eval_set_item_id="i",
            eval_set_item_name="n",
            evaluator_name="m",
            evaluator_type="code",
            status=ResultStatus.ERRORED,
            score=math.nan,
            per_invocation_scores=[None, 0.5],
            latency_ms=0,
        )
        out = convert.result_to_pb(result)
        assert [i.HasField("score") for i in out.per_invocation] == [False, True]
        assert out.per_invocation[1].score == 0.5
        assert not out.HasField("score")
        assert out.HasField("latency_ms")
        assert (out.evaluator_type, out.status) == (runs_pb2.EVALUATOR_TYPE_CODE, runs_pb2.RESULT_STATUS_ERRORED)

    def test_metric_result_pairs_scores_with_statuses(self):
        metric = MetricResult(
            metric_name="m",
            score=0.0,
            eval_status="FAILED",
            per_invocation_scores=[1.0, None],
            per_invocation_statuses=["PASSED", "NOT_EVALUATED", "FAILED"],
            duration_ms=math.inf,
        )
        out = convert.metric_result_to_pb(metric)
        assert out.HasField("score") and out.score == 0.0
        assert [(i.HasField("score"), i.status) for i in out.per_invocation] == [
            (True, evaluation_pb2.EVAL_STATUS_PASSED),
            (False, evaluation_pb2.EVAL_STATUS_NOT_EVALUATED),
            (False, evaluation_pb2.EVAL_STATUS_FAILED),
        ]
        assert not out.HasField("duration_ms")
