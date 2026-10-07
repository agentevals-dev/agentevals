"""Evaluation results as gen_ai.evaluation.result log events."""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from agentevals.config import BuiltinMetricDef, CodeEvaluatorDef
from agentevals.genai.extract import extract_conversation
from agentevals.otel import emit
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from agentevals.runner import MetricResult

from .builders import agent, assistant, chat, request, sid, span, tid, user

ALLOWED = {
    "gen_ai.evaluation.name",
    "gen_ai.evaluation.score.value",
    "gen_ai.evaluation.score.label",
    "gen_ai.evaluation.explanation",
    "error.type",
    "gen_ai.agent.name",
    "gen_ai.conversation.id",
    "gen_ai.response.id",
    "agentevals.evaluator.type",
    "agentevals.eval.run.id",
    "agentevals.eval.case.id",
    "agentevals.eval_set.id",
}


def _setup(*spans: dict):
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    conversation = extract_conversation(traces, "s")
    index = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
    return conversation, index


def _two_turns():
    turns = []
    for n in (1, 2):
        turns.append(
            span(
                trace=f"t{n}",
                span_id=f"a{n}",
                name="invoke_agent helper",
                attrs={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "helper",
                    "gen_ai.conversation.id": "conv-1",
                },
                start=n * 10_000,
                end=n * 10_000 + 5_000,
            )
        )
        turns.append(
            chat(
                trace=f"t{n}",
                span_id=f"c{n}",
                parent=f"a{n}",
                inputs=[user(f"secret question {n}")],
                outputs=[assistant(f"secret answer {n}")],
                start=n * 10_000 + 1,
                end=n * 10_000 + 4_000,
                extra={"gen_ai.response.id": f"resp-{n}"},
            )
        )
    return _setup(*turns)


def _emit(conversation, index, metrics, evaluators, explanation=False, **ids):
    exporter = InMemoryLogRecordExporter()
    emitter = emit.EvaluationEmitter(exporter, explanation=explanation)
    count = emitter.emit(conversation=conversation, metrics=metrics, evaluators=evaluators, spans=index, **ids)
    emitter.flush()
    records = [r.log_record for r in exporter.get_finished_logs()]
    assert len(records) == count
    return records


def _ref(record) -> tuple[str, str]:
    return format(record.trace_id, "032x"), format(record.span_id, "016x")


TRAJECTORY = BuiltinMetricDef(name="tool_trajectory_avg_score")


class TestRecords:
    def test_per_turn_scores_give_one_record_per_turn_on_its_anchor(self):
        conversation, index = _two_turns()
        metric = MetricResult(
            metric_name=TRAJECTORY.name,
            score=0.5,
            eval_status="FAILED",
            per_invocation_scores=[1.0, 0.0],
            per_invocation_statuses=["PASSED", "FAILED"],
        )
        records = _emit(
            conversation, index, [metric], [TRAJECTORY], run_id="run-1", eval_case_id="case", eval_set_id="set"
        )
        assert [_ref(r) for r in records] == [(tid("t1"), sid("a1")), (tid("t2"), sid("a2"))]
        attrs = [dict(r.attributes) for r in records]
        assert [(a["gen_ai.evaluation.score.value"], a["gen_ai.evaluation.score.label"]) for a in attrs] == [
            (1.0, "pass"),
            (0.0, "fail"),
        ]
        assert attrs[0]["agentevals.evaluator.type"] == "deterministic"
        assert (
            attrs[0]["agentevals.eval.run.id"],
            attrs[0]["agentevals.eval.case.id"],
            attrs[0]["agentevals.eval_set.id"],
        ) == ("run-1", "case", "set")
        assert (attrs[0]["gen_ai.agent.name"], attrs[0]["gen_ai.conversation.id"], attrs[0]["gen_ai.response.id"]) == (
            "helper",
            "conv-1",
            "resp-1",
        )
        assert all(
            r.event_name == "gen_ai.evaluation.result" and r.severity_number.value == 9 and r.body is None
            for r in records
        )

    def test_conversation_level_score_gives_one_record_on_the_last_turn(self):
        conversation, index = _two_turns()
        metric = MetricResult(metric_name="judge", score=0.8, eval_status="PASSED")
        records = _emit(conversation, index, [metric], [CodeEvaluatorDef(name="judge", path="judge.py")])
        assert [_ref(r) for r in records] == [(tid("t2"), sid("a2"))]
        assert "agentevals.evaluator.type" not in records[0].attributes

    def test_evaluator_error_has_error_type_and_no_score(self):
        conversation, index = _two_turns()
        metric = MetricResult(metric_name="judge", error="boom", error_type="TimeoutError")
        attrs = dict(
            _emit(conversation, index, [metric], [CodeEvaluatorDef(name="judge", path="judge.py")])[0].attributes
        )
        assert attrs["error.type"] == "TimeoutError"
        assert "gen_ai.evaluation.score.value" not in attrs and "gen_ai.evaluation.score.label" not in attrs

    def test_not_evaluated_is_labelled_and_explained_only_with_the_flag(self):
        conversation, index = _two_turns()
        metric = MetricResult(metric_name=TRAJECTORY.name, details={"reason": "no eval set provided"})
        quiet = dict(_emit(conversation, index, [metric], [TRAJECTORY])[0].attributes)
        loud = dict(_emit(conversation, index, [metric], [TRAJECTORY], explanation=True)[0].attributes)
        assert quiet["gen_ai.evaluation.score.label"] == "not_evaluated"
        assert "gen_ai.evaluation.explanation" not in quiet
        assert loud["gen_ai.evaluation.explanation"] == "no eval set provided"

    def test_records_never_carry_content(self):
        conversation, index = _two_turns()
        metric = MetricResult(
            metric_name=TRAJECTORY.name, score=1.0, eval_status="PASSED", per_invocation_scores=[1.0, 1.0]
        )
        for record in _emit(conversation, index, [metric], [TRAJECTORY]):
            assert set(record.attributes) <= ALLOWED
            assert "secret" not in repr(dict(record.attributes))

    def test_a_non_genai_root_defers_to_the_final_model_call(self):
        conversation, index = _setup(
            span(span_id="http", name="POST /chat", start=1, end=9_000),
            chat(span_id="c1", parent="http", start=100, end=200, outputs=[assistant("first")]),
            chat(span_id="c2", parent="http", start=300, end=400, outputs=[assistant("second")]),
        )
        records = _emit(
            conversation,
            index,
            [MetricResult(metric_name="judge", score=1.0, eval_status="PASSED")],
            [CodeEvaluatorDef(name="judge", path="judge.py")],
        )
        assert _ref(records[0]) == (tid("t1"), sid("c2"))

    def test_records_are_marked_sampled(self):
        conversation, index = _two_turns()
        record = _emit(
            conversation,
            index,
            [MetricResult(metric_name="judge", score=1.0)],
            [CodeEvaluatorDef(name="judge", path="j.py")],
        )[0]
        assert int(record.trace_flags) & 1 == 1

    def test_metrics_without_an_evaluator_are_skipped(self):
        conversation, index = _two_turns()
        metric = MetricResult(metric_name="(all)", error="Turn extraction failed")
        assert _emit(conversation, index, [metric], [TRAJECTORY]) == []

    def test_no_turns_no_records(self):
        conversation, index = _setup(span(name="GET /"))
        assert (
            _emit(
                conversation,
                index,
                [MetricResult(metric_name="judge", score=1.0)],
                [CodeEvaluatorDef(name="judge", path="j.py")],
            )
            == []
        )

    def test_judge_metrics_are_typed_llm_judge(self):
        conversation, index = _two_turns()
        judge = BuiltinMetricDef(name="final_response_match_v2")
        attrs = dict(
            _emit(conversation, index, [MetricResult(metric_name=judge.name, score=1.0)], [judge])[0].attributes
        )
        assert attrs["agentevals.evaluator.type"] == "llm_judge"


class TestFailures:
    def test_an_export_failure_never_raises_and_logs_rarely(self, caplog, monkeypatch):
        monkeypatch.setattr(emit.time, "monotonic", lambda: 1.0)
        conversation, index = _two_turns()
        emitter = emit.EvaluationEmitter(InMemoryLogRecordExporter())

        def broken(*args, **kwargs):
            raise RuntimeError("exporter down")

        emitter._record = broken
        metric = MetricResult(metric_name="judge", score=1.0)
        with caplog.at_level(logging.WARNING, logger="agentevals.otel.emit"):
            for _ in range(3):
                assert (
                    emitter.emit(
                        conversation=conversation,
                        metrics=[metric],
                        evaluators=[CodeEvaluatorDef(name="judge", path="j.py")],
                        spans=index,
                    )
                    == 0
                )
        assert len([r for r in caplog.records if "Could not emit" in r.message]) == 1


@pytest.fixture
def clean_switches(monkeypatch):
    for name in (
        "AGENTEVALS_EVALUATION_EVENTS",
        "AGENTEVALS_EVALUATION_EVENTS_EXPLANATION",
        "OTEL_SDK_DISABLED",
        "OTEL_LOGS_EXPORTER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(emit, "_requested", False)
    return monkeypatch


class TestSwitches:
    def test_off_by_default(self, clean_switches):
        assert emit.enabled() is False

    def test_env_turns_it_on(self, clean_switches):
        clean_switches.setenv("AGENTEVALS_EVALUATION_EVENTS", "true")
        assert emit.enabled() is True

    def test_flag_turns_it_on(self, clean_switches):
        emit.enable()
        assert emit.enabled() is True

    @pytest.mark.parametrize("name,value", [("OTEL_SDK_DISABLED", "true"), ("OTEL_LOGS_EXPORTER", "none")])
    def test_standard_switches_win(self, clean_switches, name, value):
        clean_switches.setenv("AGENTEVALS_EVALUATION_EVENTS", "true")
        clean_switches.setenv(name, value)
        emit.enable()
        assert emit.enabled() is False
        assert emit.get_emitter() is None

    @pytest.mark.parametrize("name", ["AGENTEVALS_EVALUATION_EVENTS", "AGENTEVALS_EVALUATION_EVENTS_EXPLANATION"])
    def test_unrecognized_values_are_errors(self, clean_switches, name):
        clean_switches.setenv(name, "maybe")
        with pytest.raises(ValueError, match=name):
            emit.validate_env()

    def test_explanation_flag_is_read_from_env(self, clean_switches):
        clean_switches.setenv("AGENTEVALS_EVALUATION_EVENTS_EXPLANATION", "yes")
        assert emit.EvaluationEmitter(InMemoryLogRecordExporter()).explanation is True


class TestExporterAndResource:
    @pytest.mark.parametrize(
        "env,module",
        [
            ({}, "opentelemetry.exporter.otlp.proto.http._log_exporter"),
            ({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "opentelemetry.exporter.otlp.proto.grpc._log_exporter"),
            ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}, "opentelemetry.exporter.otlp.proto.http._log_exporter"),
            (
                {"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/protobuf"},
                "opentelemetry.exporter.otlp.proto.http._log_exporter",
            ),
        ],
    )
    def test_protocol_selection(self, monkeypatch, env, module):
        for name in ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert type(emit._exporter_from_env()).__module__ == module

    def test_resource_identity_and_overrides(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "evals-prod")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=replica-3,deployment.environment.name=prod")
        attrs = emit.build_resource().attributes
        assert (attrs["service.name"], attrs["service.instance.id"], attrs["deployment.environment.name"]) == (
            "evals-prod",
            "replica-3",
            "prod",
        )
        assert emit.instance_id() == "replica-3"

    def test_default_service_name(self, monkeypatch):
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        attrs = emit.build_resource().attributes
        assert attrs["service.name"] == "agentevals"
        assert attrs["service.instance.id"] == emit.instance_id()
