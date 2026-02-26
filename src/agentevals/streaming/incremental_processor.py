"""Incremental span processor for extracting conversation elements in real-time.

Processes OTLP spans as they arrive and extracts:
- User input from call_llm spans
- Tool calls from execute_tool spans
- Agent responses from call_llm spans
- Token usage information

This enables real-time display of agent execution progress without waiting for
session completion.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tag keys from ADK OTel instrumentation
_TAG_SCOPE = "otel.scope.name"
_ADK_SCOPE = "gcp.vertex.agent"
_TAG_LLM_REQUEST = "gcp.vertex.agent.llm_request"
_TAG_LLM_RESPONSE = "gcp.vertex.agent.llm_response"
_TAG_TOOL_NAME = "gen_ai.tool.name"
_TAG_TOOL_CALL_ID = "gen_ai.tool.call.id"
_TAG_TOOL_CALL_ARGS = "gcp.vertex.agent.tool_call_args"
_TAG_INVOCATION_ID = "gcp.vertex.agent.invocation_id"


class IncrementalInvocationExtractor:
    """Extracts conversation elements from spans as they arrive."""

    def __init__(self):
        self.seen_user_input = set()  # Track which invocations have user input sent
        self.seen_tool_calls = {}  # invocation_id -> set of tool call IDs
        self.seen_agent_response = set()  # Track which invocations have agent response sent
        self.llm_spans_by_invocation = {}  # invocation_id -> list of call_llm spans
        self.token_totals = {}  # invocation_id -> {inputTokens, outputTokens, model}

    def process_span(self, span: dict) -> list[dict]:
        """Process a single OTLP span and return conversation updates to broadcast.

        Args:
            span: OTLP JSON span dictionary

        Returns:
            List of update events to broadcast via SSE
        """
        updates = []
        operation_name = span.get("name", "")

        # Extract attributes from OTLP format
        attributes = self._extract_attributes(span.get("attributes", []))

        # Only process ADK spans
        if attributes.get(_TAG_SCOPE) != _ADK_SCOPE:
            return updates

        invocation_id = self._get_invocation_id(span, attributes)
        if not invocation_id:
            return updates

        # User input detection (from first call_llm span)
        if operation_name.startswith("call_llm"):
            # Track LLM span for later response extraction
            if invocation_id not in self.llm_spans_by_invocation:
                self.llm_spans_by_invocation[invocation_id] = []
            self.llm_spans_by_invocation[invocation_id].append(span)

            # Extract user input from first LLM call
            if invocation_id not in self.seen_user_input:
                user_text = self._extract_user_input(span, attributes)
                if user_text:
                    updates.append({
                        "type": "user_input",
                        "invocationId": invocation_id,
                        "text": user_text,
                        "timestamp": int(span.get("startTimeUnixNano", 0)) / 1e9,
                    })
                    self.seen_user_input.add(invocation_id)

            # Extract agent response from LLM response
            agent_text = self._extract_agent_response(span, attributes)
            if agent_text and invocation_id not in self.seen_agent_response:
                updates.append({
                    "type": "agent_response",
                    "invocationId": invocation_id,
                    "text": agent_text,
                    "timestamp": int(span.get("endTimeUnixNano", 0)) / 1e9,
                })
                self.seen_agent_response.add(invocation_id)

            token_info = self._extract_token_info(span, attributes)
            if token_info:
                if invocation_id not in self.token_totals:
                    self.token_totals[invocation_id] = {
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "model": token_info.get("model", "unknown"),
                    }

                self.token_totals[invocation_id]["inputTokens"] += token_info.get("inputTokens", 0)
                self.token_totals[invocation_id]["outputTokens"] += token_info.get("outputTokens", 0)

                logger.info("Token update for %s: +%d input, +%d output (total: %d/%d)",
                    invocation_id,
                    token_info.get("inputTokens", 0),
                    token_info.get("outputTokens", 0),
                    self.token_totals[invocation_id]["inputTokens"],
                    self.token_totals[invocation_id]["outputTokens"])

                updates.append({
                    "type": "token_update",
                    "invocationId": invocation_id,
                    "inputTokens": token_info.get("inputTokens", 0),
                    "outputTokens": token_info.get("outputTokens", 0),
                    "model": token_info.get("model", "unknown"),
                })

        # Tool call detection (from execute_tool spans)
        elif operation_name.startswith("execute_tool"):
            tool_call = self._extract_tool_call(span, attributes)
            if tool_call:
                call_id = tool_call["id"]
                if invocation_id not in self.seen_tool_calls:
                    self.seen_tool_calls[invocation_id] = set()

                if call_id not in self.seen_tool_calls[invocation_id]:
                    updates.append({
                        "type": "tool_call",
                        "invocationId": invocation_id,
                        "toolCall": tool_call,
                        "timestamp": int(span.get("startTimeUnixNano", 0)) / 1e9,
                    })
                    self.seen_tool_calls[invocation_id].add(call_id)

        return updates

    def _extract_attributes(self, attrs_list: list[dict]) -> dict:
        """Convert OTLP attributes array to flat dict.

        OTLP attributes are [{key, value: {stringValue|intValue|...}}]
        """
        result = {}
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

    def _get_invocation_id(self, span: dict, attributes: dict) -> str | None:
        """Extract invocation ID from span attributes or use span ID as fallback."""
        invocation_id = attributes.get(_TAG_INVOCATION_ID)
        if invocation_id:
            return invocation_id

        # Fallback: use parent span ID or span ID
        # For ADK, invocations are typically marked by invoke_agent spans
        # For now, we'll extract from the span itself
        parent_span_id = span.get("parentSpanId")
        if parent_span_id:
            return parent_span_id

        return span.get("spanId")

    def _extract_user_input(self, span: dict, attributes: dict) -> str | None:
        """Extract user text from llm_request tag in call_llm span."""
        llm_request_raw = attributes.get(_TAG_LLM_REQUEST)
        if not llm_request_raw:
            return None

        try:
            llm_request = json.loads(llm_request_raw)
            contents = llm_request.get("contents", [])

            # Look for last user content with text parts (skip function_response)
            for content_dict in reversed(contents):
                if content_dict.get("role") != "user":
                    continue
                parts = content_dict.get("parts", [])
                text_parts = [p for p in parts if "text" in p]
                if text_parts:
                    return " ".join(p["text"] for p in text_parts)

            # Fallback: any user content
            for content_dict in contents:
                if content_dict.get("role") == "user":
                    parts = content_dict.get("parts", [])
                    if parts:
                        return " ".join(p.get("text", "") for p in parts if "text" in p)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse llm_request: %s", e)

        return None

    def _extract_agent_response(self, span: dict, attributes: dict) -> str | None:
        """Extract agent text from llm_response tag in call_llm span."""
        llm_response_raw = attributes.get(_TAG_LLM_RESPONSE)
        if not llm_response_raw:
            return None

        try:
            llm_response = json.loads(llm_response_raw)
            content_dict = llm_response.get("content", {})
            if not content_dict:
                return None

            parts_dicts = content_dict.get("parts", [])
            # Extract text parts (not function_call parts)
            text_parts = [p for p in parts_dicts if "text" in p]
            if text_parts:
                return " ".join(p["text"] for p in text_parts)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse llm_response: %s", e)

        return None

    def _extract_tool_call(self, span: dict, attributes: dict) -> dict | None:
        """Extract tool name, args, and ID from execute_tool span."""
        tool_name = attributes.get(_TAG_TOOL_NAME)

        # Fallback: parse tool name from operationName "execute_tool <name>"
        if not tool_name:
            operation_name = span.get("name", "")
            if operation_name.startswith("execute_tool "):
                tool_name = operation_name[len("execute_tool "):]
            else:
                logger.warning("execute_tool span has no tool name")
                return None

        tool_call_id = attributes.get(_TAG_TOOL_CALL_ID, span.get("spanId", "unknown"))

        args_raw = attributes.get(_TAG_TOOL_CALL_ARGS, "{}")
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            args = {}

        return {
            "id": tool_call_id,
            "name": tool_name,
            "args": args,
        }

    def _extract_token_info(self, span: dict, attributes: dict) -> dict | None:
        """Extract token usage and model from call_llm span.

        Token counts are typically in the llm_response metadata.
        """
        llm_response_raw = attributes.get(_TAG_LLM_RESPONSE)
        if not llm_response_raw:
            logger.debug("No llm_response in span attributes")
            return None

        try:
            llm_response = json.loads(llm_response_raw)
            logger.debug("Parsed llm_response: %s", llm_response.keys())

            usage = llm_response.get("usage_metadata", {})
            logger.debug("Usage metadata: %s", usage)

            input_tokens = usage.get("prompt_token_count", 0)
            output_tokens = usage.get("candidates_token_count", 0)

            model = "unknown"
            llm_request_raw = attributes.get(_TAG_LLM_REQUEST)
            if llm_request_raw:
                llm_request = json.loads(llm_request_raw)
                model = llm_request.get("model", "unknown")

            if input_tokens or output_tokens:
                logger.info("Extracted tokens: input=%d, output=%d, model=%s",
                    input_tokens, output_tokens, model)
                return {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "model": model,
                }
            else:
                logger.warning("Token counts are 0 in usage_metadata: %s", usage)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Could not extract token info: %s", e)

        return None
