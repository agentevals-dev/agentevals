"""Tests for the shared extraction module."""

from __future__ import annotations

import json

import pytest

from agentevals import trace_attrs
from agentevals.extraction import (
    extract_agent_response_from_attrs,
    extract_extended_model_info_from_attrs,
    extract_token_usage_from_attrs,
    extract_tool_call_from_attrs,
    extract_user_text_from_attrs,
    flatten_otlp_attributes,
    resolve_attr,
    resolve_schema_version,
)
from agentevals.trace_attrs import (
    ADK_LLM_REQUEST,
    ADK_LLM_RESPONSE,
    ADK_SCOPE_VALUE,
    ADK_TOOL_CALL_ARGS,
    GENAI_ATTRIBUTE_ALIASES,
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
    OTEL_SCHEMA_URL,
    OTEL_SCOPE,
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

    def test_adk_llm_request_outer_contents_pascalcase(self):
        attrs = {
            ADK_LLM_REQUEST: json.dumps(
                {
                    "Contents": [
                        {"role": "user", "parts": [{"text": "Outer PascalCase only"}]},
                    ]
                }
            )
        }
        assert extract_user_text_from_attrs(attrs) == "Outer PascalCase only"

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

    def test_adk_llm_response_outer_content_pascalcase(self):
        attrs = {ADK_LLM_RESPONSE: json.dumps({"Content": {"parts": [{"text": "Outer Content only"}]}})}
        assert extract_agent_response_from_attrs(attrs) == "Outer Content only"

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

    def test_array_value(self):
        result = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.response.finish_reasons",
                    "value": {"arrayValue": {"values": [{"stringValue": "stop"}]}},
                },
            ]
        )
        assert result == {"gen_ai.response.finish_reasons": ["stop"]}

    def test_kvlist_value(self):
        result = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.tool.call.arguments",
                    "value": {
                        "kvlistValue": {
                            "values": [
                                {"key": "city", "value": {"stringValue": "Berlin"}},
                                {"key": "metric", "value": {"boolValue": False}},
                            ]
                        }
                    },
                },
            ]
        )
        assert result == {"gen_ai.tool.call.arguments": {"city": "Berlin", "metric": False}}

    def test_array_of_kvlist(self):
        """Messages arrive as an arrayValue of kvlistValue."""
        result = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.input.messages",
                    "value": {
                        "arrayValue": {
                            "values": [
                                {"kvlistValue": {"values": [{"key": "role", "value": {"stringValue": "user"}}]}},
                            ]
                        }
                    },
                },
            ]
        )
        assert result == {"gen_ai.input.messages": [{"role": "user"}]}

    def test_finish_reasons_survive_to_extracted_model_info(self):
        """The symptom #173 names: gen_ai.response.finish_reasons reaching the
        consumer as ["stop"] rather than a literal blob or nothing.

        Asserting through extract_extended_model_info_from_attrs rather than at
        the decoder keeps the whole path covered - decoding it correctly is not
        the same as it arriving correctly.
        """
        attrs = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.response.finish_reasons",
                    "value": {"arrayValue": {"values": [{"stringValue": "stop"}]}},
                },
                {"key": "gen_ai.response.model", "value": {"stringValue": "claude-opus-5"}},
            ]
        )
        info = extract_extended_model_info_from_attrs(attrs)
        assert info["finish_reasons"] == ["stop"]
        assert info["response_model"] == "claude-opus-5"

    def test_multiple_finish_reasons_survive(self):
        attrs = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.response.finish_reasons",
                    "value": {"arrayValue": {"values": [{"stringValue": "stop"}, {"stringValue": "length"}]}},
                }
            ]
        )
        assert extract_extended_model_info_from_attrs(attrs)["finish_reasons"] == [
            "stop",
            "length",
        ]

    def test_unlisted_key_drops_container_value(self):
        """Containers survive only for SPEC_CONTAINER_ATTRS. Everything else is
        dropped, which is what extraction did before the decoder was shared."""
        attrs = flatten_otlp_attributes(
            [
                {
                    "key": "gen_ai.response.model",
                    "value": {"arrayValue": {"values": [{"stringValue": "claude-opus-5"}]}},
                },
                {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-5"}},
            ]
        )
        assert "gen_ai.response.model" not in attrs
        info = extract_extended_model_info_from_attrs(attrs)
        assert info["response_model"] is None
        assert info["request_model"] == "claude-sonnet-5"

    def test_no_unlisted_key_can_yield_an_unhashable_value(self):
        """The property the allowlist exists for: nothing outside
        SPEC_CONTAINER_ATTRS can reach a consumer as a dict key or set member
        and raise TypeError. Covers every attribute constant we declare, so a
        new one cannot quietly reopen the hazard."""
        container = {"arrayValue": {"values": [{"stringValue": "x"}]}}
        for name in dir(trace_attrs):
            if not name.isupper():
                continue
            key = getattr(trace_attrs, name)
            if not isinstance(key, str):
                continue
            value = flatten_otlp_attributes([{"key": key, "value": container}]).get(key)
            if key in trace_attrs.SPEC_CONTAINER_ATTRS:
                assert value == ["x"], f"{key} should keep its container"
            else:
                assert value is None, f"{key} leaked a container"
                hash(value)

    def test_bytes_value(self):
        """MessageToDict base64-encodes bytes fields, so the decoder sees a str."""
        result = flatten_otlp_attributes([{"key": "payload", "value": {"bytesValue": "AP9oaQ=="}}])
        assert result == {"payload": "AP9oaQ=="}

    def test_empty(self):
        assert flatten_otlp_attributes([]) == {}

    def test_schema_url_flows_through(self):
        # otel.schema_url is a plain string attribute; flatten_otlp_attributes
        # has no allowlist, so it passes through like any other key.
        result = flatten_otlp_attributes(
            [
                {"key": OTEL_SCHEMA_URL, "value": {"stringValue": "https://opentelemetry.io/schemas/1.37.0"}},
            ]
        )
        assert result == {OTEL_SCHEMA_URL: "https://opentelemetry.io/schemas/1.37.0"}


# ---------------------------------------------------------------------------
# resolve_attr — alias-aware attribute lookup
# ---------------------------------------------------------------------------


class TestResolveAttr:
    def test_canonical_present_alias_absent(self):
        attrs = {OTEL_GENAI_PROVIDER_NAME: "openai"}
        assert resolve_attr(attrs, OTEL_GENAI_PROVIDER_NAME) == "openai"

    def test_canonical_absent_alias_present(self):
        attrs = {OTEL_GENAI_SYSTEM: "anthropic"}
        assert resolve_attr(attrs, OTEL_GENAI_PROVIDER_NAME) == "anthropic"

    def test_both_present_canonical_wins(self):
        attrs = {
            OTEL_GENAI_PROVIDER_NAME: "openai",
            OTEL_GENAI_SYSTEM: "old_value",
        }
        assert resolve_attr(attrs, OTEL_GENAI_PROVIDER_NAME) == "openai"

    def test_neither_present_returns_none(self):
        assert resolve_attr({}, OTEL_GENAI_PROVIDER_NAME) is None

    def test_unknown_canonical_key_with_no_alias_entry_returns_none(self):
        # A key with no entry in GENAI_ATTRIBUTE_ALIASES simply has no
        # fallback list; absence should not raise.
        assert resolve_attr({}, "gen_ai.some.unmapped.key") is None

    def test_alias_table_contains_expected_seed_entry(self):
        # Sanity check that the table wiring itself is intact.
        assert OTEL_GENAI_SYSTEM in GENAI_ATTRIBUTE_ALIASES.get(OTEL_GENAI_PROVIDER_NAME, [])


# ---------------------------------------------------------------------------
# resolve_schema_version
# ---------------------------------------------------------------------------


class TestResolveSchemaVersion:
    def test_valid_schema_url(self):
        assert resolve_schema_version("https://opentelemetry.io/schemas/1.37.0") == "1.37.0"

    def test_valid_schema_url_with_trailing_slash(self):
        assert resolve_schema_version("https://opentelemetry.io/schemas/1.4.0/") == "1.4.0"

    def test_valid_schema_url_with_prerelease(self):
        assert resolve_schema_version("https://opentelemetry.io/schemas/1.37.0-rc.1") == "1.37.0-rc.1"

    def test_valid_schema_url_with_build_metadata(self):
        assert resolve_schema_version("https://opentelemetry.io/schemas/1.37.0+build.5") == "1.37.0+build.5"

    def test_none_degrades_to_none(self):
        assert resolve_schema_version(None) is None

    def test_empty_string_degrades_to_none(self):
        assert resolve_schema_version("") is None

    def test_malformed_string_degrades_to_none(self):
        assert resolve_schema_version("not-a-schema-url") is None

    def test_non_version_last_path_segment_returns_none(self):
        assert resolve_schema_version("https://opentelemetry.io/schemas/1.4.0/extra") is None

    def test_non_string_input_degrades_to_none(self):
        # Defensive: attrs values are technically Any; must not raise.
        assert resolve_schema_version(123) is None  # type: ignore[arg-type]

    def test_never_raises_on_garbage_input(self):
        for garbage in (object(), [], {}, 1.5):
            assert resolve_schema_version(garbage) is None  # type: ignore[arg-type]


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
            OTEL_SCHEMA_URL: "https://opentelemetry.io/schemas/1.37.0",
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
        assert result["schema_version"] == "1.37.0"

    # -----------------------------------------------------------------
    # schema_version / schema_url resolution
    # -----------------------------------------------------------------

    def test_schema_version_newest_attribute_names_only(self):
        # Fixture with only newest attribute names (gen_ai.provider.name,
        # not gen_ai.system) plus a schema_url; all fields, including
        # schema_version, should populate normally.
        attrs = {
            OTEL_GENAI_PROVIDER_NAME: "openai",
            OTEL_GENAI_REQUEST_MODEL: "gpt-4o",
            OTEL_SCHEMA_URL: "https://opentelemetry.io/schemas/1.37.0",
        }
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["schema_version"] == "1.37.0"
        assert result["provider"] == "openai"
        assert result["request_model"] == "gpt-4o"

    def test_schema_version_with_aliased_attribute_names_only(self):
        # Older instrumentor: gen_ai.system present, gen_ai.provider.name
        # absent. provider should still populate via alias fallback, and
        # schema_version should resolve from an older schema_url.
        attrs = {
            OTEL_GENAI_SYSTEM: "anthropic",
            OTEL_SCHEMA_URL: "https://opentelemetry.io/schemas/1.20.0",
        }
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["provider"] == "anthropic"
        assert result["schema_version"] == "1.20.0"

    def test_schema_version_missing_schema_url_entirely(self):
        # No schema_url at all -> None, with no impact on other fields.
        attrs = {OTEL_GENAI_PROVIDER_NAME: "openai", OTEL_GENAI_REQUEST_MODEL: "gpt-4o"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["schema_version"] is None
        assert result["provider"] == "openai"
        assert result["request_model"] == "gpt-4o"

    def test_schema_version_malformed_schema_url(self):
        attrs = {OTEL_SCHEMA_URL: "not-a-real-schema-url"}
        result = extract_extended_model_info_from_attrs(attrs)
        assert result["schema_version"] is None


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
