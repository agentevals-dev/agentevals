"""Attribute level extraction helpers used by the live incremental extractor.

Batch evaluation runs on :mod:`agentevals.genai`. These helpers remain only for the
WebSocket era live path (``streaming/incremental_processor.py``, ``api/otlp_processing.py``)
and go away with it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, TypedDict, TypeVar

from .otlp_anyvalue import decode_attributes
from .trace_attrs import (
    ADK_LLM_REQUEST,
    ADK_LLM_RESPONSE,
    ADK_SCOPE_VALUE,
    ADK_TOOL_CALL_ARGS,
    ADK_TOOL_RESPONSE,
    GENAI_ATTRIBUTE_ALIASES,
    OTEL_ERROR_TYPE,
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
    OTEL_GENAI_TOOL_CALL_RESULT,
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
from .utils.genai_messages import (
    ASSISTANT_ROLES,
    USER_ROLES,
    extract_text_from_message,
    extract_tool_call_args_from_messages,
    parse_json_attr,
)

logger = logging.getLogger(__name__)

FORMAT_DETECTION_SPAN_LIMIT = 10

# ---------------------------------------------------------------------------
# Alias-aware attribute resolution
# ---------------------------------------------------------------------------

# Per the OTel schema URL spec (https://opentelemetry.io/docs/specs/otel/schemas/#schema-url),
# a schema URL has the form `http[s]://server[:port]/path/<version>`.
_SCHEMA_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def resolve_attr(attrs: dict[str, Any], canonical_key: str) -> Any | None:
    """Look up *canonical_key* in *attrs*, falling back to older aliased names.

    Resolution order:
    1. The canonical (current/newest) key, if present with a truthy value.
    2. Each alias in ``GENAI_ATTRIBUTE_ALIASES[canonical_key]``, in order,
       first truthy value wins.
    3. ``None`` if neither the canonical key nor any alias is present.

    The canonical value always takes precedence over aliases, even if both
    are present on the same span. This resolves only against the flat attrs
    dict at read time - it never mutates or rewrites stored/transmitted
    attributes.
    """
    value = attrs.get(canonical_key)
    if value:
        return value

    for alias in GENAI_ATTRIBUTE_ALIASES.get(canonical_key, []):
        alias_value = attrs.get(alias)
        if alias_value:
            return alias_value

    return None


def resolve_schema_version(schema_url: str | None) -> str | None:
    """Extract the version segment from a schema URL.

    Per the OTel schema URL spec, the version is the last path segment
    (e.g. "1.37.0" in "https://opentelemetry.io/schemas/1.37.0").

    It's scoped to that URL's schema family and only equals the OTel semconv
    version for the official OTel family. Degrades gracefully to None for
    missing, empty, or malformed input - never raises.
    """
    if not schema_url or not isinstance(schema_url, str):
        return None

    last_segment = schema_url.rstrip("/").rsplit("/", 1)[-1]
    if _SCHEMA_VERSION_RE.match(last_segment):
        return last_segment

    return None


# ---------------------------------------------------------------------------
# Pure extraction functions (operate on flat attribute dicts)
# ---------------------------------------------------------------------------


def extract_user_text_from_attrs(attrs: dict[str, Any]) -> str | None:
    """Extract user input text from span attributes, ADK-first."""
    llm_request_raw = attrs.get(ADK_LLM_REQUEST)
    if llm_request_raw:
        llm_request = parse_json(llm_request_raw)
        if isinstance(llm_request, dict):
            contents = llm_request.get("contents", llm_request.get("Contents", []))
            for content_dict in reversed(contents):
                if content_dict.get("role") != "user":
                    continue
                parts = content_dict.get("parts", [])
                text_parts = [p for p in parts if "text" in p]
                if text_parts:
                    return " ".join(p["text"] for p in text_parts)
            for content_dict in contents:
                if content_dict.get("role") == "user":
                    parts = content_dict.get("parts", [])
                    if parts:
                        return " ".join(p.get("text", "") for p in parts if "text" in p)

    messages_raw = attrs.get(OTEL_GENAI_INPUT_MESSAGES)
    if messages_raw:
        messages = parse_json_attr(messages_raw, "gen_ai.input.messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") in USER_ROLES:
                    text = extract_text_from_message(msg)
                    if text:
                        return text

    return None


def extract_agent_response_from_attrs(attrs: dict[str, Any]) -> str | None:
    """Extract agent response text from span attributes, ADK-first."""
    llm_response_raw = attrs.get(ADK_LLM_RESPONSE)
    if llm_response_raw:
        llm_response = parse_json(llm_response_raw)
        if isinstance(llm_response, dict):
            content_dict = llm_response.get("content", llm_response.get("Content", {}))
            if content_dict:
                parts_dicts = content_dict.get("parts", [])
                text_parts = [p for p in parts_dicts if "text" in p]
                if text_parts:
                    return " ".join(p["text"] for p in text_parts)

    messages_raw = attrs.get(OTEL_GENAI_OUTPUT_MESSAGES)
    if messages_raw:
        messages = parse_json_attr(messages_raw, "gen_ai.output.messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") in ASSISTANT_ROLES:
                    text = extract_text_from_message(msg)
                    if text:
                        return text

    return None


def extract_token_usage_from_attrs(
    attrs: dict[str, Any],
) -> tuple[int, int, str]:
    """Extract (input_tokens, output_tokens, model) from attributes, ADK-first."""
    model = attrs.get(OTEL_GENAI_REQUEST_MODEL, "unknown")

    llm_response_raw = attrs.get(ADK_LLM_RESPONSE)
    if llm_response_raw:
        llm_response = parse_json(llm_response_raw)
        if isinstance(llm_response, dict):
            usage = llm_response.get("usage_metadata", {})
            input_toks = usage.get("prompt_token_count", 0)
            output_toks = usage.get("candidates_token_count", 0)
            if input_toks or output_toks:
                llm_request_raw = attrs.get(ADK_LLM_REQUEST)
                if llm_request_raw:
                    llm_request = parse_json(llm_request_raw)
                    if isinstance(llm_request, dict) and "model" in llm_request:
                        model = llm_request["model"]
                return int(input_toks), int(output_toks), model

    input_toks = attrs.get(OTEL_GENAI_USAGE_INPUT_TOKENS, 0)
    output_toks = attrs.get(OTEL_GENAI_USAGE_OUTPUT_TOKENS, 0)
    if isinstance(input_toks, (int, float)) and isinstance(output_toks, (int, float)):
        if input_toks or output_toks:
            return int(input_toks), int(output_toks), model

    return 0, 0, model


_T = TypeVar("_T", int, float)


def _safe_cast(value: Any, target_type: type[_T], default: _T | None = None) -> _T | None:
    """Try to cast *value* to *target_type*, returning *default* on failure."""
    if value is None:
        return default
    try:
        return target_type(value)
    except (TypeError, ValueError):
        return default


def _parse_finish_reasons(raw: Any) -> list[str]:
    """Parse finish reasons from a list, JSON string, or plain string."""
    if isinstance(raw, list):
        return [str(r) for r in raw]
    if isinstance(raw, str):
        parsed = parse_json(raw)
        if isinstance(parsed, list):
            return [str(r) for r in parsed]
        if raw:
            return [raw]
    return []


class ExtendedModelInfo(TypedDict):
    request_model: str | None
    response_model: str | None
    provider: str | None
    finish_reasons: list[str]
    response_id: str | None
    temperature: float | None
    max_tokens: int | None
    cache_creation_tokens: int
    cache_read_tokens: int
    error_type: str | None
    schema_version: str | None


def extract_extended_model_info_from_attrs(attrs: dict[str, Any]) -> ExtendedModelInfo:
    """Extract extended model and provider metadata from span attributes.

    Uses the alias-resolving lookup for attributes with known historical
    renames (e.g. provider falls back to gen_ai.system when
    gen_ai.provider.name - the canonical, current name - is absent, for
    backward compat with pre-v1.37.0 instrumentors).

    ``schema_version`` is resolved from the scope's ``schema_url`` (captured
    at ingest as the ``otel.schema_url`` attribute) and is ``None`` when
    absent or malformed. This is purely informational metadata about which
    schema version emitted the span - it is never used to detect whether a
    span is GenAI (that remains the sole responsibility of the existing
    gen_ai.* presence checks).
    """
    return {
        "request_model": attrs.get(OTEL_GENAI_REQUEST_MODEL),
        "response_model": attrs.get(OTEL_GENAI_RESPONSE_MODEL),
        "provider": resolve_attr(attrs, OTEL_GENAI_PROVIDER_NAME),
        "finish_reasons": _parse_finish_reasons(attrs.get(OTEL_GENAI_RESPONSE_FINISH_REASONS)),
        "response_id": attrs.get(OTEL_GENAI_RESPONSE_ID),
        "temperature": _safe_cast(attrs.get(OTEL_GENAI_REQUEST_TEMPERATURE), float),
        "max_tokens": _safe_cast(attrs.get(OTEL_GENAI_REQUEST_MAX_TOKENS), int),
        "cache_creation_tokens": _safe_cast(attrs.get(OTEL_GENAI_USAGE_CACHE_CREATION_TOKENS), int, 0),
        "cache_read_tokens": _safe_cast(attrs.get(OTEL_GENAI_USAGE_CACHE_READ_TOKENS), int, 0),
        "error_type": attrs.get(OTEL_ERROR_TYPE),
        "schema_version": resolve_schema_version(attrs.get(OTEL_SCHEMA_URL)),
    }


def extract_tool_call_from_attrs(
    attrs: dict[str, Any], operation_name: str = "", span_id: str = ""
) -> dict[str, Any] | None:
    """Extract tool call info from span attributes. Returns {id, name, args} or None."""
    tool_name = attrs.get(OTEL_GENAI_TOOL_NAME)
    if not tool_name:
        if operation_name.startswith("execute_tool "):
            tool_name = operation_name[len("execute_tool ") :]
        else:
            return None

    tool_call_id = attrs.get(OTEL_GENAI_TOOL_CALL_ID) or span_id or "unknown"

    args_raw = attrs.get(OTEL_GENAI_TOOL_CALL_ARGUMENTS)
    if not args_raw:
        args_raw = attrs.get(ADK_TOOL_CALL_ARGS)

    args: dict = {}
    if args_raw:
        parsed = parse_json_attr(args_raw, "tool.call.arguments")
        if isinstance(parsed, dict):
            args = parsed

    if not args:
        messages_raw = attrs.get(OTEL_GENAI_INPUT_MESSAGES)
        if messages_raw:
            fallback_args, fallback_id = extract_tool_call_args_from_messages(messages_raw, tool_name)
            if fallback_args:
                args = fallback_args
            if fallback_id:
                tool_call_id = fallback_id

    result: dict[str, Any] = {"id": tool_call_id, "name": tool_name, "args": args}

    tool_type = attrs.get(OTEL_GENAI_TOOL_TYPE)
    if tool_type:
        result["type"] = tool_type

    tool_description = attrs.get(OTEL_GENAI_TOOL_DESCRIPTION)
    if tool_description:
        result["description"] = tool_description

    return result


def parse_tool_response_content(content: Any) -> dict:
    """Parse raw tool response content into a response dict.

    Handles str (tries JSON parse), dict (pass-through), and other types (stringified).
    On JSON parse failure, wraps raw content as {"result": content}.
    """
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {"result": str(parsed)}
        except (json.JSONDecodeError, TypeError):
            return {"result": content}
    elif isinstance(content, dict):
        return content
    return {"result": str(content)}


def extract_tool_result_from_attrs(attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Extract tool result from span attributes, ADK-first.

    Checks (in order):
    1. ADK tool response attribute
    2. GenAI semconv tool call result attribute
    3. gen_ai.output.messages for tool_call_response parts (Strands format)

    Returns {"response": <parsed dict>, "isError": bool} or None if no result present.
    """
    raw = attrs.get(ADK_TOOL_RESPONSE)
    if not raw:
        raw = attrs.get(OTEL_GENAI_TOOL_CALL_RESULT)

    if raw:
        parsed = parse_tool_response_content(raw)
        if isinstance(parsed, dict):
            is_error = bool(parsed.get("isError", False))
            return {"response": parsed, "isError": is_error}

    output_msgs_raw = attrs.get(OTEL_GENAI_OUTPUT_MESSAGES)
    if output_msgs_raw:
        messages = parse_json_attr(output_msgs_raw, "gen_ai.output.messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                for part in msg.get("parts", []):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_call_response" and "response" in part:
                        resp = part["response"]
                        if isinstance(resp, list):
                            texts = [t.get("text", "") for t in resp if isinstance(t, dict) and "text" in t]
                            parsed = parse_tool_response_content(" ".join(texts))
                        elif isinstance(resp, dict):
                            parsed = resp
                        else:
                            continue
                        return {"response": parsed, "isError": bool(parsed.get("isError", False))}

    return None


# ---------------------------------------------------------------------------
# Span-level convenience wrappers
# ---------------------------------------------------------------------------


def flatten_otlp_attributes(attrs_list: list[dict]) -> dict[str, Any]:
    """Convert OTLP attributes array [{key, value: {stringValue|...}}] to flat dict.

    Delegates to the shared ``AnyValue`` decoder so array/kvlist/bytes
    attributes survive instead of being dropped.
    """
    return decode_attributes(attrs_list)


# ---------------------------------------------------------------------------
# Format-aware extractor strategy
# ---------------------------------------------------------------------------


def parse_json(raw: str | dict | Any) -> dict | list | Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}
