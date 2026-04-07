"""Tests for MCP server response models and transformation logic."""

from __future__ import annotations

import pytest

from agentevals.mcp_server import (
    EvaluateSessionsResponse,
    EvaluateTracesResponse,
    InvocationSummaryResponse,
    MetricInfoResponse,
    MetricScoreResponse,
    SessionEvalResultResponse,
    SessionSummaryResponse,
    SummarizeSessionResponse,
    ToolCallResponse,
    TraceEvalResponse,
    summarize_run_result,
)
from agentevals.runner import MetricResult, RunResult, TraceResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metric_result(
    name: str = "tool_trajectory_avg_score",
    score: float | None = 0.85,
    status: str = "PASSED",
    error: str | None = None,
) -> MetricResult:
    return MetricResult(metric_name=name, score=score, eval_status=status, error=error)


def _make_trace_result(
    trace_id: str = "trace-1",
    num_invocations: int = 2,
    metric_results: list[MetricResult] | None = None,
    warnings: list[str] | None = None,
) -> TraceResult:
    return TraceResult(
        trace_id=trace_id,
        num_invocations=num_invocations,
        metric_results=[_make_metric_result()] if metric_results is None else metric_results,
        conversion_warnings=[] if warnings is None else warnings,
    )


def _make_run_result(
    trace_results: list[TraceResult] | None = None,
    errors: list[str] | None = None,
) -> RunResult:
    return RunResult(
        trace_results=[_make_trace_result()] if trace_results is None else trace_results,
        errors=[] if errors is None else errors,
    )


# ---------------------------------------------------------------------------
# summarize_run_result
# ---------------------------------------------------------------------------


class TestSummarizeRunResult:
    def test_single_passing_trace(self):
        result = summarize_run_result(_make_run_result())

        assert isinstance(result, EvaluateTracesResponse)
        assert result.passed is True
        assert len(result.traces) == 1
        assert result.traces[0].trace_id == "trace-1"
        assert result.traces[0].num_invocations == 2
        assert result.traces[0].metrics[0].metric == "tool_trajectory_avg_score"
        assert result.traces[0].metrics[0].score == 0.85
        assert result.traces[0].metrics[0].status == "PASSED"
        assert result.traces[0].metrics[0].error is None
        assert result.traces[0].warnings is None
        assert result.errors is None

    def test_failed_metric_sets_passed_false(self):
        run = _make_run_result(
            trace_results=[_make_trace_result(metric_results=[_make_metric_result(status="FAILED", score=0.3)])]
        )
        result = summarize_run_result(run)

        assert result.passed is False

    def test_mixed_pass_fail_across_traces(self):
        run = _make_run_result(
            trace_results=[
                _make_trace_result(
                    trace_id="t1",
                    metric_results=[_make_metric_result(status="PASSED")],
                ),
                _make_trace_result(
                    trace_id="t2",
                    metric_results=[_make_metric_result(status="FAILED", score=0.2)],
                ),
            ]
        )
        result = summarize_run_result(run)

        assert result.passed is False
        assert len(result.traces) == 2

    def test_not_evaluated_does_not_cause_failure(self):
        run = _make_run_result(
            trace_results=[_make_trace_result(metric_results=[_make_metric_result(status="NOT_EVALUATED", score=None)])]
        )
        result = summarize_run_result(run)

        assert result.passed is True

    def test_multiple_metrics_per_trace(self):
        run = _make_run_result(
            trace_results=[
                _make_trace_result(
                    metric_results=[
                        _make_metric_result(name="tool_trajectory_avg_score", score=0.9, status="PASSED"),
                        _make_metric_result(name="response_match_score", score=0.7, status="PASSED"),
                    ]
                )
            ]
        )
        result = summarize_run_result(run)

        assert result.passed is True
        assert len(result.traces[0].metrics) == 2
        assert result.traces[0].metrics[0].metric == "tool_trajectory_avg_score"
        assert result.traces[0].metrics[1].metric == "response_match_score"

    def test_conversion_warnings_included(self):
        run = _make_run_result(trace_results=[_make_trace_result(warnings=["Missing root span", "Unknown scope"])])
        result = summarize_run_result(run)

        assert result.traces[0].warnings == ["Missing root span", "Unknown scope"]

    def test_empty_warnings_becomes_none(self):
        run = _make_run_result(trace_results=[_make_trace_result(warnings=[])])
        result = summarize_run_result(run)

        assert result.traces[0].warnings is None

    def test_errors_included(self):
        run = _make_run_result(errors=["Failed to load trace file 'bad.json'"])
        result = summarize_run_result(run)

        assert result.errors == ["Failed to load trace file 'bad.json'"]

    def test_empty_errors_becomes_none(self):
        run = _make_run_result(errors=[])
        result = summarize_run_result(run)

        assert result.errors is None

    def test_metric_error_preserved(self):
        run = _make_run_result(
            trace_results=[
                _make_trace_result(
                    metric_results=[
                        _make_metric_result(
                            status="NOT_EVALUATED",
                            score=None,
                            error="Metric requires eval set",
                        )
                    ]
                )
            ]
        )
        result = summarize_run_result(run)

        assert result.traces[0].metrics[0].error == "Metric requires eval set"

    def test_empty_metric_error_becomes_none(self):
        run = _make_run_result(trace_results=[_make_trace_result(metric_results=[_make_metric_result(error="")])])
        result = summarize_run_result(run)

        assert result.traces[0].metrics[0].error is None

    def test_no_traces(self):
        run = _make_run_result(trace_results=[])
        result = summarize_run_result(run)

        assert result.passed is True
        assert result.traces == []

    def test_no_metrics_on_trace(self):
        run = _make_run_result(trace_results=[_make_trace_result(metric_results=[])])
        result = summarize_run_result(run)

        assert result.passed is True
        assert result.traces[0].metrics == []


# ---------------------------------------------------------------------------
# Response model serialization
# ---------------------------------------------------------------------------


class TestMetricInfoResponse:
    def test_from_api_shaped_data(self):
        api_data = {
            "name": "tool_trajectory_avg_score",
            "category": "trajectory",
            "requiresEvalSet": True,
            "requiresLLM": False,
            "requiresGCP": False,
            "requiresRubrics": False,
            "description": "Compares tool call sequences",
            "working": True,
        }
        model = MetricInfoResponse(
            name=api_data["name"],
            category=api_data["category"],
            requires_eval_set=api_data["requiresEvalSet"],
            requires_llm=api_data["requiresLLM"],
            requires_gcp=api_data["requiresGCP"],
            requires_rubrics=api_data["requiresRubrics"],
            description=api_data["description"],
            working=api_data["working"],
        )

        assert model.name == "tool_trajectory_avg_score"
        assert model.requires_eval_set is True
        assert model.requires_llm is False
        dumped = model.model_dump()
        assert "requires_eval_set" in dumped
        assert "requiresEvalSet" not in dumped


class TestSessionSummaryResponse:
    def test_from_api_shaped_data(self):
        api_data = {
            "sessionId": "sess-abc",
            "isComplete": True,
            "spanCount": 42,
            "startedAt": "2025-01-15T10:30:00Z",
        }
        model = SessionSummaryResponse(
            session_id=api_data["sessionId"],
            is_complete=api_data["isComplete"],
            span_count=api_data["spanCount"],
            started_at=api_data["startedAt"],
        )

        assert model.session_id == "sess-abc"
        assert model.is_complete is True
        assert model.span_count == 42
        dumped = model.model_dump()
        assert "session_id" in dumped
        assert "sessionId" not in dumped


class TestEvaluateSessionsResponse:
    def test_from_api_shaped_data(self):
        api_data = {
            "goldenSessionId": "golden-1",
            "evalSetId": "eval-golden-1",
            "results": [
                {
                    "sessionId": "sess-2",
                    "traceId": "t2",
                    "numInvocations": 3,
                    "metricResults": [
                        {"metricName": "tool_trajectory_avg_score", "score": 0.9, "evalStatus": "PASSED"}
                    ],
                    "error": None,
                },
                {
                    "sessionId": "sess-3",
                    "traceId": None,
                    "numInvocations": None,
                    "metricResults": None,
                    "error": "Session has no spans",
                },
            ],
        }
        model = EvaluateSessionsResponse(
            golden_session_id=api_data["goldenSessionId"],
            eval_set_id=api_data["evalSetId"],
            results=[
                SessionEvalResultResponse(
                    session_id=r["sessionId"],
                    trace_id=r.get("traceId"),
                    num_invocations=r.get("numInvocations"),
                    metric_results=r.get("metricResults"),
                    error=r.get("error"),
                )
                for r in api_data["results"]
            ],
        )

        assert model.golden_session_id == "golden-1"
        assert len(model.results) == 2
        assert model.results[0].session_id == "sess-2"
        assert model.results[0].metric_results[0]["score"] == 0.9
        assert model.results[1].error == "Session has no spans"
        assert model.results[1].metric_results is None


class TestSummarizeSessionResponse:
    def test_with_invocations(self):
        model = SummarizeSessionResponse(
            session_id="sess-1",
            num_spans=15,
            num_invocations=2,
            invocations=[
                InvocationSummaryResponse(
                    user="deploy nginx",
                    response="I'll deploy nginx using helm.",
                    tool_calls=[
                        ToolCallResponse(tool="helm_install", args={"chart": "nginx"}),
                        ToolCallResponse(tool="kubectl_get", args={"resource": "pods"}),
                    ],
                ),
                InvocationSummaryResponse(
                    user="check status",
                    response="All pods are running.",
                    tool_calls=[],
                ),
            ],
        )

        assert model.num_invocations == 2
        assert model.invocations[0].tool_calls[0].tool == "helm_install"
        assert model.invocations[0].tool_calls[0].args == {"chart": "nginx"}
        assert model.invocations[1].tool_calls == []

    def test_empty_session(self):
        model = SummarizeSessionResponse(
            session_id="sess-empty",
            num_spans=0,
            invocations=[],
        )

        assert model.num_invocations == 0
        assert model.invocations == []

    def test_tool_call_default_args(self):
        tc = ToolCallResponse(tool="my_tool")
        assert tc.args == {}
