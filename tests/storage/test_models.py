"""Storage model unit tests: pure functions, validation, MetricResult mapping."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from agentevals.runner import MetricResult
from agentevals.storage.models import (
    Result,
    ResultStatus,
    Run,
    RunSpec,
    RunStatus,
    TraceTarget,
    compute_result_id,
)


class TestComputeResultId:
    def test_deterministic(self):
        a = compute_result_id("00000000-0000-0000-0000-000000000001", "item-x", "metric-y")
        b = compute_result_id("00000000-0000-0000-0000-000000000001", "item-x", "metric-y")
        assert a == b

    def test_uuid_lowercased(self):
        upper = compute_result_id("00000000-0000-0000-0000-00000000ABCD", "item", "m")
        lower = compute_result_id("00000000-0000-0000-0000-00000000abcd", "item", "m")
        assert upper == lower

    def test_uuid_object_and_string_match(self):
        u = UUID("00000000-0000-0000-0000-000000000001")
        assert compute_result_id(u, "item", "m") == compute_result_id(str(u), "item", "m")

    def test_pipe_delimiter_byte_spec(self):
        """Locks the canonical formula so producer (Python) and any future
        consumer agree byte-for-byte. Any change here is a breaking change."""
        expected = hashlib.sha256(b"abc|item|m").hexdigest()
        assert compute_result_id("abc", "item", "m") == expected


class TestTraceTargetValidation:
    def test_inline(self):
        t = TraceTarget(kind="inline", inline={"data": []})
        assert t.kind == "inline"

    def test_http_with_base_url(self):
        t = TraceTarget(kind="http", base_url="https://example/", trace_id="abc")
        assert t.base_url == "https://example/"
        assert t.trace_id == "abc"

    def test_uploaded_with_audit_metadata(self):
        t = TraceTarget(kind="uploaded", trace_count=2, trace_files=["a.json", "b.json"])
        assert t.kind == "uploaded"
        assert t.trace_count == 2
        assert t.trace_files == ["a.json", "b.json"]

    def test_unknown_kind_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TraceTarget(kind="not-a-kind")


class TestRunSpec:
    def test_minimal_inline_spec(self):
        spec = RunSpec(approach="trace_replay", target=TraceTarget(kind="inline", inline={}))
        assert spec.approach == "trace_replay"
        assert spec.target.kind == "inline"
        assert spec.eval_set is None
        assert spec.eval_config == {}
        assert spec.sinks == []
        assert spec.context == {}

    def test_extra_fields_allowed_for_forward_compat(self):
        """RunSpec uses extra='allow' so a host can include forward-compatible
        metadata without breaking older agentevals replicas."""
        spec = RunSpec.model_validate(
            {
                "approach": "trace_replay",
                "target": {"kind": "inline", "inline": {}},
                "futureField": "unknown",
            }
        )
        assert spec.target.kind == "inline"


class TestResultFromMetricResult:
    """Locks the renaming + status-mapping behavior between the in-pipeline
    MetricResult shape and the persisted Result shape."""

    def _mr(self, **overrides):
        defaults = dict(
            metric_name="tool_trajectory_avg_score",
            score=0.8,
            eval_status="PASSED",
            per_invocation_scores=[1.0, 0.6],
            error=None,
            details={"foo": "bar"},
            duration_ms=42.5,
        )
        defaults.update(overrides)
        return MetricResult(**defaults)

    def _build(self, mr):
        return Result.from_metric_result(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            eval_set_item_id="item-1",
            eval_set_item_name="trace-abc",
            trace_id="trace-abc",
            evaluator_type="builtin",
            metric_result=mr,
        )

    def test_passed_maps_to_passed(self):
        r = self._build(self._mr(eval_status="PASSED"))
        assert r.status == ResultStatus.PASSED
        assert r.score == 0.8
        assert r.evaluator_name == "tool_trajectory_avg_score"
        assert r.evaluator_type == "builtin"
        assert r.eval_set_item_id == "item-1"
        assert r.trace_id == "trace-abc"

    def test_failed_maps_to_failed(self):
        r = self._build(self._mr(eval_status="FAILED"))
        assert r.status == ResultStatus.FAILED

    def test_not_evaluated_maps_to_skipped(self):
        r = self._build(self._mr(eval_status="NOT_EVALUATED", score=None, per_invocation_scores=[]))
        assert r.status == ResultStatus.SKIPPED

    def test_unknown_status_maps_to_skipped(self):
        """Defensive: ADK sometimes emits non-standard status strings;
        anything unknown should land as skipped, not crash."""
        r = self._build(self._mr(eval_status="MAYBE_PASSED"))
        assert r.status == ResultStatus.SKIPPED

    def test_error_dominates_status(self):
        """Even if eval_status says PASSED, a non-empty error means
        the row should land as 'errored' so downstream consumers can
        filter cleanly without special-casing the error field."""
        r = self._build(self._mr(eval_status="PASSED", error="boom"))
        assert r.status == ResultStatus.ERRORED
        assert r.error_text == "boom"

    def test_duration_ms_renamed_to_latency_ms(self):
        r = self._build(self._mr(duration_ms=42.7))
        assert r.latency_ms == 42  # int truncation matches the schema column type

    def test_latency_ms_none_when_duration_missing(self):
        r = self._build(self._mr(duration_ms=None))
        assert r.latency_ms is None

    def test_per_invocation_scores_preserved(self):
        r = self._build(self._mr(per_invocation_scores=[0.0, 0.5, 1.0]))
        assert r.per_invocation_scores == [0.0, 0.5, 1.0]

    def test_details_default_to_empty_dict(self):
        r = self._build(self._mr(details=None))
        assert r.details == {}

    def test_result_id_matches_canonical_formula(self):
        r = self._build(self._mr())
        expected = compute_result_id(
            UUID("00000000-0000-0000-0000-000000000001"),
            "item-1",
            "tool_trajectory_avg_score",
        )
        assert r.result_id == expected


class TestRun:
    def test_default_status_and_attempt(self):
        run = Run(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            status=RunStatus.QUEUED,
            spec=RunSpec(approach="trace_replay", target=TraceTarget(kind="inline", inline={})),
        )
        assert run.attempt == 0
        assert run.cancel_requested is False
        assert run.error is None
