import json

from agentevals.loader.base import Span, Trace
from agentevals.trace_metrics import (
    _calc_percentiles,
    _calc_summary_stats,
    extract_performance_metrics,
)


class TestCalcSummaryStats:
    def test_empty_list(self):
        result = _calc_summary_stats([])
        assert result["min"] == 0.0
        assert result["median"] == 0.0
        assert result["max"] == 0.0
        assert result["count"] == 0
        assert result["p50"] == 0.0
        assert result["p95"] == 0.0
        assert result["p99"] == 0.0

    def test_single_value(self):
        result = _calc_summary_stats([42.0])
        assert result["min"] == 42.0
        assert result["median"] == 42.0
        assert result["max"] == 42.0
        assert result["count"] == 1
        assert result["p50"] == 42.0

    def test_multiple_values(self):
        result = _calc_summary_stats([10.0, 20.0, 30.0, 40.0, 50.0])
        assert result["min"] == 10.0
        assert result["median"] == 30.0
        assert result["max"] == 50.0
        assert result["count"] == 5
        assert result["p50"] == 30.0

    def test_unsorted_input(self):
        result = _calc_summary_stats([50.0, 10.0, 30.0])
        assert result["min"] == 10.0
        assert result["median"] == 30.0
        assert result["max"] == 50.0

    def test_backwards_compat_keys_present(self):
        result = _calc_summary_stats([100.0, 200.0, 300.0])
        assert "p50" in result
        assert "p95" in result
        assert "p99" in result
        assert "min" in result
        assert "median" in result
        assert "max" in result
        assert "count" in result


class TestCalcPercentiles:
    def test_empty_list(self):
        result = _calc_percentiles([])
        assert result == {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_single_value(self):
        result = _calc_percentiles([100.0])
        assert result["p50"] == 100.0
        assert result["p95"] == 100.0
        assert result["p99"] == 100.0


def _make_genai_trace(
    num_llm_calls: int = 2,
    num_tool_calls: int = 1,
    model: str = "claude-sonnet-4-20250514",
    tool_names: list[str] | None = None,
    prompt_tokens: int = 1000,
    output_tokens: int = 100,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> Trace:
    """Build a GenAI semconv trace with configurable LLM and tool spans."""
    if tool_names is None:
        tool_names = [f"tool_{i}" for i in range(num_tool_calls)]

    root = Span(
        trace_id="t1",
        span_id="root",
        parent_span_id=None,
        operation_name="agent_run",
        start_time=0,
        duration=5_000_000,
        tags={},
    )
    spans = [root]

    for i in range(num_llm_calls):
        tags = {
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": prompt_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
        }
        if cache_creation and i == 0:
            tags["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
        if cache_read and i == 0:
            tags["gen_ai.usage.cache_read.input_tokens"] = cache_read
        span = Span(
            trace_id="t1",
            span_id=f"llm_{i}",
            parent_span_id="root",
            operation_name="chat",
            start_time=1_000_000 + i * 500_000,
            duration=400_000 + i * 100_000,
            tags=tags,
        )
        spans.append(span)

    for i, name in enumerate(tool_names):
        span = Span(
            trace_id="t1",
            span_id=f"tool_{i}",
            parent_span_id="root",
            operation_name=f"execute_tool {name}",
            start_time=2_000_000 + i * 100_000,
            duration=50_000 + i * 10_000,
            tags={"gen_ai.tool.name": name},
        )
        spans.append(span)

    return Trace(trace_id="t1", root_spans=[root], all_spans=spans)


class TestExtractPerformanceMetrics:
    def test_counts(self):
        trace = _make_genai_trace(num_llm_calls=3, num_tool_calls=2)
        result = extract_performance_metrics(trace)
        assert result["counts"]["llm_calls"] == 3
        assert result["counts"]["tool_calls"] == 2

    def test_models(self):
        trace = _make_genai_trace(model="gpt-4o")
        result = extract_performance_metrics(trace)
        assert result["models"] == ["gpt-4o"]

    def test_tool_names(self):
        trace = _make_genai_trace(tool_names=["search", "read_file"])
        result = extract_performance_metrics(trace)
        assert result["tool_names"] == ["read_file", "search"]

    def test_tool_names_deduped(self):
        trace = _make_genai_trace(tool_names=["search", "search", "read"])
        result = extract_performance_metrics(trace)
        assert result["tool_names"] == ["read", "search"]

    def test_cache_tokens(self):
        trace = _make_genai_trace(cache_creation=500, cache_read=1200)
        result = extract_performance_metrics(trace)
        assert result["tokens"]["cache_creation_tokens"] == 500
        assert result["tokens"]["cache_read_tokens"] == 1200

    def test_cache_tokens_zero_when_absent(self):
        trace = _make_genai_trace()
        result = extract_performance_metrics(trace)
        assert result["tokens"]["cache_creation_tokens"] == 0
        assert result["tokens"]["cache_read_tokens"] == 0

    def test_latency_has_summary_stats(self):
        trace = _make_genai_trace(num_llm_calls=3)
        result = extract_performance_metrics(trace)
        llm_lat = result["latency"]["llm_calls"]
        assert llm_lat["count"] == 3
        assert llm_lat["min"] > 0
        assert llm_lat["min"] <= llm_lat["median"] <= llm_lat["max"]

    def test_latency_backwards_compat(self):
        trace = _make_genai_trace()
        result = extract_performance_metrics(trace)
        for key in ("overall", "llm_calls", "tool_executions"):
            lat = result["latency"][key]
            assert "p50" in lat
            assert "p95" in lat
            assert "p99" in lat

    def test_tokens_total(self):
        trace = _make_genai_trace(num_llm_calls=2, prompt_tokens=500, output_tokens=50)
        result = extract_performance_metrics(trace)
        assert result["tokens"]["total_prompt"] == 1000
        assert result["tokens"]["total_output"] == 100
        assert result["tokens"]["total"] == 1100

    def test_per_llm_call_has_summary_stats(self):
        trace = _make_genai_trace(num_llm_calls=3)
        result = extract_performance_metrics(trace)
        per_call = result["tokens"]["per_llm_call"]
        assert "min" in per_call
        assert "median" in per_call
        assert "max" in per_call
        assert "count" in per_call
        assert per_call["count"] == 3

    def test_empty_trace(self):
        trace = Trace(trace_id="t1", root_spans=[], all_spans=[])
        result = extract_performance_metrics(trace)
        assert result["counts"]["llm_calls"] == 0
        assert result["counts"]["tool_calls"] == 0
        assert result["models"] == []
        assert result["tool_names"] == []

    def test_tool_name_from_operation_name_fallback(self):
        root = Span(
            trace_id="t1",
            span_id="root",
            parent_span_id=None,
            operation_name="agent",
            start_time=0,
            duration=1_000_000,
            tags={},
        )
        tool_span = Span(
            trace_id="t1",
            span_id="tool1",
            parent_span_id="root",
            operation_name="execute_tool my_tool",
            start_time=100_000,
            duration=50_000,
            tags={"otel.scope.name": "gcp.vertex.agent"},
        )
        trace = Trace(trace_id="t1", root_spans=[root], all_spans=[root, tool_span])
        result = extract_performance_metrics(trace)
        assert "my_tool" in result["tool_names"]


class TestOutputFormatPerformanceMetrics:
    def _make_perf_metrics(self, **overrides):
        base = {
            "latency": {
                "overall": {
                    "min": 3800.0,
                    "median": 4164.0,
                    "max": 4164.0,
                    "count": 1,
                    "p50": 4164.0,
                    "p95": 4164.0,
                    "p99": 4164.0,
                },
                "llm_calls": {
                    "min": 1900.0,
                    "median": 2072.0,
                    "max": 2344.0,
                    "count": 2,
                    "p50": 2072.0,
                    "p95": 2344.0,
                    "p99": 2344.0,
                },
                "tool_executions": {
                    "min": 50.0,
                    "median": 57.0,
                    "max": 57.0,
                    "count": 2,
                    "p50": 57.0,
                    "p95": 57.0,
                    "p99": 57.0,
                },
            },
            "tokens": {
                "total_prompt": 3776,
                "total_output": 130,
                "total": 3906,
                "per_llm_call": {
                    "min": 1800.0,
                    "median": 1953.0,
                    "max": 2073.0,
                    "count": 2,
                    "p50": 1953.0,
                    "p95": 2073.0,
                    "p99": 2073.0,
                },
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
            },
            "counts": {"llm_calls": 2, "tool_calls": 2, "invocations": 1},
            "models": ["claude-sonnet-4-20250514"],
            "tool_names": ["search"],
        }
        base.update(overrides)
        return base

    def test_table_shows_model(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "claude-sonnet-4-20250514" in output

    def test_table_shows_counts(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "2 LLM calls" in output
        assert "2 tool calls" in output
        assert "1 invocations" in output

    def test_table_shows_min_median_max(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "min=" in output
        assert "median=" in output
        assert "max=" in output
        assert "p50=" not in output or "Latency per Trace" in output

    def test_table_shows_cache_tokens_when_present(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        perf = self._make_perf_metrics()
        perf["tokens"]["cache_read_tokens"] = 1200
        perf["tokens"]["cache_creation_tokens"] = 500
        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=perf,
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "1200 cache read" in output
        assert "500 cache write" in output

    def test_table_hides_cache_tokens_when_zero(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "cache read" not in output
        assert "cache write" not in output

    def test_no_per_llm_call_line_in_table(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="table")
        assert "Per LLM Call" not in output

    def test_json_includes_new_fields(self):
        from agentevals.output import format_results
        from agentevals.runner import MetricResult, RunResult, TraceResult

        tr = TraceResult(
            trace_id="abc",
            num_invocations=1,
            metric_results=[MetricResult(metric_name="test", score=1.0, eval_status="PASSED")],
            performance_metrics=self._make_perf_metrics(),
        )
        output = format_results(RunResult(trace_results=[tr]), fmt="json")
        data = json.loads(output)
        perf = data["traces"][0]["performance_metrics"]
        assert "counts" in perf
        assert "models" in perf
        assert "tool_names" in perf
        assert perf["latency"]["overall"]["count"] == 1
        assert perf["latency"]["overall"]["min"] == 3800.0


class TestOverallPerformanceOutput:
    def test_overall_shows_counts(self):
        from agentevals.output import format_results
        from agentevals.runner import RunResult

        result = RunResult(
            trace_results=[],
            performance_metrics={
                "tokens": {
                    "total": 1000,
                    "total_prompt": 800,
                    "total_output": 200,
                    "avg_per_trace": {"prompt": 400, "output": 100},
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                },
                "counts": {
                    "traces": 2,
                    "total_llm_calls": 6,
                    "total_tool_calls": 4,
                    "avg_llm_calls_per_trace": 3,
                    "avg_tool_calls_per_trace": 2,
                },
                "latency": {
                    "overall_per_trace": {"p50": 4000.0, "p95": 5000.0, "p99": 5000.0},
                },
                "models": ["gpt-4o"],
                "trace_count": 2,
            },
        )
        output = format_results(result, fmt="table")
        assert "Traces: 2" in output
        assert "LLM Calls: 6" in output
        assert "Tool Calls: 4" in output

    def test_overall_shows_models(self):
        from agentevals.output import format_results
        from agentevals.runner import RunResult

        result = RunResult(
            trace_results=[],
            performance_metrics={
                "tokens": {
                    "total": 1000,
                    "total_prompt": 800,
                    "total_output": 200,
                    "avg_per_trace": {"prompt": 400, "output": 100},
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                },
                "counts": {
                    "traces": 2,
                    "total_llm_calls": 6,
                    "total_tool_calls": 4,
                    "avg_llm_calls_per_trace": 3,
                    "avg_tool_calls_per_trace": 2,
                },
                "latency": {},
                "models": ["claude-sonnet-4-20250514", "gpt-4o"],
                "trace_count": 2,
            },
        )
        output = format_results(result, fmt="table")
        assert "Models: claude-sonnet-4-20250514, gpt-4o" in output

    def test_overall_shows_latency_per_trace(self):
        from agentevals.output import format_results
        from agentevals.runner import RunResult

        result = RunResult(
            trace_results=[],
            performance_metrics={
                "tokens": {
                    "total": 1000,
                    "total_prompt": 800,
                    "total_output": 200,
                    "avg_per_trace": {"prompt": 400, "output": 100},
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                },
                "counts": {
                    "traces": 2,
                    "total_llm_calls": 6,
                    "total_tool_calls": 4,
                    "avg_llm_calls_per_trace": 3,
                    "avg_tool_calls_per_trace": 2,
                },
                "latency": {
                    "overall_per_trace": {"p50": 4000.0, "p95": 5000.0, "p99": 5000.0},
                },
                "models": [],
                "trace_count": 2,
            },
        )
        output = format_results(result, fmt="table")
        assert "Latency per Trace:" in output
        assert "p50=" in output
        assert "p95=" in output

    def test_overall_shows_cache_tokens(self):
        from agentevals.output import format_results
        from agentevals.runner import RunResult

        result = RunResult(
            trace_results=[],
            performance_metrics={
                "tokens": {
                    "total": 1000,
                    "total_prompt": 800,
                    "total_output": 200,
                    "avg_per_trace": {"prompt": 400, "output": 100},
                    "cache_creation_tokens": 300,
                    "cache_read_tokens": 1500,
                },
                "counts": {
                    "traces": 2,
                    "total_llm_calls": 6,
                    "total_tool_calls": 4,
                    "avg_llm_calls_per_trace": 3,
                    "avg_tool_calls_per_trace": 2,
                },
                "latency": {},
                "models": [],
                "trace_count": 2,
            },
        )
        output = format_results(result, fmt="table")
        assert "1500 cache read" in output
        assert "300 cache write" in output
