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

# GenAI semantic conventions
_TAG_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
_TAG_GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
_TAG_GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
_TAG_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_TAG_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_TAG_GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
_TAG_GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"


class IncrementalInvocationExtractor:
    """Extracts conversation elements from spans and logs as they arrive."""

    def __init__(self):
        self.seen_user_input = set()
        self.seen_tool_calls = {}
        self.seen_agent_response = set()
        self.llm_spans_by_invocation = {}
        self.token_totals = {}
        self.current_invocation_id = None
        self.seen_message_contents = set()  # Track message contents to avoid duplicates

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

        is_adk = attributes.get(_TAG_SCOPE) == _ADK_SCOPE
        is_genai = attributes.get(_TAG_GEN_AI_REQUEST_MODEL) or attributes.get(_TAG_GEN_AI_INPUT_MESSAGES)

        if not (is_adk or is_genai):
            return updates

        invocation_id = self._get_invocation_id(span, attributes)
        if not invocation_id:
            return updates

        # Track current invocation ID for log processing
        self.current_invocation_id = invocation_id

        # User input detection (from call_llm or LLM call spans)
        is_llm_span = operation_name.startswith("call_llm") or is_genai
        if is_llm_span:
            # Track LLM span for later response extraction
            if invocation_id not in self.llm_spans_by_invocation:
                self.llm_spans_by_invocation[invocation_id] = []
            self.llm_spans_by_invocation[invocation_id].append(span)

            # Extract user input from first LLM call
            if invocation_id not in self.seen_user_input:
                user_text = self._extract_user_input(span, attributes)
                if user_text:
                    logger.debug(f"Extracted user input for invocation {invocation_id}")
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
                logger.debug(f"Extracted agent response for invocation {invocation_id}")
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

                logger.debug("Token update for %s: +%d input, +%d output",
                    invocation_id,
                    token_info.get("inputTokens", 0),
                    token_info.get("outputTokens", 0))

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

    def process_log(self, log_event: dict) -> list[dict]:
        """Process a GenAI log event and extract conversation updates.

        Args:
            log_event: Log event dict with event_name, body, attributes

        Returns:
            List of update events to broadcast via SSE
        """
        updates = []
        event_name = log_event.get("event_name", "")
        body = log_event.get("body", {})

        # Use current invocation ID (from most recent span)
        if not self.current_invocation_id:
            return updates

        invocation_id = self.current_invocation_id

        # Extract user messages (gen_ai.user.message)
        if event_name == "gen_ai.user.message":
            if isinstance(body, dict) and "content" in body:
                user_text = body["content"]
                message_key = f"user:{user_text}"
                if user_text and message_key not in self.seen_message_contents:
                    logger.debug(f"Extracted user input from log for invocation {invocation_id}")
                    updates.append({
                        "type": "user_input",
                        "invocationId": invocation_id,
                        "text": user_text,
                        "timestamp": log_event.get("timestamp", 0),
                    })
                    self.seen_message_contents.add(message_key)
                    self.seen_user_input.add(invocation_id)

        # Extract assistant messages (gen_ai.assistant.message or gen_ai.choice)
        elif event_name in ("gen_ai.assistant.message", "gen_ai.choice"):
            agent_text = None

            if isinstance(body, dict):
                # Check for direct content
                if "content" in body:
                    agent_text = body["content"]
                # Check for message.content (gen_ai.choice format)
                elif "message" in body and isinstance(body["message"], dict):
                    if "content" in body["message"]:
                        agent_text = body["message"]["content"]

            if agent_text:
                message_key = f"agent:{agent_text}"
                if message_key not in self.seen_message_contents:
                    logger.debug(f"Extracted agent response from log for invocation {invocation_id}")
                    updates.append({
                        "type": "agent_response",
                        "invocationId": invocation_id,
                        "text": agent_text,
                        "timestamp": log_event.get("timestamp", 0),
                    })
                    self.seen_message_contents.add(message_key)
                    self.seen_agent_response.add(invocation_id)

            # Extract tool calls from assistant message
            if isinstance(body, dict):
                tool_calls = None
                if "tool_calls" in body:
                    tool_calls = body["tool_calls"]
                elif "message" in body and isinstance(body["message"], dict) and "tool_calls" in body["message"]:
                    tool_calls = body["message"]["tool_calls"]

                if tool_calls and isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            tool_id = tc.get("id", "unknown")
                            tool_key = f"tool:{tool_id}"

                            if tool_key not in self.seen_message_contents:
                                tool_call = {
                                    "id": tool_id,
                                    "name": tc.get("function", {}).get("name", "unknown") if "function" in tc else tc.get("name", "unknown"),
                                    "args": {},
                                }

                                if "function" in tc and "arguments" in tc["function"]:
                                    import json
                                    try:
                                        tool_call["args"] = json.loads(tc["function"]["arguments"])
                                    except:
                                        tool_call["args"] = {}

                                logger.debug(f"Extracted tool call from log for invocation {invocation_id}")
                                updates.append({
                                    "type": "tool_call",
                                    "invocationId": invocation_id,
                                    "toolCall": tool_call,
                                    "timestamp": log_event.get("timestamp", 0),
                                })
                                self.seen_message_contents.add(tool_key)

                                if invocation_id not in self.seen_tool_calls:
                                    self.seen_tool_calls[invocation_id] = set()
                                self.seen_tool_calls[invocation_id].add(tool_id)

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
        """Extract user text from llm_request tag (ADK) or gen_ai.input.messages (GenAI)."""
        # Try GenAI format first
        messages_raw = attributes.get(_TAG_GEN_AI_INPUT_MESSAGES)
        if messages_raw:
            try:
                messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
                if isinstance(messages, list):
                    for msg in reversed(messages):
                        if isinstance(msg, dict) and msg.get("role") in ("user", "human"):
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                return content
                            elif isinstance(content, list):
                                texts = [item.get("text", "") for item in content if isinstance(item, dict) and "text" in item]
                                if texts:
                                    return " ".join(texts)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to parse gen_ai.input.messages: %s", e)

        # Try ADK format
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
        """Extract agent text from llm_response tag (ADK) or gen_ai.output.messages (GenAI)."""
        # Try GenAI format first
        messages_raw = attributes.get(_TAG_GEN_AI_OUTPUT_MESSAGES)
        if messages_raw:
            try:
                messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
                if isinstance(messages, list):
                    for msg in messages:
                        if isinstance(msg, dict) and msg.get("role") in ("assistant", "model", "ai"):
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                return content
                            elif isinstance(content, list):
                                texts = [item.get("text", "") for item in content if isinstance(item, dict) and "text" in item]
                                if texts:
                                    return " ".join(texts)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to parse gen_ai.output.messages: %s", e)

        # Try ADK format
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
        """Extract tool name, args, and ID from execute_tool span or GenAI tool span."""
        tool_name = attributes.get(_TAG_TOOL_NAME)

        # Fallback: parse tool name from operationName "execute_tool <name>"
        if not tool_name:
            operation_name = span.get("name", "")
            if operation_name.startswith("execute_tool "):
                tool_name = operation_name[len("execute_tool "):]
            else:
                logger.warning("Tool span has no tool name")
                return None

        tool_call_id = attributes.get(_TAG_TOOL_CALL_ID, span.get("spanId", "unknown"))

        # Try GenAI format first
        args_raw = attributes.get(_TAG_GEN_AI_TOOL_CALL_ARGUMENTS)
        if not args_raw:
            # Fall back to ADK format
            args_raw = attributes.get(_TAG_TOOL_CALL_ARGS, "{}")

        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse tool args: {e}")
            args = {}

        return {
            "id": tool_call_id,
            "name": tool_name,
            "args": args,
        }

    def _extract_token_info(self, span: dict, attributes: dict) -> dict | None:
        """Extract token usage and model from call_llm span (ADK or GenAI format)."""
        # Try GenAI format first
        input_tokens = attributes.get(_TAG_GEN_AI_USAGE_INPUT_TOKENS, 0)
        output_tokens = attributes.get(_TAG_GEN_AI_USAGE_OUTPUT_TOKENS, 0)
        model = attributes.get(_TAG_GEN_AI_REQUEST_MODEL, "unknown")

        if input_tokens or output_tokens:
            return {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "model": model,
            }

        # Fall back to ADK format
        llm_response_raw = attributes.get(_TAG_LLM_RESPONSE)
        if not llm_response_raw:
            return None

        try:
            llm_response = json.loads(llm_response_raw)
            usage = llm_response.get("usage_metadata", {})

            input_tokens = usage.get("prompt_token_count", 0)
            output_tokens = usage.get("candidates_token_count", 0)

            model = "unknown"
            llm_request_raw = attributes.get(_TAG_LLM_REQUEST)
            if llm_request_raw:
                llm_request = json.loads(llm_request_raw)
                model = llm_request.get("model", "unknown")

            if input_tokens or output_tokens:
                return {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "model": model,
                }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Could not extract token info: %s", e)

        return None
