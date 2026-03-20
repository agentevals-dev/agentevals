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

import logging

from ..extraction import (
    extract_agent_response_from_attrs,
    extract_token_usage_from_attrs,
    extract_tool_call_from_attrs,
    extract_tool_result_from_attrs,
    extract_user_text_from_attrs,
    flatten_otlp_attributes,
    parse_tool_response_content,
)
from ..trace_attrs import (
    ADK_INVOCATION_ID,
    ADK_SCOPE_VALUE,
    OTEL_GENAI_REQUEST_MODEL,
    OTEL_GENAI_TOOL_NAME,
    OTEL_SCOPE,
)
from ..utils.genai_messages import parse_json_attr

logger = logging.getLogger(__name__)


def _normalize_ts(raw_ts) -> float:
    """Normalize a nanosecond timestamp (string or int) to seconds."""
    try:
        ns = int(raw_ts)
    except (TypeError, ValueError):
        return 0.0
    if ns > 1e15:
        return ns / 1e9
    return float(ns)


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
        self.tool_names_by_id: dict[str, str] = {}  # tool_call_id -> tool_name

    def process_span(self, span: dict) -> list[dict]:
        """Process a single OTLP span and return conversation updates to broadcast.

        Args:
            span: OTLP JSON span dictionary

        Returns:
            List of update events to broadcast via SSE
        """
        updates = []
        operation_name = span.get("name", "")

        attributes = flatten_otlp_attributes(span.get("attributes", []))

        is_adk = attributes.get(OTEL_SCOPE) == ADK_SCOPE_VALUE
        is_genai_llm = bool(attributes.get(OTEL_GENAI_REQUEST_MODEL))
        is_genai_tool = bool(attributes.get(OTEL_GENAI_TOOL_NAME))

        if not (is_adk or is_genai_llm or is_genai_tool):
            return updates

        invocation_id = self._get_invocation_id(span, attributes)
        if not invocation_id:
            return updates

        self.current_invocation_id = invocation_id

        is_llm = operation_name.startswith("call_llm") or is_genai_llm
        if is_llm:
            if invocation_id not in self.llm_spans_by_invocation:
                self.llm_spans_by_invocation[invocation_id] = []
            self.llm_spans_by_invocation[invocation_id].append(span)

            if invocation_id not in self.seen_user_input:
                user_text = extract_user_text_from_attrs(attributes)
                if user_text:
                    message_key = f"user:{user_text.strip()}"
                    if message_key not in self.seen_message_contents:
                        logger.debug(f"Extracted user input for invocation {invocation_id}")
                        updates.append(
                            {
                                "type": "user_input",
                                "invocationId": invocation_id,
                                "text": user_text,
                                "timestamp": int(span.get("startTimeUnixNano", 0)) / 1e9,
                            }
                        )
                        self.seen_message_contents.add(message_key)
                self.seen_user_input.add(invocation_id)

            agent_text = extract_agent_response_from_attrs(attributes)
            if agent_text and invocation_id not in self.seen_agent_response:
                message_key = f"agent:{agent_text.strip()}"
                if message_key not in self.seen_message_contents:
                    logger.debug(f"Extracted agent response for invocation {invocation_id}")
                    updates.append(
                        {
                            "type": "agent_response",
                            "invocationId": invocation_id,
                            "text": agent_text,
                            "timestamp": int(span.get("endTimeUnixNano", 0)) / 1e9,
                        }
                    )
                    self.seen_message_contents.add(message_key)
                self.seen_agent_response.add(invocation_id)

            in_toks, out_toks, model = extract_token_usage_from_attrs(attributes)
            if in_toks or out_toks:
                if invocation_id not in self.token_totals:
                    self.token_totals[invocation_id] = {
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "model": model,
                    }

                self.token_totals[invocation_id]["inputTokens"] += in_toks
                self.token_totals[invocation_id]["outputTokens"] += out_toks

                logger.debug("Token update for %s: +%d input, +%d output", invocation_id, in_toks, out_toks)

                updates.append(
                    {
                        "type": "token_update",
                        "invocationId": invocation_id,
                        "inputTokens": in_toks,
                        "outputTokens": out_toks,
                        "model": model,
                    }
                )

        elif operation_name.startswith("execute_tool") or is_genai_tool:
            span_id = span.get("spanId", "")
            tool_call = extract_tool_call_from_attrs(attributes, operation_name, span_id=span_id)
            if tool_call:
                call_id = tool_call["id"]
                if invocation_id not in self.seen_tool_calls:
                    self.seen_tool_calls[invocation_id] = set()

                self.tool_names_by_id[call_id] = tool_call["name"]

                if call_id not in self.seen_tool_calls[invocation_id]:
                    updates.append(
                        {
                            "type": "tool_call",
                            "invocationId": invocation_id,
                            "toolCall": tool_call,
                            "timestamp": int(span.get("startTimeUnixNano", 0)) / 1e9,
                        }
                    )
                    self.seen_tool_calls[invocation_id].add(call_id)

                    tool_result = extract_tool_result_from_attrs(attributes)
                    if tool_result:
                        updates.append(
                            {
                                "type": "tool_result",
                                "invocationId": invocation_id,
                                "toolCallId": call_id,
                                "toolName": tool_call["name"],
                                "response": tool_result["response"],
                                "isError": tool_result["isError"],
                                "timestamp": int(span.get("endTimeUnixNano", 0)) / 1e9,
                            }
                        )

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

        invocation_id = log_event.get("span_id")
        if not invocation_id:
            invocation_id = self.current_invocation_id
        if not invocation_id:
            return updates

        # Extract user messages (gen_ai.user.message)
        if event_name == "gen_ai.user.message":
            if isinstance(body, dict) and "content" in body:
                user_text = body["content"]
                message_key = f"user:{user_text.strip() if isinstance(user_text, str) else user_text}"
                if user_text and message_key not in self.seen_message_contents:
                    logger.debug(f"Extracted user input from log for invocation {invocation_id}")
                    updates.append(
                        {
                            "type": "user_input",
                            "invocationId": invocation_id,
                            "text": user_text,
                            "timestamp": _normalize_ts(log_event.get("timestamp", 0)),
                        }
                    )
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
                message_key = f"agent:{agent_text.strip() if isinstance(agent_text, str) else agent_text}"
                if message_key not in self.seen_message_contents:
                    logger.debug(f"Extracted agent response from log for invocation {invocation_id}")
                    updates.append(
                        {
                            "type": "agent_response",
                            "invocationId": invocation_id,
                            "text": agent_text,
                            "timestamp": _normalize_ts(log_event.get("timestamp", 0)),
                        }
                    )
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

                            tc_name = (
                                tc.get("function", {}).get("name", "unknown")
                                if "function" in tc
                                else tc.get("name", "unknown")
                            )
                            self.tool_names_by_id[tool_id] = tc_name

                            if tool_key not in self.seen_message_contents:
                                tool_call = {
                                    "id": tool_id,
                                    "name": tc_name,
                                    "args": {},
                                }

                                if "function" in tc and "arguments" in tc["function"]:
                                    parsed = parse_json_attr(
                                        tc["function"]["arguments"], "tool_call.function.arguments"
                                    )
                                    if isinstance(parsed, dict):
                                        tool_call["args"] = parsed

                                logger.debug(f"Extracted tool call from log for invocation {invocation_id}")
                                updates.append(
                                    {
                                        "type": "tool_call",
                                        "invocationId": invocation_id,
                                        "toolCall": tool_call,
                                        "timestamp": _normalize_ts(log_event.get("timestamp", 0)),
                                    }
                                )
                                self.seen_message_contents.add(tool_key)

                                if invocation_id not in self.seen_tool_calls:
                                    self.seen_tool_calls[invocation_id] = set()
                                self.seen_tool_calls[invocation_id].add(tool_id)

        # Extract tool results from gen_ai.tool.message logs
        elif event_name == "gen_ai.tool.message":
            if isinstance(body, dict):
                tool_id = body.get("id", "unknown")
                tool_name = body.get("name") or self.tool_names_by_id.get(tool_id, "unknown")
                content = body.get("content")
                if content is not None:
                    response = parse_tool_response_content(content)
                    result_key = f"tool_result:{tool_id}"
                    if result_key not in self.seen_message_contents:
                        is_error = bool(response.get("isError", False))
                        updates.append(
                            {
                                "type": "tool_result",
                                "invocationId": invocation_id,
                                "toolCallId": tool_id,
                                "toolName": tool_name,
                                "response": response,
                                "isError": is_error,
                                "timestamp": _normalize_ts(log_event.get("timestamp", 0)),
                            }
                        )
                        self.seen_message_contents.add(result_key)

        return updates

    def _get_invocation_id(self, span: dict, attributes: dict) -> str | None:
        invocation_id = attributes.get(ADK_INVOCATION_ID)
        if invocation_id:
            return invocation_id
        parent_span_id = span.get("parentSpanId")
        if parent_span_id:
            return parent_span_id
        return span.get("spanId")
