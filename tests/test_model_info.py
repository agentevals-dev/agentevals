"""Regression tests for per-invocation model info extraction.

Covers https://github.com/agentevals-dev/agentevals/issues/204:
`_extract_model_info_from_trace` aggregated LLM spans across the whole trace
for every invocation, so each invocation reported the session-wide totals.
Now the converter records which LLM spans each invocation was built from and
ws_server aggregates only those spans.
"""

import asyncio
import json

from agentevals.converter import convert_trace
from agentevals.loader.base import Span, Trace
from agentevals.streaming.session import TraceSession
from agentevals.streaming.ws_server import StreamingTraceManager


def _adk_llm_span(
    span_id: str, model: str, input_tokens: int, output_tokens: int, start_time: int, parent: str = "invoke"
) -> Span:
    """Build an ADK call_llm span with distinct usage metadata and user text."""
    return Span(
        trace_id="t1",
        span_id=span_id,
        parent_span_id=parent,
        operation_name="call_llm",
        start_time=start_time,
        duration=1000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_request": json.dumps(
                {
                    "model": model,
                    "contents": [
                        {"role": "user", "parts": [{"text": f"hello from {span_id}"}]},
                    ],
                }
            ),
            "gcp.vertex.agent.llm_response": json.dumps(
                {
                    "content": {"parts": [{"text": f"answer from {span_id}"}], "role": "model"},
                    "usage_metadata": {
                        "prompt_token_count": input_tokens,
                        "candidates_token_count": output_tokens,
                    },
                }
            ),
            "gen_ai.provider.name": "vertex_ai",
            "gen_ai.response.finish_reasons": "stop",
        },
    )


def _two_invocation_adk_trace() -> Trace:
    """ADK trace with two invoke_agent spans, each owning its own LLM spans."""
    invoke1 = Span(
        trace_id="t1",
        span_id="invoke1",
        parent_span_id=None,
        operation_name="invoke_agent agent_a",
        start_time=1000,
        duration=20000,
        tags={"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
    )
    invoke2 = Span(
        trace_id="t1",
        span_id="invoke2",
        parent_span_id=None,
        operation_name="invoke_agent agent_b",
        start_time=30000,
        duration=20000,
        tags={"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
    )

    llm1 = _adk_llm_span("llm1", "model-a", 100, 20, 2000)
    llm2a = _adk_llm_span("llm2a", "model-b", 300, 50, 31000)
    llm2b = _adk_llm_span("llm2b", "model-b", 400, 60, 32000)

    llm1.parent_span_id = "invoke1"
    llm2a.parent_span_id = "invoke2"
    llm2b.parent_span_id = "invoke2"
    invoke1.children.append(llm1)
    invoke2.children.extend([llm2a, llm2b])

    return Trace(
        trace_id="t1",
        root_spans=[invoke1, invoke2],
        all_spans=[invoke1, llm1, invoke2, llm2a, llm2b],
    )


# ---------------------------------------------------------------------------
# OTLP span dict helpers (for driving a real TraceSession through
# StreamingTraceManager._extract_invocations, the wiring fixed in #204)
# ---------------------------------------------------------------------------


def _otlp_attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _otlp_span(
    span_id: str,
    name: str,
    start_ns: int,
    end_ns: int,
    attrs: dict,
    parent: str | None = None,
    trace_id: str = "t1",
) -> dict:
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "attributes": [_otlp_attr(k, str(v)) for k, v in attrs.items()],
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def _adk_llm_otlp_span(
    span_id: str, model: str, input_tokens: int, output_tokens: int, start_ns: int, parent: str
) -> dict:
    return _otlp_span(
        span_id,
        "call_llm",
        start_ns,
        start_ns + 1_000_000,
        {
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_request": json.dumps(
                {
                    "model": model,
                    "contents": [{"role": "user", "parts": [{"text": f"hello from {span_id}"}]}],
                }
            ),
            "gcp.vertex.agent.llm_response": json.dumps(
                {
                    "content": {"parts": [{"text": f"answer from {span_id}"}], "role": "model"},
                    "usage_metadata": {
                        "prompt_token_count": input_tokens,
                        "candidates_token_count": output_tokens,
                    },
                }
            ),
            "gen_ai.provider.name": "vertex_ai",
            "gen_ai.response.finish_reasons": "stop",
        },
        parent=parent,
    )


class TestPerInvocationSpans:
    def test_conversion_tracks_each_invocations_own_llm_spans(self):
        result = convert_trace(_two_invocation_adk_trace())
        assert len(result.invocations) == 2
        assert len(result.invocation_llm_spans) == 2
        assert [s.span_id for s in result.invocation_llm_spans[0]] == ["llm1"]
        assert [s.span_id for s in result.invocation_llm_spans[1]] == ["llm2a", "llm2b"]

    def test_model_info_is_per_invocation_not_session_wide(self):
        """Drive a real TraceSession through ``_extract_invocations``.

        This exercises the converter wiring that #204 actually broke, rather
        than calling the aggregator directly on hand-picked spans.
        """
        manager = StreamingTraceManager()

        invoke1 = _otlp_span(
            "invoke1",
            "invoke_agent agent_a",
            1_000_000_000,
            21_000_000_000,
            {"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
        )
        invoke2 = _otlp_span(
            "invoke2",
            "invoke_agent agent_b",
            30_000_000_000,
            50_000_000_000,
            {"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
        )
        llm1 = _adk_llm_otlp_span("llm1", "model-a", 100, 20, 2_000_000_000, parent="invoke1")
        llm2a = _adk_llm_otlp_span("llm2a", "model-b", 300, 50, 31_000_000_000, parent="invoke2")
        llm2b = _adk_llm_otlp_span("llm2b", "model-b", 400, 60, 32_000_000_000, parent="invoke2")

        session = TraceSession(
            session_id="s1",
            trace_id="t1",
            eval_set_id=None,
            spans=[invoke1, llm1, invoke2, llm2a, llm2b],
            logs=[],
        )

        data = asyncio.run(manager._extract_invocations(session))

        assert len(data) == 2
        info_a = data[0]["modelInfo"]
        info_b = data[1]["modelInfo"]
        assert info_a["inputTokens"] == 100
        assert info_a["outputTokens"] == 20
        assert info_b["inputTokens"] == 700  # 300 + 400, only invocation B's spans
        assert info_b["outputTokens"] == 110  # 50 + 60

        # The bug made every invocation report identical session-wide totals.
        assert info_a["inputTokens"] != info_b["inputTokens"]
        assert info_a["outputTokens"] != info_b["outputTokens"]

    def test_empty_spans_yield_empty_model_info(self):
        manager = StreamingTraceManager()
        assert manager._extract_model_info_from_llm_spans([]) == {}

    def test_nested_sub_agent_spans_not_double_counted(self):
        """Nested ``invoke_agent`` spans attribute LLM spans to one invocation.

        A coordinator ``invoke_agent`` span that nests a specialist
        ``invoke_agent`` span must not include the specialist's LLM spans:
        ``find_adk_llm_spans_in`` skips nested ``invoke_agent`` subtrees so
        each LLM span is attributed to exactly one invocation and token spend
        is not double counted.
        """
        coordinator = Span(
            trace_id="t1",
            span_id="coord",
            parent_span_id=None,
            operation_name="invoke_agent coordinator",
            start_time=1000,
            duration=30000,
            tags={"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
        )
        specialist = Span(
            trace_id="t1",
            span_id="spec",
            parent_span_id="coord",
            operation_name="invoke_agent specialist",
            start_time=2000,
            duration=20000,
            tags={"otel.scope.name": "gcp.vertex.agent", "gen_ai.operation.name": "invoke_agent"},
        )
        llm_root = _adk_llm_span("llm_root", "model-a", 600, 50, 3000, parent="coord")
        llm_sub = _adk_llm_span("llm_sub", "model-b", 500, 40, 4000, parent="spec")
        coordinator.children.extend([llm_root, specialist])
        specialist.children.append(llm_sub)

        trace = Trace(
            trace_id="t1",
            root_spans=[coordinator],
            all_spans=[coordinator, llm_root, specialist, llm_sub],
        )

        result = convert_trace(trace)

        # Both the coordinator and the nested specialist are treated as
        # invocations, and each LLM span belongs to exactly one of them.
        assert len(result.invocations) == 2
        assert [s.span_id for s in result.invocation_llm_spans[0]] == ["llm_root"]
        assert [s.span_id for s in result.invocation_llm_spans[1]] == ["llm_sub"]
