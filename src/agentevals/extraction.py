"""Shared extraction functions and format-aware extractor strategy.

Provides:
- Pure functions that extract user text, agent responses, token usage, and tool
  calls from flat attribute dictionaries (usable by both Span-based converters
  and the raw-OTLP-dict incremental processor).
- Span classification predicates (is_llm_span, is_tool_span, etc.).
- A lightweight TraceFormatExtractor protocol with ADK and GenAI implementations,
  plus a get_extractor() dispatcher for trace-level format selection.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .loader.base import Span, Trace
from .trace_attrs import (
    ADK_LLM_REQUEST,
    ADK_LLM_RESPONSE,
    ADK_SCOPE_VALUE,
    ADK_TOOL_CALL_ARGS,
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OP,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_GENAI_REQUEST_MODEL,
    OTEL_GENAI_TOOL_CALL_ARGUMENTS,
    OTEL_GENAI_TOOL_CALL_ID,
    OTEL_GENAI_TOOL_NAME,
    OTEL_GENAI_USAGE_INPUT_TOKENS,
    OTEL_GENAI_USAGE_OUTPUT_TOKENS,
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
# Pure extraction functions (operate on flat attribute dicts)
# ---------------------------------------------------------------------------


def extract_user_text_from_attrs(attrs: dict[str, Any]) -> str | None:
    """Extract user input text from span attributes, ADK-first."""
    llm_request_raw = attrs.get(ADK_LLM_REQUEST)
    if llm_request_raw:
        llm_request = parse_json(llm_request_raw)
        if isinstance(llm_request, dict):
            for content_dict in reversed(llm_request.get("contents", [])):
                if content_dict.get("role") != "user":
                    continue
                parts = content_dict.get("parts", [])
                text_parts = [p for p in parts if "text" in p]
                if text_parts:
                    return " ".join(p["text"] for p in text_parts)
            for content_dict in llm_request.get("contents", []):
                if content_dict.get("role") == "user":
                    parts = content_dict.get("parts", [])
                    if parts:
                        return " ".join(
                            p.get("text", "") for p in parts if "text" in p
                        )

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
            content_dict = llm_response.get("content", {})
            if content_dict:
                parts_dicts = content_dict.get("parts", [])
                text_parts = [p for p in parts_dicts if "text" in p]
                if text_parts:
                    return " ".join(p["text"] for p in text_parts)

    messages_raw = attrs.get(OTEL_GENAI_OUTPUT_MESSAGES)
    if messages_raw:
        messages = parse_json_attr(messages_raw, "gen_ai.output.messages")
        if isinstance(messages, list):
            for msg in messages:
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


def extract_tool_call_from_attrs(
    attrs: dict[str, Any], operation_name: str = "", span_id: str = ""
) -> dict[str, Any] | None:
    """Extract tool call info from span attributes. Returns {id, name, args} or None."""
    tool_name = attrs.get(OTEL_GENAI_TOOL_NAME)
    if not tool_name:
        if operation_name.startswith("execute_tool "):
            tool_name = operation_name[len("execute_tool "):]
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
            fallback_args, fallback_id = extract_tool_call_args_from_messages(
                messages_raw, tool_name
            )
            if fallback_args:
                args = fallback_args
            if fallback_id:
                tool_call_id = fallback_id

    return {"id": tool_call_id, "name": tool_name, "args": args}


# ---------------------------------------------------------------------------
# Span classification helpers
# ---------------------------------------------------------------------------


def is_adk_scope(span: Span) -> bool:
    return span.get_tag(OTEL_SCOPE) == ADK_SCOPE_VALUE


def is_llm_span(span: Span) -> bool:
    return (
        span.get_tag(OTEL_GENAI_REQUEST_MODEL) is not None
        or span.get_tag(OTEL_GENAI_INPUT_MESSAGES) is not None
    )


def is_tool_span(span: Span) -> bool:
    return span.get_tag(OTEL_GENAI_TOOL_NAME) is not None


def is_invocation_span(span: Span) -> bool:
    """Check if a span represents an agent invocation.

    Checks gen_ai.operation.name first (reliable for Strands and ADK),
    then falls back to keyword heuristics on the operation name.
    """
    op_name_attr = span.get_tag(OTEL_GENAI_OP)
    if op_name_attr == "invoke_agent":
        return True

    op_lower = span.operation_name.lower()
    invocation_keywords = ["agent", "chain", "executor", "workflow"]
    return any(keyword in op_lower for keyword in invocation_keywords)


# ---------------------------------------------------------------------------
# OTLP attribute flattening (shared by incremental_processor and processor)
# ---------------------------------------------------------------------------


def flatten_otlp_attributes(attrs_list: list[dict]) -> dict[str, Any]:
    """Convert OTLP attributes array [{key, value: {stringValue|...}}] to flat dict."""
    result: dict[str, Any] = {}
    for attr in attrs_list:
        key = attr.get("key", "")
        value_obj = attr.get("value", {})
        if "stringValue" in value_obj:
            result[key] = value_obj["stringValue"]
        elif "intValue" in value_obj:
            result[key] = int(value_obj["intValue"])
        elif "doubleValue" in value_obj:
            result[key] = float(value_obj["doubleValue"])
        elif "boolValue" in value_obj:
            result[key] = value_obj["boolValue"]
    return result


# ---------------------------------------------------------------------------
# Format-aware extractor strategy
# ---------------------------------------------------------------------------


class TraceFormatExtractor(Protocol):
    def detect(self, trace: Trace) -> bool: ...
    def format_name(self) -> str: ...
    def find_invocation_spans(self, trace: Trace) -> list[Span]: ...
    def find_llm_spans_in(self, root: Span) -> list[Span]: ...
    def find_tool_spans_in(self, root: Span) -> list[Span]: ...
    def classify_span(self, span: Span) -> str | None:
        """Return 'llm', 'tool', 'invocation', or None."""
        ...


class AdkExtractor:
    def detect(self, trace: Trace) -> bool:
        for span in trace.all_spans[:FORMAT_DETECTION_SPAN_LIMIT]:
            if is_adk_scope(span):
                return True
        for span in trace.all_spans[FORMAT_DETECTION_SPAN_LIMIT:]:
            if is_adk_scope(span):
                return True
        return False

    def format_name(self) -> str:
        return "adk"

    def find_invocation_spans(self, trace: Trace) -> list[Span]:
        matches = [
            s for s in trace.all_spans
            if is_adk_scope(s) and s.operation_name.startswith("invoke_agent")
        ]
        matches.sort(key=lambda s: s.start_time)
        return matches

    def find_llm_spans_in(self, root: Span) -> list[Span]:
        results: list[Span] = []
        self._walk(root, lambda s: s.operation_name.startswith("call_llm"), results)
        results.sort(key=lambda s: s.start_time)
        return results

    def find_tool_spans_in(self, root: Span) -> list[Span]:
        results: list[Span] = []
        self._walk(root, lambda s: s.operation_name.startswith("execute_tool"), results)
        results.sort(key=lambda s: s.start_time)
        return results

    def classify_span(self, span: Span) -> str | None:
        if not is_adk_scope(span):
            return None
        if span.operation_name.startswith("invoke_agent"):
            return "invocation"
        if span.operation_name.startswith("call_llm"):
            return "llm"
        if span.operation_name.startswith("execute_tool"):
            return "tool"
        return None

    @staticmethod
    def _walk(span: Span, predicate, acc: list[Span]) -> None:
        for child in span.children:
            if predicate(child):
                acc.append(child)
            AdkExtractor._walk(child, predicate, acc)


class GenAIExtractor:
    def detect(self, trace: Trace) -> bool:
        for span in trace.all_spans[:FORMAT_DETECTION_SPAN_LIMIT]:
            if span.get_tag(OTEL_GENAI_REQUEST_MODEL) or span.get_tag(OTEL_GENAI_INPUT_MESSAGES):
                return True
        for span in trace.all_spans[FORMAT_DETECTION_SPAN_LIMIT:]:
            if span.get_tag(OTEL_GENAI_REQUEST_MODEL) or span.get_tag(OTEL_GENAI_INPUT_MESSAGES):
                return True
        return False

    def format_name(self) -> str:
        return "genai"

    def find_invocation_spans(self, trace: Trace) -> list[Span]:
        candidates = [s for s in trace.root_spans if is_invocation_span(s)]
        if not candidates:
            candidates = [s for s in trace.root_spans if self._has_llm_children(s)]
        if not candidates and trace.root_spans:
            llm_spans = [s for s in trace.root_spans if is_llm_span(s)]
            candidates = llm_spans if llm_spans else list(trace.root_spans)
        candidates.sort(key=lambda s: s.start_time)
        return candidates

    def find_llm_spans_in(self, root: Span) -> list[Span]:
        results: list[Span] = []
        self._walk(root, is_llm_span, results)
        results.sort(key=lambda s: s.start_time)
        return results

    def find_tool_spans_in(self, root: Span) -> list[Span]:
        results: list[Span] = []
        self._walk(root, is_tool_span, results)
        results.sort(key=lambda s: s.start_time)
        return results

    def classify_span(self, span: Span) -> str | None:
        if is_invocation_span(span):
            return "invocation"
        if is_llm_span(span):
            return "llm"
        if is_tool_span(span):
            return "tool"
        return None

    @staticmethod
    def _has_llm_children(span: Span) -> bool:
        for child in span.children:
            if is_llm_span(child):
                return True
            if GenAIExtractor._has_llm_children(child):
                return True
        return False

    @staticmethod
    def _walk(span: Span, predicate, acc: list[Span]) -> None:
        if predicate(span):
            acc.append(span)
        for child in span.children:
            GenAIExtractor._walk(child, predicate, acc)


# Registry: ADK checked first (richer data, more specific detection).
_EXTRACTORS: list[TraceFormatExtractor] = [AdkExtractor(), GenAIExtractor()]  # type: ignore[list-item]


def get_extractor(trace: Trace) -> TraceFormatExtractor:
    for ext in _EXTRACTORS:
        if ext.detect(trace):
            logger.debug("Trace %s: detected format %s", trace.trace_id, ext.format_name())
            return ext
    logger.warning("Trace %s: no format detected, defaulting to ADK", trace.trace_id)
    return _EXTRACTORS[0]


# ---------------------------------------------------------------------------
# Internal helpers
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
