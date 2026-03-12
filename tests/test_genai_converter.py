"""
Tests for GenAI trace converter.

Run with: pytest tests/test_genai_converter.py -v

Tests cover:
- JSON parsing and error handling
- User content extraction (string and list formats)
- Final response extraction (assistant/model/ai roles)
- Tool extraction from tool spans
- Single-turn LLM conversation conversion
- Multi-turn conversation conversion (multiple LLM root spans)
- Edge cases (missing data, malformed JSON)
"""

import json
import pytest

from agentevals.genai_converter import (
    ConversionResult,
    convert_genai_trace,
    _extract_user_text,
    _extract_assistant_text,
    _extract_tool_calls,
)
from agentevals.utils.genai_messages import parse_json_attr
from agentevals.loader.base import Span, Trace


def _make_genai_llm_span(
    span_id: str,
    input_messages: list[dict] | None = None,
    output_messages: list[dict] | None = None,
    model: str = "gpt-3.5-turbo",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> Span:
    """Create a GenAI LLM span with messages."""
    tags = {
        "gen_ai.request.model": model,
    }

    if input_messages is not None:
        tags["gen_ai.input.messages"] = json.dumps(input_messages)

    if output_messages is not None:
        tags["gen_ai.output.messages"] = json.dumps(output_messages)

    if input_tokens is not None:
        tags["gen_ai.usage.input_tokens"] = input_tokens

    if output_tokens is not None:
        tags["gen_ai.usage.output_tokens"] = output_tokens

    return Span(
        trace_id="test-trace",
        span_id=span_id,
        parent_span_id=None,
        operation_name="chat",
        start_time=1000,
        duration=5000,
        tags=tags,
    )


def _make_genai_tool_span(
    span_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    result: str | dict | None = None,
) -> Span:
    """Create a GenAI tool execution span."""
    tags = {
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.call.id": tool_call_id,
        "gen_ai.tool.call.arguments": json.dumps(arguments),
    }

    if result is not None:
        if isinstance(result, dict):
            tags["gen_ai.tool.call.result"] = json.dumps(result)
        else:
            tags["gen_ai.tool.call.result"] = result

    return Span(
        trace_id="test-trace",
        span_id=span_id,
        parent_span_id=None,
        operation_name=f"tool.{tool_name}",
        start_time=2000,
        duration=1000,
        tags=tags,
    )


class TestParseJsonAttr:
    def test_parse_json_string(self):
        result = parse_json_attr('{"key": "value"}', "test_tag")
        assert result == {"key": "value"}

    def test_parse_json_already_dict(self):
        result = parse_json_attr({"key": "value"}, "test_tag")
        assert result == {"key": "value"}

    def test_parse_json_list(self):
        result = parse_json_attr([1, 2, 3], "test_tag")
        assert result == [1, 2, 3]

    def test_parse_json_malformed(self):
        result = parse_json_attr("{invalid json}", "test_tag")
        assert result == {}

    def test_parse_json_none(self):
        result = parse_json_attr(None, "test_tag")
        assert result == {}


class TestExtractUserText:
    def test_extract_user_text_string_format(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Hello, world!"}
            ]
        )
        text = _extract_user_text(span)
        assert text == "Hello, world!"

    def test_extract_user_text_list_format(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": [{"text": "Multiple parts"}]}
            ]
        )
        text = _extract_user_text(span)
        assert text == "Multiple parts"

    def test_extract_user_text_human_role(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "human", "content": "Human role variant"}
            ]
        )
        text = _extract_user_text(span)
        assert text == "Human role variant"

    def test_extract_user_text_missing_raises(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "assistant", "content": "No user message"}
            ]
        )
        with pytest.raises(ValueError, match="no user message found"):
            _extract_user_text(span)

    def test_extract_user_text_empty_messages(self):
        span = _make_genai_llm_span("span1", input_messages=[])
        with pytest.raises(ValueError):
            _extract_user_text(span)


class TestExtractAssistantText:
    def test_extract_assistant_text_string_format(self):
        span = _make_genai_llm_span(
            "span1",
            output_messages=[
                {"role": "assistant", "content": "Response text"}
            ]
        )
        text = _extract_assistant_text(span)
        assert text == "Response text"

    def test_extract_assistant_text_list_format(self):
        span = _make_genai_llm_span(
            "span1",
            output_messages=[
                {"role": "assistant", "content": [{"text": "List response"}]}
            ]
        )
        text = _extract_assistant_text(span)
        assert text == "List response"

    def test_extract_assistant_text_model_role(self):
        span = _make_genai_llm_span(
            "span1",
            output_messages=[
                {"role": "model", "content": "Model role variant"}
            ]
        )
        text = _extract_assistant_text(span)
        assert text == "Model role variant"

    def test_extract_assistant_text_ai_role(self):
        span = _make_genai_llm_span(
            "span1",
            output_messages=[
                {"role": "ai", "content": "AI role variant"}
            ]
        )
        text = _extract_assistant_text(span)
        assert text == "AI role variant"

    def test_extract_assistant_text_missing_returns_empty(self):
        span = _make_genai_llm_span("span1", output_messages=[])
        text = _extract_assistant_text(span)
        assert text == ""

    def test_extract_assistant_text_takes_last_with_content(self):
        span = _make_genai_llm_span(
            "span1",
            output_messages=[
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": "Second message"},
            ]
        )
        text = _extract_assistant_text(span)
        assert text == "Second message"


class TestExtractToolCalls:
    def test_extract_tools_from_tool_spans(self):
        tool_span = _make_genai_tool_span(
            "tool1",
            tool_name="get_weather",
            tool_call_id="call_123",
            arguments={"location": "NYC"},
            result={"temperature": 72}
        )

        tool_calls, tool_responses = _extract_tool_calls([tool_span], [])

        assert len(tool_calls) == 1
        assert tool_calls[0].name == "get_weather"
        assert tool_calls[0].id == "call_123"
        assert tool_calls[0].args == {"location": "NYC"}

        assert len(tool_responses) == 1
        assert tool_responses[0].name == "get_weather"
        assert tool_responses[0].response == {"temperature": 72}

    def test_extract_tools_missing_name(self):
        span = Span(
            trace_id="test-trace",
            span_id="tool1",
            parent_span_id=None,
            operation_name="tool",
            start_time=1000,
            duration=1000,
            tags={"gen_ai.tool.call.id": "call_123"},
        )

        tool_calls, tool_responses = _extract_tool_calls([span], [])
        assert len(tool_calls) == 0
        assert len(tool_responses) == 0

    def test_extract_tools_invalid_arguments_json(self):
        tool_span = _make_genai_tool_span(
            "tool1",
            tool_name="test_tool",
            tool_call_id="call_123",
            arguments={},
        )
        tool_span.tags["gen_ai.tool.call.arguments"] = "{invalid json}"

        tool_calls, tool_responses = _extract_tool_calls([tool_span], [])

        assert len(tool_calls) == 1
        assert tool_calls[0].args == {}


class TestConvertGenaiTrace:
    def test_convert_single_turn_llm_only(self):
        llm_span = _make_genai_llm_span(
            "llm1",
            input_messages=[{"role": "user", "content": "Hello"}],
            output_messages=[{"role": "assistant", "content": "Hi there"}],
            input_tokens=10,
            output_tokens=5,
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span],
            all_spans=[llm_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        inv = result.invocations[0]
        assert inv.user_content.parts[0].text == "Hello"
        assert inv.final_response.parts[0].text == "Hi there"
        assert len(result.warnings) == 0

    def test_convert_multiturn_conversation(self):
        llm_span1 = _make_genai_llm_span(
            "llm1",
            input_messages=[
                {"role": "user", "content": "First question"},
                {"role": "user", "content": "Second question"},
            ],
            output_messages=[
                {"role": "assistant", "content": "First answer"},
                {"role": "assistant", "content": "Second answer"},
            ],
        )

        llm_span2 = _make_genai_llm_span(
            "llm2",
            input_messages=[
                {"role": "user", "content": "First question"},
                {"role": "user", "content": "Second question"},
            ],
            output_messages=[
                {"role": "assistant", "content": "First answer"},
                {"role": "assistant", "content": "Second answer"},
            ],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span1, llm_span2],
            all_spans=[llm_span1, llm_span2],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 2
        assert result.invocations[0].user_content.parts[0].text == "First question"
        assert result.invocations[0].final_response.parts[0].text == "First answer"
        assert result.invocations[1].user_content.parts[0].text == "Second question"
        assert result.invocations[1].final_response.parts[0].text == "Second answer"

    def test_convert_with_tool_spans(self):
        llm_span = _make_genai_llm_span(
            "llm1",
            input_messages=[
                {"role": "user", "content": "What's the weather?"}
            ],
            output_messages=[
                {"role": "assistant", "content": "It's 72 degrees"}
            ],
        )

        tool_span = _make_genai_tool_span(
            "tool1",
            tool_name="get_weather",
            tool_call_id="call_123",
            arguments={"location": "NYC"},
            result={"temperature": 72}
        )
        tool_span.parent_span_id = "llm1"

        llm_span.children = [tool_span]

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span],
            all_spans=[llm_span, tool_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        inv = result.invocations[0]
        assert inv.user_content.parts[0].text == "What's the weather?"
        assert inv.final_response.parts[0].text == "It's 72 degrees"
        assert len(inv.intermediate_data.tool_uses) == 1
        assert inv.intermediate_data.tool_uses[0].name == "get_weather"
        assert inv.intermediate_data.tool_uses[0].args == {"location": "NYC"}
        assert len(inv.intermediate_data.tool_responses) == 1

    def test_convert_no_llm_spans_returns_warning(self):
        span = Span(
            trace_id="test-trace",
            span_id="span1",
            parent_span_id=None,
            operation_name="http",
            start_time=1000,
            duration=1000,
            tags={},
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[span],
            all_spans=[span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 0
        assert len(result.warnings) > 0

    def test_convert_with_nested_llm_span(self):
        parent_span = Span(
            trace_id="test-trace",
            span_id="parent1",
            parent_span_id=None,
            operation_name="agent",
            start_time=1000,
            duration=10000,
            tags={},
        )

        llm_span = _make_genai_llm_span(
            "llm1",
            input_messages=[{"role": "user", "content": "Hello"}],
            output_messages=[{"role": "assistant", "content": "Hi"}],
        )
        llm_span.parent_span_id = "parent1"

        parent_span.children = [llm_span]

        trace = Trace(
            trace_id="test-trace",
            root_spans=[parent_span],
            all_spans=[parent_span, llm_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        assert result.invocations[0].user_content.parts[0].text == "Hello"

    def test_convert_malformed_messages_creates_warning(self):
        llm_span = _make_genai_llm_span(
            "llm1",
            input_messages=[{"role": "user", "content": "Hello"}],
            output_messages=[{"role": "assistant", "content": "Hi"}],
        )
        llm_span.tags["gen_ai.input.messages"] = "{malformed json}"

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span],
            all_spans=[llm_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.warnings) > 0

    def test_convert_missing_logs_creates_warning(self):
        llm_span = Span(
            trace_id="test-trace",
            span_id="llm1",
            parent_span_id=None,
            operation_name="chat",
            start_time=1000,
            duration=5000,
            tags={
                "gen_ai.request.model": "gpt-3.5-turbo",
            },
            children=[],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span],
            all_spans=[llm_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.warnings) > 0
        assert any("missing message content" in w.lower() for w in result.warnings)

    def test_convert_with_messages_no_warning(self):
        llm_span = _make_genai_llm_span(
            "llm1",
            input_messages=[{"role": "user", "content": "Hello"}],
            output_messages=[{"role": "assistant", "content": "Hi"}],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[llm_span],
            all_spans=[llm_span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        assert len(result.warnings) == 0
