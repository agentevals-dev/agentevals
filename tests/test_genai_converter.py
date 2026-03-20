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
    _deduplicate_invocations,
    _extract_assistant_text,
    _extract_tool_calls,
    _extract_user_text,
    _trim_cumulative_output,
    convert_genai_trace,
)
from agentevals.loader.base import Span, Trace
from agentevals.utils.genai_messages import parse_json_attr


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
        span = _make_genai_llm_span("span1", input_messages=[{"role": "user", "content": "Hello, world!"}])
        text = _extract_user_text(span)
        assert text == "Hello, world!"

    def test_extract_user_text_list_format(self):
        span = _make_genai_llm_span("span1", input_messages=[{"role": "user", "content": [{"text": "Multiple parts"}]}])
        text = _extract_user_text(span)
        assert text == "Multiple parts"

    def test_extract_user_text_human_role(self):
        span = _make_genai_llm_span("span1", input_messages=[{"role": "human", "content": "Human role variant"}])
        text = _extract_user_text(span)
        assert text == "Human role variant"

    def test_extract_user_text_missing_raises(self):
        span = _make_genai_llm_span("span1", input_messages=[{"role": "assistant", "content": "No user message"}])
        with pytest.raises(ValueError, match="no user message found"):
            _extract_user_text(span)

    def test_extract_user_text_empty_messages(self):
        span = _make_genai_llm_span("span1", input_messages=[])
        with pytest.raises(ValueError):
            _extract_user_text(span)


class TestExtractAssistantText:
    def test_extract_assistant_text_string_format(self):
        span = _make_genai_llm_span("span1", output_messages=[{"role": "assistant", "content": "Response text"}])
        text = _extract_assistant_text(span)
        assert text == "Response text"

    def test_extract_assistant_text_list_format(self):
        span = _make_genai_llm_span(
            "span1", output_messages=[{"role": "assistant", "content": [{"text": "List response"}]}]
        )
        text = _extract_assistant_text(span)
        assert text == "List response"

    def test_extract_assistant_text_model_role(self):
        span = _make_genai_llm_span("span1", output_messages=[{"role": "model", "content": "Model role variant"}])
        text = _extract_assistant_text(span)
        assert text == "Model role variant"

    def test_extract_assistant_text_ai_role(self):
        span = _make_genai_llm_span("span1", output_messages=[{"role": "ai", "content": "AI role variant"}])
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
            ],
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
            result={"temperature": 72},
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
            input_messages=[{"role": "user", "content": "What's the weather?"}],
            output_messages=[{"role": "assistant", "content": "It's 72 degrees"}],
        )

        tool_span = _make_genai_tool_span(
            "tool1",
            tool_name="get_weather",
            tool_call_id="call_123",
            arguments={"location": "NYC"},
            result={"temperature": 72},
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

    def test_convert_per_span_enriched_multiple_llm_spans(self):
        """Per-span enrichment (OTLP path): each span has only its own messages.

        When each LLM root span has a single user message, each should become
        a separate invocation (not collapsed into one multi-turn conversation).
        """
        span1 = _make_genai_llm_span(
            "llm1",
            input_messages=[{"role": "user", "content": "Question 1"}],
            output_messages=[{"role": "assistant", "content": "Answer 1"}],
        )
        span2 = _make_genai_llm_span(
            "llm2",
            input_messages=[{"role": "user", "content": "Question 2"}],
            output_messages=[{"role": "assistant", "content": "Answer 2"}],
        )
        span3 = _make_genai_llm_span(
            "llm3",
            input_messages=[{"role": "user", "content": "Question 3"}],
            output_messages=[{"role": "assistant", "content": "Answer 3"}],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[span1, span2, span3],
            all_spans=[span1, span2, span3],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 3
        assert result.invocations[0].user_content.parts[0].text == "Question 1"
        assert result.invocations[0].final_response.parts[0].text == "Answer 1"
        assert result.invocations[1].user_content.parts[0].text == "Question 2"
        assert result.invocations[1].final_response.parts[0].text == "Answer 2"
        assert result.invocations[2].user_content.parts[0].text == "Question 3"
        assert result.invocations[2].final_response.parts[0].text == "Answer 3"

    def test_broadcast_enriched_still_produces_multiturn(self):
        """Broadcast enrichment (WebSocket path): first span has all messages.

        When the first span has accumulated message history (multiple user
        messages), multi-turn extraction should still work correctly.
        """
        all_input = [
            {"role": "user", "content": "Q1"},
            {"role": "user", "content": "Q2"},
        ]
        all_output = [
            {"role": "assistant", "content": "A1"},
            {"role": "assistant", "content": "A2"},
        ]

        span1 = _make_genai_llm_span("llm1", input_messages=all_input, output_messages=all_output)
        span2 = _make_genai_llm_span("llm2", input_messages=all_input, output_messages=all_output)

        trace = Trace(
            trace_id="test-trace",
            root_spans=[span1, span2],
            all_spans=[span1, span2],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 2
        assert result.invocations[0].user_content.parts[0].text == "Q1"
        assert result.invocations[0].final_response.parts[0].text == "A1"
        assert result.invocations[1].user_content.parts[0].text == "Q2"
        assert result.invocations[1].final_response.parts[0].text == "A2"

    def test_cumulative_history_deduplication(self):
        """OpenAI instrumentor logs full history per LLM call.

        A tool-use loop produces multiple spans with the same user text:
        - Span 1: user asks "Roll a die" → assistant responds with tool_call
        - Span 2: user still "Roll a die" → assistant responds with final text
        Both have the same latest user message, so they should deduplicate.
        """
        span1 = _make_genai_llm_span(
            "llm1",
            input_messages=[
                {"role": "user", "content": "Roll a die"},
            ],
            output_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "function": {"name": "roll_die", "arguments": "{}"}}],
                },
            ],
        )
        span2 = _make_genai_llm_span(
            "llm2",
            input_messages=[
                {"role": "user", "content": "Roll a die"},
            ],
            output_messages=[
                {"role": "assistant", "content": "I rolled a 3!"},
            ],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[span1, span2],
            all_spans=[span1, span2],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        assert result.invocations[0].user_content.parts[0].text == "Roll a die"
        assert result.invocations[0].final_response.parts[0].text == "I rolled a 3!"


class TestExtractUserTextReversed:
    """The _extract_user_text function should return the LAST user message."""

    def test_returns_last_user_message(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "First question"},
                {"role": "user", "content": "Second question"},
                {"role": "user", "content": "Third question"},
            ],
        )
        text = _extract_user_text(span)
        assert text == "Third question"

    def test_returns_last_user_message_with_interleaved_roles(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi!"},
                {"role": "user", "content": "How are you?"},
            ],
        )
        text = _extract_user_text(span)
        assert text == "How are you?"


class TestDeduplicateInvocations:
    """Tests for _deduplicate_invocations."""

    def _make_invocation(self, user_text: str, response_text: str):
        from google.adk.evaluation.eval_case import Invocation
        from google.genai import types as genai_types

        return Invocation(
            invocation_id=f"inv-{user_text[:10]}",
            user_content=genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=user_text)],
            ),
            final_response=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=response_text)],
            ),
        )

    def test_no_dedup_when_all_unique(self):
        invocations = [
            self._make_invocation("Q1", "A1"),
            self._make_invocation("Q2", "A2"),
            self._make_invocation("Q3", "A3"),
        ]
        result = _deduplicate_invocations(invocations)
        assert len(result) == 3

    def test_dedup_keeps_last_duplicate(self):
        invocations = [
            self._make_invocation("Roll a die", "tool_call"),
            self._make_invocation("Roll a die", "I rolled a 3!"),
        ]
        result = _deduplicate_invocations(invocations)
        assert len(result) == 1
        assert result[0].final_response.parts[0].text == "I rolled a 3!"

    def test_dedup_preserves_order(self):
        invocations = [
            self._make_invocation("Q1", "A1-intermediate"),
            self._make_invocation("Q1", "A1-final"),
            self._make_invocation("Q2", "A2-intermediate"),
            self._make_invocation("Q2", "A2-final"),
        ]
        result = _deduplicate_invocations(invocations)
        assert len(result) == 2
        assert result[0].final_response.parts[0].text == "A1-final"
        assert result[1].final_response.parts[0].text == "A2-final"

    def test_single_invocation_no_change(self):
        invocations = [self._make_invocation("Q1", "A1")]
        result = _deduplicate_invocations(invocations)
        assert len(result) == 1

    def test_empty_list(self):
        result = _deduplicate_invocations([])
        assert result == []


class TestTrimCumulativeOutput:
    """Tests for _trim_cumulative_output — stripping historical tool calls."""

    def test_single_user_message_no_trimming(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[{"role": "user", "content": "Hello"}],
            output_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "greet", "arguments": "{}"}}],
                },
                {"role": "assistant", "content": "Hi!"},
            ],
        )
        output = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "greet", "arguments": "{}"}}],
            },
            {"role": "assistant", "content": "Hi!"},
        ]
        result = _trim_cumulative_output(span, output)
        assert result == output

    def test_two_turns_strips_first_turn(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": "{}"}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
            ],
        )
        output = [
            {"role": "assistant", "content": "Hello!"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": "{}"}}],
            },
            {"role": "assistant", "content": "I rolled a 3!"},
        ]
        result = _trim_cumulative_output(span, output)
        assert len(result) == 2
        assert result[0]["tool_calls"][0]["function"]["name"] == "roll_die"
        assert result[1]["content"] == "I rolled a 3!"

    def test_three_turns_strips_first_two(self):
        """Matches the LangChain bug scenario: 3 user messages, output has tool calls
        from all turns. Only the current turn's messages should remain."""
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
                {"role": "user", "content": "Is it prime?"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": '{"sides": 20}'}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "check_prime", "arguments": '{"nums": [3]}'},
                        }
                    ],
                },
                {"role": "assistant", "content": "Yes, 3 is prime!"},
            ],
        )
        output = [
            {"role": "assistant", "content": "Hello!"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": '{"sides": 20}'}}
                ],
            },
            {"role": "assistant", "content": "I rolled a 3!"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c2", "type": "function", "function": {"name": "check_prime", "arguments": '{"nums": [3]}'}}
                ],
            },
            {"role": "assistant", "content": "Yes, 3 is prime!"},
        ]
        result = _trim_cumulative_output(span, output)
        assert len(result) == 2
        assert result[0]["tool_calls"][0]["function"]["name"] == "check_prime"
        assert result[1]["content"] == "Yes, 3 is prime!"

    def test_no_input_messages_no_trimming(self):
        span = _make_genai_llm_span("span1", input_messages=None)
        output = [{"role": "assistant", "content": "Hello"}]
        result = _trim_cumulative_output(span, output)
        assert result == output

    def test_fewer_text_responses_than_expected_returns_full(self):
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Q1"},
                {"role": "user", "content": "Q2"},
                {"role": "user", "content": "Q3"},
            ],
            output_messages=[
                {"role": "assistant", "content": "A1"},
            ],
        )
        output = [{"role": "assistant", "content": "A1"}]
        result = _trim_cumulative_output(span, output)
        assert result == output

    def test_intermediate_span_with_stale_output(self):
        """Intermediate tool-call span where output hasn't caught up yet.

        Span 4 in the LangChain scenario: 3 user messages but output only has
        2 text responses (from previous turns). Trimming yields empty list.
        """
        span = _make_genai_llm_span(
            "span1",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
                {"role": "user", "content": "Is it prime?"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": "{}"}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
            ],
        )
        output = [
            {"role": "assistant", "content": "Hello!"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": "{}"}}],
            },
            {"role": "assistant", "content": "I rolled a 3!"},
        ]
        result = _trim_cumulative_output(span, output)
        assert len(result) == 0


class TestCumulativeHistoryToolCalls:
    """End-to-end tests for cumulative history tool call extraction.

    Simulates the LangChain multi-trace pattern: each LLM call gets its own
    trace with cumulative message history.
    """

    def test_langchain_three_turn_tool_calls(self):
        """3-turn conversation: greeting, roll_die, check_prime.

        Each span has cumulative output. After dedup, tool calls should only
        include the current turn's tools.
        """
        span1 = _make_genai_llm_span(
            "span1",
            input_messages=[{"role": "user", "content": "Hi"}],
            output_messages=[{"role": "assistant", "content": "Hello!"}],
        )

        span2_tc = _make_genai_llm_span(
            "span2",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
            ],
        )
        span2_tc.start_time = 2000

        span3_final = _make_genai_llm_span(
            "span3",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": '{"sides": 20}'}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
            ],
        )
        span3_final.start_time = 3000

        span4_tc = _make_genai_llm_span(
            "span4",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
                {"role": "user", "content": "Is it prime?"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": '{"sides": 20}'}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
            ],
        )
        span4_tc.start_time = 4000

        span5_final = _make_genai_llm_span(
            "span5",
            input_messages=[
                {"role": "user", "content": "Hi"},
                {"role": "user", "content": "Roll a die"},
                {"role": "user", "content": "Is it prime?"},
            ],
            output_messages=[
                {"role": "assistant", "content": "Hello!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "roll_die", "arguments": '{"sides": 20}'}}
                    ],
                },
                {"role": "assistant", "content": "I rolled a 3!"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "check_prime", "arguments": '{"nums": [3]}'},
                        }
                    ],
                },
                {"role": "assistant", "content": "Yes, 3 is prime!"},
            ],
        )
        span5_final.start_time = 5000

        all_spans = [span1, span2_tc, span3_final, span4_tc, span5_final]
        trace = Trace(
            trace_id="test-trace",
            root_spans=all_spans,
            all_spans=all_spans,
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 3

        inv1 = result.invocations[0]
        assert inv1.user_content.parts[0].text == "Hi"
        assert len(inv1.intermediate_data.tool_uses) == 0

        inv2 = result.invocations[1]
        assert inv2.user_content.parts[0].text == "Roll a die"
        tool_names_2 = [t.name for t in inv2.intermediate_data.tool_uses]
        assert tool_names_2 == ["roll_die"]

        inv3 = result.invocations[2]
        assert inv3.user_content.parts[0].text == "Is it prime?"
        tool_names_3 = [t.name for t in inv3.intermediate_data.tool_uses]
        assert tool_names_3 == ["check_prime"], f"Expected only check_prime for turn 3, got {tool_names_3}"

    def test_single_turn_with_tool_unaffected(self):
        """Single-turn tool use should not be affected by cumulative trimming."""
        span = _make_genai_llm_span(
            "span1",
            input_messages=[{"role": "user", "content": "What's the weather?"}],
            output_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
                        }
                    ],
                },
                {"role": "assistant", "content": "It's 72F in NYC"},
            ],
        )

        trace = Trace(
            trace_id="test-trace",
            root_spans=[span],
            all_spans=[span],
        )

        result = convert_genai_trace(trace)

        assert len(result.invocations) == 1
        tool_names = [t.name for t in result.invocations[0].intermediate_data.tool_uses]
        assert tool_names == ["get_weather"]
