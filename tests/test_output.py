import json

from agentevals.output import _format_duration, format_results
from agentevals.runner import MetricResult, RunResult, TraceResult


class TestFormatDuration:
    def test_none(self):
        assert _format_duration(None) == ""

    def test_milliseconds(self):
        assert _format_duration(42.3) == "42ms"

    def test_zero(self):
        assert _format_duration(0.0) == "0ms"

    def test_seconds(self):
        assert _format_duration(1500.0) == "1.5s"

    def test_exact_one_second(self):
        assert _format_duration(1000.0) == "1.0s"

    def test_minutes(self):
        assert _format_duration(125000.0) == "2m 5s"

    def test_just_under_one_second(self):
        assert _format_duration(999.4) == "999ms"

    def test_rounding_boundary(self):
        assert _format_duration(999.5) == "1.0s"

    def test_minutes_no_60s(self):
        assert _format_duration(119500) == "2m 0s"


class TestTableFormatTiming:
    def _make_result(self, duration_ms: float | None = None) -> RunResult:
        mr = MetricResult(
            metric_name="test_metric",
            score=0.95,
            eval_status="PASSED",
            per_invocation_scores=[0.95],
            duration_ms=duration_ms,
        )
        tr = TraceResult(
            trace_id="abc123",
            num_invocations=1,
            metric_results=[mr],
        )
        return RunResult(trace_results=[tr])

    def test_time_column_in_table(self):
        output = format_results(self._make_result(duration_ms=1234.5), fmt="table")
        assert "Time" in output
        assert "1.2s" in output

    def test_time_column_milliseconds(self):
        output = format_results(self._make_result(duration_ms=42.0), fmt="table")
        assert "42ms" in output

    def test_time_column_none(self):
        output = format_results(self._make_result(duration_ms=None), fmt="table")
        assert "Time" in output

    def test_informational_status_has_its_own_icon(self):
        mr = MetricResult(metric_name="token_usage_v1", score=3906.0, eval_status="INFORMATIONAL")
        tr = TraceResult(trace_id="abc123", num_invocations=1, metric_results=[mr])
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "[INFO]  token_usage_v1" in output


class TestJsonFormatTiming:
    def test_duration_ms_in_json(self):
        mr = MetricResult(
            metric_name="test_metric",
            score=0.95,
            eval_status="PASSED",
            duration_ms=1234.5,
        )
        tr = TraceResult(
            trace_id="abc123",
            num_invocations=1,
            metric_results=[mr],
        )
        result = RunResult(trace_results=[tr])
        output = format_results(result, fmt="json")
        data = json.loads(output)
        assert data["traces"][0]["metrics"][0]["duration_ms"] == 1234.5

    def test_duration_ms_null_in_json(self):
        mr = MetricResult(metric_name="test_metric", score=0.5, eval_status="PASSED")
        tr = TraceResult(trace_id="abc123", num_invocations=1, metric_results=[mr])
        result = RunResult(trace_results=[tr])
        output = format_results(result, fmt="json")
        data = json.loads(output)
        assert data["traces"][0]["metrics"][0]["duration_ms"] is None


class TestSummaryFormatTiming:
    def test_duration_in_summary(self):
        mr = MetricResult(
            metric_name="test_metric",
            score=0.95,
            eval_status="PASSED",
            duration_ms=820.0,
        )
        tr = TraceResult(trace_id="abc123", num_invocations=1, metric_results=[mr])
        result = RunResult(trace_results=[tr])
        output = format_results(result, fmt="summary")
        assert "[820ms]" in output

    def test_no_duration_no_brackets(self):
        mr = MetricResult(metric_name="test_metric", score=0.5, eval_status="PASSED")
        tr = TraceResult(trace_id="abc123", num_invocations=1, metric_results=[mr])
        result = RunResult(trace_results=[tr])
        output = format_results(result, fmt="summary")
        metric_line = [line for line in output.splitlines() if "test_metric" in line][0]
        assert metric_line.rstrip().endswith("(PASSED)")
