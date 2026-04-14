"""Tests for the shared extraction module."""

from __future__ import annotations

import json

import pytest

from agentevals.extraction import (
    AdkExtractor,
    GenAIExtractor,
    extract_agent_response_from_attrs,
    extract_extended_model_info_from_attrs,
    extract_token_usage_from_attrs,
    extract_tool_call_from_attrs,
    extract_user_text_from_attrs,
    flatten_otlp_attributes,
    get_extractor,
    is_adk_scope,
    is_invocation_span,
    is_llm_span,
    is_tool_span,
)
from agentevals.loader.base import Span, Trace
from agentevals.trace_attrs import (
    ADK_LLM_REQUEST,
    ADK_LLM_RESPONSE,
    ADK_SCOPE_VALUE,
    ADK_TOOL_CALL_ARGS,
    OTEL_ERROR_TYPE,
    OTEL_GENAI_AGENT_NAME,
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OP,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_GENAI_PROVIDER_NAME,
    OTEL_GENAI_REQUEST_MAX_TOKENS,
    OTEL_GENAI_REQUEST_MODEL,
    OTEL_GENAI_REQUEST_TEMPERATURE,
    OTEL_GENAI_RESPONSE_FINISH_REASONS,
    OTEL_GENAI_RESPONSE_ID,
    OTEL_GENAI_RESPONSE_MODEL,
    OTEL_GENAI_SYSTEM,
    OTEL_GENAI_TOOL_CALL_ARGUMENTS,
    OTEL_GENAI_TOOL_CALL_ID,
    OTEL_GENAI_TOOL_DESCRIPTION,
    OTEL_GENAI_TOOL_NAME,
    OTEL_GENAI_TOOL_TYPE,
    OTEL_GENAI_USAGE_CACHE_CREATION_TOKENS,
    OTEL_GENAI_USAGE_CACHE_READ_TOKENS,
    OTEL_GENAI_USAGE_INPUT_TOKENS,
    OTEL_GENAI_USAGE_OUTPUT_TOKENS,
    OTEL_SCOPE,
)


def _span(op="test", tags=None, children=None, span_id="s1", start_time=0):
    return Span(
        trace_id="t1",
        span_id=span_id,
        parent_span_id=None,
        operation_name=op,
        start_time=start_time,
        duration=1000,
        tags=tags or {},
        children=children or [],
    )


def _trace(spans, root_spans=None):
    return Trace(
        trace_id="t1",
        root_spans=root_spans if root_spans is not None else spans,
        all_spans=spans,
    )


# ---------------------------------------------------------------------------
# extract_user_text_from_attrs
# ---------------------------------------------------------------------------


class TestExtractUserText:
    def test_adk_llm_request(self):
        attrs = {
            ADK_LLM_REQUEST: json.dumps(
                {
                    "contents": [
                        {"role": "user", "parts": [{"text": "Hello from ADK"}]},
                    ]
                }
            )
        }
        assert extract_user_text_from_attrs(attrs) == "Hello from ADK"

    def test_adk_llm_request_prefers_last_user(self):
        attrs = {
            ADK_LLM_REQUEST: json.dumps(
                {
                    "contents": [
                        {"role": "user", "parts": [{"text": "First"}]},
                        {"role": "model", "parts": [{"text": "Response"}]},
                        {"role": "user", "parts": [{"text": "Second"}]},
                    ]
                }
            )
        }
        assert extract_user_text_from_attrs(attrs) == "Second"

    def test_genai_content_based(self):
        attrs = {
            OTEL_GENAI_INPUT_MESSAGES: json.dumps(
                [
                    {"role": "user", "content": "Hello from GenAI"},
                ]
            )
        }
        assert extract_user_text_from_attrs(attrs) == "Hello from GenAI"

    def test_genai_parts_based(self):
        attrs = {
            OTEL_GENAI_INPUT_MESSAGES: json.dumps(
                [
                    {"role": "user", "parts": [{"type": "text", "content": "Parts hello"}]},
                ]
            )
        }
        assert extract_user_text_from_attrs(attrs) == "Parts hello"

    def test_adk_takes_priority_over_genai(self):
        attrs = {
            ADK_LLM_REQUEST: json.dumps({"contents": [{"role": "user", "parts": [{"text": "ADK wins"}]}]}),
            OTEL_GENAI_INPUT_MESSAGES: json.dumps(
                [
                    {"role": "user", "content": "GenAI loses"},
                ]
            ),
        }
        assert extract_user_text_from_attrs(attrs) == "ADK wins"

    def test_empty_attrs(self):
        assert extract_user_text_from_attrs({}) is None

    def test_no_user_role(self):
        attrs = {
            OTEL_GENAI_INPUT_MESSAGES: json.dumps(
                [
                    {"role": "system", "content": "You are helpful"},
                ]
            )
        }
        assert extract_user_text_from_attrs(attrs) is None

    def test_pre_deserialized_dict(self):
        attrs = {
            OTEL_GENAI_INPUT_MESSAGES: [
                {"role": "user", "content": "Already parsed"},
            ]
        }
        assert extract_user_text_from_attrs(attrs) == "Already parsed"


# ---------------------------------------------------------------------------
# extract_agent_response_from_attrs
# ---------------------------------------------------------------------------


class TestExtractAgentResponse:
    def test_adk_llm_response(self):
        attrs = {ADK_LLM_RESPONSE: json.dumps({"content": {"parts": [{"text": "ADK response"}]}})}
        assert extract_agent_response_from_attrs(attrs) == "ADK response"

    def test_genai_content_based(self):
        attrs = {
            OTEL_GENAI_OUTPUT_MESSAGES: json.dumps(
                [
                    {"role": "assistant", "content": "GenAI response"},
                ]
            )
        }
        assert extract_agent_response_from_attrs(attrs) == "GenAI response"

    def test_genai_model_role(self):
        attrs = {
            OTEL_GENAI_OUTPUT_MESSAGES: json.dumps(
                [
                    {"role": "model", "content": "Model response"},
                ]
            )
        }
        assert extract_agent_response_from_attrs(attrs) == "Model response"

    def test_adk_takes_priority(self):
        attrs = {
            ADK_LLM_RESPONSE: json.dumps({"content": {"parts": [{"text": "ADK wins"}]}}),
            OTEL_GENAI_OUTPUT_MESSAGES: json.dumps(
                [
                    {"role": "assistant", "content": "GenAI loses"},
                ]
            ),
        }
        assert extract_agent_response_from_attrs(attrs) == "ADK wins"

    def test_empty_attrs(self):
        assert extract_agent_response_from_attrs({}) is None

    def test_adk_no_text_parts(self):
        attrs = {ADK_LLM_RESPONSE: json.dumps({"content": {"parts": [{"function_call": {"name": "tool"}}]}})}
        assert extract_agent_response_from_attrs(attrs) is None

    def test_genai_prefers_last_assistant(self):
        attrs = {
            OTEL_GENAI_OUTPUT_MESSAGES: json.dumps(
                [
                    {"role": "assistant", "content": "First response"},
                    {"role": "assistant", "content": "Second response"},
                ]
            )
        }
        assert extract_agent_response_from_attrs(attrs) == "Second response"


# ---------------------------------------------------------------------------
# extract_token_usage_from_attrs
# ---------------------------------------------------------------------------


class TestExtractTokenUsage:
    def test_adk_usage_metadata(self):
        attrs = {
            ADK_LLM_RESPONSE: json.dumps(
                {
                    "usage_metadata": {
                        "prompt_token_count": 100,
                        "candidates_token_count": 50,
                    }
                }
            ),
            ADK_LLM_REQUEST: json.dumps({"model": "gemini-pro"}),
        }
        in_toks, out_toks, model = extract_token_usage_from_attrs(attrs)
        assert in_toks == 100
        assert out_toks == 50
        assert model == "gemini-pro"

    def test_genai_direct_attrs(self):
        attrs = {
            OTEL_GENAI_USAGE_INPUT_TOKENS: 200,
            OTEL_GENAI_USAGE_OUTPUT_TOKENS: 75,
            OTEL_GENAI_REQUEST_MODEL: "claude-3-opus",
        }
        in_toks, out_toks, model = extract_token_usage_from_attrs(attrs)
        assert in_toks == 200
        assert out_toks == 75
        assert model == "claude-3-opus"

    def test_adk_takes_priority(self):
        attrs = {
            ADK_LLM_RESPONSE: json.dumps(
                {
                    "usage_metadata": {
                        "prompt_token_count": 100,
                        "candidates_token_count": 50,
                    }
                }
            ),
            OTEL_GENAI_USAGE_INPUT_TOKENS: 999,
            OTEL_GENAI_USAGE_OUTPUT_TOKENS: 999,
        }
        in_toks, out_toks, _ = extract_token_usage_from_attrs(attrs)
        assert in_toks == 100
        assert out_toks == 50

    def test_empty_attrs(self):
        in_toks, out_toks, model = extract_token_usage_from_attrs({})
        assert in_toks == 0
        assert out_toks == 0
        assert model == "unknown"

    def test_zero_tokens(self):
        attrs = {
            OTEL_GENAI_USAGE_INPUT_TOKENS: 0,
            OTEL_GENAI_USAGE_OUTPUT_TOKENS: 0,
        }
        in_toks, out_toks, _ = extract_token_usage_from_attrs(attrs)
        assert in_toks == 0
        assert out_toks == 0


# ---------------------------------------------------------------------------
# extract_tool_call_from_attrs
# ---------------------------------------------------------------------------


class TestExtractToolCall:
    def test_genai_tool_attrs(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "search",
            OTEL_GENAI_TOOL_CALL_ID: "tc1",
            OTEL_GENAI_TOOL_CALL_ARGUMENTS: json.dumps({"query": "test"}),
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result == {"id": "tc1", "name": "search", "args": {"query": "test"}}

    def test_adk_tool_attrs(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "search",
            ADK_TOOL_CALL_ARGS: json.dumps({"query": "adk test"}),
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result["name"] == "search"
        assert result["args"] == {"query": "adk test"}

    def test_name_from_operation(self):
        attrs = {}
        result = extract_tool_call_from_attrs(attrs, operation_name="execute_tool my_tool")
        assert result is not None
        assert result["name"] == "my_tool"

    def test_no_name_returns_none(self):
        assert extract_tool_call_from_attrs({}) is None

    def test_span_id_fallback_when_no_tool_call_id(self):
        attrs = {OTEL_GENAI_TOOL_NAME: "search"}
        result = extract_tool_call_from_attrs(attrs, span_id="abc123")
        assert result["id"] == "abc123"

    def test_unknown_fallback_when_no_ids(self):
        attrs = {OTEL_GENAI_TOOL_NAME: "search"}
        result = extract_tool_call_from_attrs(attrs)
        assert result["id"] == "unknown"

    def test_tool_call_id_takes_priority_over_span_id(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "search",
            OTEL_GENAI_TOOL_CALL_ID: "tc1",
        }
        result = extract_tool_call_from_attrs(attrs, span_id="span-xyz")
        assert result["id"] == "tc1"

    def test_genai_args_take_priority_over_adk(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "tool",
            OTEL_GENAI_TOOL_CALL_ARGUMENTS: json.dumps({"genai": True}),
            ADK_TOOL_CALL_ARGS: json.dumps({"adk": True}),
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result["args"] == {"genai": True}


# ---------------------------------------------------------------------------
# flatten_otlp_attributes
# ---------------------------------------------------------------------------


class TestFlattenOtlpAttributes:
    def test_string_value(self):
        result = flatten_otlp_attributes(
            [
                {"key": "k1", "value": {"stringValue": "v1"}},
            ]
        )
        assert result == {"k1": "v1"}

    def test_int_value(self):
        result = flatten_otlp_attributes(
            [
                {"key": "k1", "value": {"intValue": "42"}},
            ]
        )
        assert result == {"k1": 42}

    def test_mixed_types(self):
        result = flatten_otlp_attributes(
            [
                {"key": "str", "value": {"stringValue": "hello"}},
                {"key": "num", "value": {"doubleValue": 3.14}},
                {"key": "flag", "value": {"boolValue": True}},
            ]
        )
        assert result == {"str": "hello", "num": 3.14, "flag": True}

    def test_empty(self):
        assert flatten_otlp_attributes([]) == {}


# ---------------------------------------------------------------------------
# Span classification helpers
# ---------------------------------------------------------------------------


class TestSpanClassifiers:
    def test_is_adk_scope(self):
        assert is_adk_scope(_span(tags={OTEL_SCOPE: ADK_SCOPE_VALUE}))
        assert not is_adk_scope(_span(tags={}))

    def test_is_llm_span_by_model(self):
        assert is_llm_span(_span(tags={OTEL_GENAI_REQUEST_MODEL: "gpt-4"}))

    def test_is_llm_span_by_input_messages_alone_is_not_enough(self):
        assert not is_llm_span(_span(tags={OTEL_GENAI_INPUT_MESSAGES: "[]"}))

    def test_is_llm_span_empty(self):
        assert not is_llm_span(_span())

    def test_is_tool_span(self):
        assert is_tool_span(_span(tags={OTEL_GENAI_TOOL_NAME: "search"}))
        assert not is_tool_span(_span())

    def test_is_invocation_span_by_operation_name(self):
        assert is_invocation_span(_span(tags={OTEL_GENAI_OP: "invoke_agent"}))

    def test_is_invocation_span_by_keyword(self):
        assert is_invocation_span(_span(op="my_agent_runner"))
        assert is_invocation_span(_span(op="LangChain chain"))
        assert is_invocation_span(_span(op="workflow_executor"))

    def test_is_invocation_span_false(self):
        assert not is_invocation_span(_span(op="chat gpt-4"))


# ---------------------------------------------------------------------------
# Extractor detection
# ---------------------------------------------------------------------------


class TestExtractorDetection:
    def test_adk_trace_detected(self):
        span = _span(tags={OTEL_SCOPE: ADK_SCOPE_VALUE})
        trace = _trace([span])
        ext = get_extractor(trace)
        assert isinstance(ext, AdkExtractor)
        assert ext.format_name() == "adk"

    def test_genai_trace_detected(self):
        span = _span(tags={OTEL_GENAI_REQUEST_MODEL: "claude-3"})
        trace = _trace([span])
        ext = get_extractor(trace)
        assert isinstance(ext, GenAIExtractor)
        assert ext.format_name() == "genai"

    def test_empty_trace_defaults_to_adk(self):
        span = _span()
        trace = _trace([span])
        ext = get_extractor(trace)
        assert isinstance(ext, AdkExtractor)

    def test_adk_takes_priority_over_genai(self):
        span = _span(
            tags={
                OTEL_SCOPE: ADK_SCOPE_VALUE,
                OTEL_GENAI_REQUEST_MODEL: "gemini-pro",
            }
        )
        trace = _trace([span])
        ext = get_extractor(trace)
        assert isinstance(ext, AdkExtractor)

    def test_adk_find_invocation_spans(self):
        inv_span = _span(
            op="invoke_agent my_agent",
            tags={OTEL_SCOPE: ADK_SCOPE_VALUE},
            span_id="inv1",
        )
        llm_span = _span(
            op="call_llm",
            tags={OTEL_SCOPE: ADK_SCOPE_VALUE},
            span_id="llm1",
        )
        trace = _trace([inv_span, llm_span])
        ext = AdkExtractor()
        invocations = ext.find_invocation_spans(trace)
        assert len(invocations) == 1
        assert invocations[0].span_id == "inv1"

    def test_genai_find_invocation_spans_by_keyword(self):
        agent_span = _span(op="my_agent", span_id="a1")
        trace = _trace([agent_span])
        ext = GenAIExtractor()
        invocations = ext.find_invocation_spans(trace)
        assert len(invocations) == 1
        assert invocations[0].span_id == "a1"

    def test_genai_find_invocation_spans_by_op_name(self):
        agent_span = _span(
            op="invoke_agent helper",
            tags={OTEL_GENAI_OP: "invoke_agent"},
            span_id="a1",
        )
        trace = _trace([agent_span])
        ext = GenAIExtractor()
        invocations = ext.find_invocation_spans(trace)
        assert len(invocations) == 1


class TestAdkExtractorSpanFinding:
    def test_find_llm_spans_in(self):
        child_llm = _span(op="call_llm gemini", span_id="llm1")
        child_tool = _span(op="execute_tool search", span_id="tool1")
        root = _span(op="invoke_agent a", children=[child_llm, child_tool])
        ext = AdkExtractor()
        assert [s.span_id for s in ext.find_llm_spans_in(root)] == ["llm1"]

    def test_find_tool_spans_in(self):
        child_llm = _span(op="call_llm gemini", span_id="llm1")
        child_tool = _span(op="execute_tool search", span_id="tool1")
        root = _span(op="invoke_agent a", children=[child_llm, child_tool])
        ext = AdkExtractor()
        assert [s.span_id for s in ext.find_tool_spans_in(root)] == ["tool1"]

    def test_classify_span(self):
        ext = AdkExtractor()
        assert ext.classify_span(_span(op="invoke_agent a", tags={OTEL_SCOPE: ADK_SCOPE_VALUE})) == "invocation"
        assert ext.classify_span(_span(op="call_llm", tags={OTEL_SCOPE: ADK_SCOPE_VALUE})) == "llm"
        assert ext.classify_span(_span(op="execute_tool x", tags={OTEL_SCOPE: ADK_SCOPE_VALUE})) == "tool"
        assert ext.classify_span(_span(op="random")) is None


class TestGenAIExtractorSpanFinding:
    def test_find_llm_spans_in(self):
        child = _span(
            op="chat gpt-4",
            tags={OTEL_GENAI_REQUEST_MODEL: "gpt-4"},
            span_id="llm1",
        )
        root = _span(op="agent_run", children=[child])
        ext = GenAIExtractor()
        assert [s.span_id for s in ext.find_llm_spans_in(root)] == ["llm1"]

    def test_find_tool_spans_in(self):
        child = _span(
            op="execute_tool search",
            tags={OTEL_GENAI_TOOL_NAME: "search"},
            span_id="tool1",
        )
        root = _span(op="agent_run", children=[child])
        ext = GenAIExtractor()
        assert [s.span_id for s in ext.find_tool_spans_in(root)] == ["tool1"]


# ---------------------------------------------------------------------------
# extract_extended_model_info_from_attrs
# ---------------------------------------------------------------------------


class TestExtractExtendedModelInfo:
    def test_provider_from_provider_name(self):
        attrs = {OTEL_GENAI_PROVIDER_NAME: "openai"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["provider"] == "openai"

    def test_provider_fallback_to_gen_ai_system(self):
        attrs = {OTEL_GENAI_SYSTEM: "anthropic"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["provider"] == "anthropic"

    def test_provider_name_takes_priority_over_system(self):
        attrs = {
            OTEL_GENAI_PROVIDER_NAME: "openai",
            OTEL_GENAI_SYSTEM: "old_value",
        }
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["provider"] == "openai"

    def test_provider_none_when_absent(self):
        result = extract_extended_model_info_from_attrs({})
        assert result["provider"] is None

    def test_response_model(self):
        attrs = {OTEL_GENAI_RESPONSE_MODEL: "gpt-4o-2024-08-06"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["response_model"] == "gpt-4o-2024-08-06"

    def test_request_model(self):
        attrs = {OTEL_GENAI_REQUEST_MODEL: "gpt-4o"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["request_model"] == "gpt-4o"

    def test_response_id(self):
        attrs = {OTEL_GENAI_RESPONSE_ID: "chatcmpl-abc123"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["response_id"] == "chatcmpl-abc123"

    def test_finish_reasons_from_list(self):
        attrs = {OTEL_GENAI_RESPONSE_FINISH_REASONS: ["stop", "tool_calls"]}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["finish_reasons"] == ["stop", "tool_calls"]

    def test_finish_reasons_from_json_string(self):
        attrs = {OTEL_GENAI_RESPONSE_FINISH_REASONS: '["stop"]'}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["finish_reasons"] == ["stop"]

    def test_finish_reasons_from_plain_string(self):
        attrs = {OTEL_GENAI_RESPONSE_FINISH_REASONS: "stop"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["finish_reasons"] == ["stop"]

    def test_finish_reasons_empty_when_absent(self):
        result = extract_extended_model_info_from_attrs({})
        assert result["finish_reasons"] == []

    def test_temperature_numeric(self):
        attrs = {OTEL_GENAI_REQUEST_TEMPERATURE: 0.7}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["temperature"] == 0.7

    def test_temperature_from_string(self):
        attrs = {OTEL_GENAI_REQUEST_TEMPERATURE: "0.9"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["temperature"] == 0.9

    def test_temperature_invalid_returns_none(self):
        attrs = {OTEL_GENAI_REQUEST_TEMPERATURE: "not_a_number"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["temperature"] is None

    def test_max_tokens_numeric(self):
        attrs = {OTEL_GENAI_REQUEST_MAX_TOKENS: 4096}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["max_tokens"] == 4096

    def test_max_tokens_from_string(self):
        attrs = {OTEL_GENAI_REQUEST_MAX_TOKENS: "2048"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["max_tokens"] == 2048

    def test_cache_creation_tokens(self):
        attrs = {OTEL_GENAI_USAGE_CACHE_CREATION_TOKENS: 1500}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["cache_creation_tokens"] == 1500

    def test_cache_read_tokens(self):
        attrs = {OTEL_GENAI_USAGE_CACHE_READ_TOKENS: 3000}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["cache_read_tokens"] == 3000

    def test_cache_tokens_default_to_zero(self):
        result = extract_extended_model_info_from_attrs({})
        assert result["cache_creation_tokens"] == 0
        assert result["cache_read_tokens"] == 0

    def test_cache_tokens_from_string(self):
        attrs = {
            OTEL_GENAI_USAGE_CACHE_CREATION_TOKENS: "500",
            OTEL_GENAI_USAGE_CACHE_READ_TOKENS: "1000",
        }
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["cache_creation_tokens"] == 500
        assert result["cache_read_tokens"] == 1000

    def test_error_type(self):
        attrs = {OTEL_ERROR_TYPE: "timeout"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["error_type"] == "timeout"

    def test_error_type_none_when_absent(self):
        result = extract_extended_model_info_from_attrs({})
        assert result["error_type"] is None

    def test_full_attribute_set(self):
        attrs = {
            OTEL_GENAI_PROVIDER_NAME: "anthropic",
            OTEL_GENAI_REQUEST_MODEL: "claude-sonnet-4-20250514",
            OTEL_GENAI_RESPONSE_MODEL: "claude-sonnet-4-20250514",
            OTEL_GENAI_RESPONSE_ID: "msg_abc",
            OTEL_GENAI_RESPONSE_FINISH_REASONS: ["end_turn"],
            OTEL_GENAI_REQUEST_TEMPERATURE: 1.0,
            OTEL_GENAI_REQUEST_MAX_TOKENS: 8192,
            OTEL_GENAI_USAGE_CACHE_CREATION_TOKENS: 2000,
            OTEL_GENAI_USAGE_CACHE_READ_TOKENS: 5000,
            OTEL_ERROR_TYPE: None,
        }
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["provider"] == "anthropic"
        assert result["request_model"] == "claude-sonnet-4-20250514"
        assert result["response_model"] == "claude-sonnet-4-20250514"
        assert result["response_id"] == "msg_abc"
        assert result["finish_reasons"] == ["end_turn"]
        assert result["temperature"] == 1.0
        assert result["max_tokens"] == 8192
        assert result["cache_creation_tokens"] == 2000
        assert result["cache_read_tokens"] == 5000
        assert result["error_type"] is None


# ---------------------------------------------------------------------------
# extract_tool_call_from_attrs — tool type and description
# ---------------------------------------------------------------------------


class TestExtractToolCallTypeAndDescription:
    def test_type_and_description_present(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "search",
            OTEL_GENAI_TOOL_CALL_ID: "tc1",
            OTEL_GENAI_TOOL_CALL_ARGUMENTS: json.dumps({"q": "test"}),
            OTEL_GENAI_TOOL_TYPE: "function",
            OTEL_GENAI_TOOL_DESCRIPTION: "Search the web",
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result["name"] == "search"
        assert result["type"] == "function"
        assert result["description"] == "Search the web"

    def test_type_without_description(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "retriever",
            OTEL_GENAI_TOOL_TYPE: "datastore",
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result["type"] == "datastore"
        assert "description" not in result

    def test_description_without_type(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "calculator",
            OTEL_GENAI_TOOL_DESCRIPTION: "Performs arithmetic",
        }
        result = extract_tool_call_from_attrs(attrs)
        assert result["description"] == "Performs arithmetic"
        assert "type" not in result

    def test_absent_type_and_description(self):
        attrs = {
            OTEL_GENAI_TOOL_NAME: "search",
            OTEL_GENAI_TOOL_CALL_ID: "tc1",
        }
        result = extract_tool_call_from_attrs(attrs)
        assert "type" not in result
        assert "description" not in result
