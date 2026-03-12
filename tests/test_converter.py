import json
import os

import pytest

from agentevals.loader.jaeger import JaegerJsonLoader
from agentevals.converter import (
    ConversionResult,
    convert_trace,
    convert_traces,
    _extract_user_content,
    _extract_final_response,
    _find_adk_spans,
)
from agentevals.loader.base import Span, Trace


SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def _make_adk_trace():
    """Build a minimal trace with ADK-style spans for testing."""
    invoke = Span(
        trace_id="t1",
        span_id="invoke1",
        parent_span_id=None,
        operation_name="invoke_agent test_agent",
        start_time=1000,
        duration=10000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "test_agent",
        },
    )

    call_llm_1 = Span(
        trace_id="t1",
        span_id="llm1",
        parent_span_id="invoke1",
        operation_name="call_llm",
        start_time=2000,
        duration=3000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_request": json.dumps(
                {
                    "model": "test-model",
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": "hello world"}],
                        }
                    ],
                }
            ),
            "gcp.vertex.agent.llm_response": json.dumps(
                {
                    "content": {
                        "parts": [
                            {
                                "function_call": {
                                    "name": "my_tool",
                                    "args": {"arg1": "value1"},
                                    "id": "call_123",
                                }
                            }
                        ],
                        "role": "model",
                    },
                }
            ),
        },
    )

    tool_span = Span(
        trace_id="t1",
        span_id="tool1",
        parent_span_id="llm1",
        operation_name="execute_tool my_tool",
        start_time=5000,
        duration=1000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "my_tool",
            "gen_ai.tool.call.id": "call_123",
            "gcp.vertex.agent.tool_call_args": json.dumps({"arg1": "value1"}),
            "gcp.vertex.agent.tool_response": json.dumps({"result": "tool output"}),
        },
    )

    call_llm_2 = Span(
        trace_id="t1",
        span_id="llm2",
        parent_span_id="invoke1",
        operation_name="call_llm",
        start_time=7000,
        duration=2000,
        tags={
            "otel.scope.name": "gcp.vertex.agent",
            "gcp.vertex.agent.llm_request": json.dumps(
                {
                    "model": "test-model",
                    "contents": [
                        {"role": "user", "parts": [{"text": "hello world"}]},
                        {
                            "role": "model",
                            "parts": [
                                {
                                    "function_call": {
                                        "name": "my_tool",
                                        "args": {"arg1": "value1"},
                                        "id": "call_123",
                                    }
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "function_response": {
                                        "name": "my_tool",
                                        "response": {"result": "tool output"},
                                    }
                                }
                            ],
                        },
                    ],
                }
            ),
            "gcp.vertex.agent.llm_response": json.dumps(
                {
                    "content": {
                        "parts": [{"text": "Here is the final answer."}],
                        "role": "model",
                    },
                }
            ),
        },
    )

    call_llm_1.children.append(tool_span)
    invoke.children.extend([call_llm_1, call_llm_2])

    trace = Trace(
        trace_id="t1",
        root_spans=[invoke],
        all_spans=[invoke, call_llm_1, tool_span, call_llm_2],
    )
    return trace


class TestConverter:
    def test_convert_synthetic_trace(self):
        trace = _make_adk_trace()
        result = convert_trace(trace)

        assert result.trace_id == "t1"
        assert len(result.invocations) == 1
        assert len(result.warnings) == 0

        inv = result.invocations[0]

        assert inv.user_content.role == "user"
        assert len(inv.user_content.parts) == 1
        assert inv.user_content.parts[0].text == "hello world"

        assert inv.final_response is not None
        assert inv.final_response.role == "model"
        assert inv.final_response.parts[0].text == "Here is the final answer."

        assert inv.intermediate_data is not None
        assert len(inv.intermediate_data.tool_uses) == 1
        assert inv.intermediate_data.tool_uses[0].name == "my_tool"
        assert inv.intermediate_data.tool_uses[0].args == {"arg1": "value1"}
        assert inv.intermediate_data.tool_uses[0].id == "call_123"

        assert len(inv.intermediate_data.tool_responses) == 1
        assert inv.intermediate_data.tool_responses[0].name == "my_tool"
        assert inv.intermediate_data.tool_responses[0].response == {
            "result": "tool output"
        }

    def test_convert_traces_multiple(self):
        trace = _make_adk_trace()
        results = convert_traces([trace, trace])
        assert len(results) == 2
        assert all(r.trace_id == "t1" for r in results)

    def test_no_invoke_agent_warns(self):
        trace = Trace(
            trace_id="empty",
            root_spans=[],
            all_spans=[
                Span(
                    trace_id="empty",
                    span_id="s1",
                    parent_span_id=None,
                    operation_name="something_else",
                    start_time=0,
                    duration=0,
                    tags={},
                )
            ],
        )
        result = convert_trace(trace)
        assert len(result.invocations) == 0
        assert len(result.warnings) == 1
        assert "no invoke_agent" in result.warnings[0]

    def test_no_tool_spans_fallback_to_llm_response(self):
        """When no execute_tool spans exist, function_calls should be
        extracted from call_llm responses instead."""
        invoke = Span(
            trace_id="t2",
            span_id="inv",
            parent_span_id=None,
            operation_name="invoke_agent agent",
            start_time=0,
            duration=10000,
            tags={
                "otel.scope.name": "gcp.vertex.agent",
                "gen_ai.agent.name": "agent",
            },
        )
        call_llm = Span(
            trace_id="t2",
            span_id="llm",
            parent_span_id="inv",
            operation_name="call_llm",
            start_time=1000,
            duration=5000,
            tags={
                "otel.scope.name": "gcp.vertex.agent",
                "gcp.vertex.agent.llm_request": json.dumps(
                    {
                        "contents": [
                            {"role": "user", "parts": [{"text": "do something"}]}
                        ]
                    }
                ),
                "gcp.vertex.agent.llm_response": json.dumps(
                    {
                        "content": {
                            "parts": [
                                {
                                    "function_call": {
                                        "name": "fn1",
                                        "args": {},
                                        "id": "c1",
                                    }
                                }
                            ],
                            "role": "model",
                        }
                    }
                ),
            },
        )
        invoke.children.append(call_llm)
        trace = Trace(
            trace_id="t2",
            root_spans=[invoke],
            all_spans=[invoke, call_llm],
        )
        result = convert_trace(trace)
        inv = result.invocations[0]
        assert len(inv.intermediate_data.tool_uses) == 1
        assert inv.intermediate_data.tool_uses[0].name == "fn1"
        assert len(inv.intermediate_data.tool_responses) == 0

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(SAMPLES_DIR, "helm.json")),
        reason="Sample file not available",
    )
    def test_convert_helm_sample(self):
        loader = JaegerJsonLoader()
        traces = loader.load(os.path.join(SAMPLES_DIR, "helm.json"))
        result = convert_trace(traces[0])

        assert len(result.invocations) == 1
        inv = result.invocations[0]

        assert "helm" in inv.user_content.parts[0].text.lower()
        assert len(inv.intermediate_data.tool_uses) == 1
        assert inv.intermediate_data.tool_uses[0].name == "helm_list_releases"
        assert "kagent" in inv.final_response.parts[0].text.lower()

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(SAMPLES_DIR, "k8s.json")),
        reason="Sample file not available",
    )
    def test_convert_k8s_sample(self):
        loader = JaegerJsonLoader()
        traces = loader.load(os.path.join(SAMPLES_DIR, "k8s.json"))
        result = convert_trace(traces[0])

        assert len(result.invocations) == 1
        inv = result.invocations[0]

        assert len(inv.intermediate_data.tool_uses) == 0
        assert len(inv.intermediate_data.tool_responses) == 0
        assert inv.final_response is not None
        assert inv.final_response.parts[0].text

    def test_explicit_format_parameter(self):
        trace = _make_adk_trace()
        result = convert_trace(trace, format="adk")

        assert len(result.invocations) == 1
        assert len(result.warnings) == 0

    def test_format_detection_with_genai_span_late_in_trace(self):
        non_llm_spans = []
        for i in range(15):
            non_llm_spans.append(Span(
                trace_id="test-trace",
                span_id=f"http-{i}",
                parent_span_id=None,
                operation_name="http.request",
                start_time=1000 + i * 100,
                duration=50,
                tags={},
                children=[],
            ))

        genai_span = Span(
            trace_id="test-trace",
            span_id="llm1",
            parent_span_id=None,
            operation_name="chat",
            start_time=3000,
            duration=1000,
            tags={
                "gen_ai.request.model": "gpt-3.5-turbo",
                "gen_ai.input.messages": json.dumps([{"role": "user", "content": "Hello"}]),
                "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "Hi"}]),
            },
            children=[],
        )

        all_spans = non_llm_spans + [genai_span]

        trace = Trace(
            trace_id="test-trace",
            root_spans=all_spans,
            all_spans=all_spans,
        )

        result = convert_trace(trace)

        assert len(result.invocations) == 1
        assert result.invocations[0].user_content.parts[0].text == "Hello"

    def test_format_detection_adk_with_mixed_genai_spans(self):
        from agentevals.converter import _detect_trace_format

        genai_span = Span(
            trace_id="mixed",
            span_id="openai1",
            parent_span_id=None,
            operation_name="openai.chat",
            start_time=500,
            duration=1000,
            tags={"gen_ai.request.model": "gpt-5-mini"},
            children=[],
        )
        adk_span = Span(
            trace_id="mixed",
            span_id="invoke1",
            parent_span_id=None,
            operation_name="invoke_agent test_agent",
            start_time=1000,
            duration=5000,
            tags={"otel.scope.name": "gcp.vertex.agent"},
            children=[],
        )
        trace = Trace(
            trace_id="mixed",
            root_spans=[genai_span, adk_span],
            all_spans=[genai_span, adk_span],
        )
        assert _detect_trace_format(trace) == "adk"

    def test_format_detection_defaults_to_adk_when_no_indicators(self):
        plain_span = Span(
            trace_id="test-trace",
            span_id="span1",
            parent_span_id=None,
            operation_name="generic_operation",
            start_time=1000,
            duration=1000,
            tags={},
            children=[],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[plain_span],
            all_spans=[plain_span],
        )

        result = convert_trace(trace)

        assert len(result.warnings) > 0
        assert "no invoke_agent spans found" in result.warnings[0]
