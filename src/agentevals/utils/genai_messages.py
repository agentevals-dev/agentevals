"""Utilities for parsing OTel GenAI semantic convention message formats.

Supports two message formats:
- Content-based (e.g. opentelemetry-instrumentation-openai-v2):
    {"role": "user", "content": "Hello"}
    {"role": "assistant", "content": "...", "tool_calls": [{"type": "function", ...}]}

- Parts-based (OTel GenAI semconv v1.36.0+):
    {"role": "user", "parts": [{"type": "text", "content": "Hello"}]}
    {"role": "assistant", "parts": [{"type": "tool_call", "name": "...", "arguments": {...}}]}
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

USER_ROLES = ("user", "human")
ASSISTANT_ROLES = ("assistant", "model", "ai")


def parse_json_attr(raw: str | dict | list | Any, tag_name: str = "") -> dict | list | Any:
    """Parse a JSON string from an OTel span attribute value.

    If *raw* is already a dict or list it is returned as-is.
    Returns ``{}`` on parse failure.
    """
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON in %s: %s", tag_name, raw[:200])
            return {}
    return {}


def extract_text_from_message(msg: dict) -> str:
    """Extract text content from a GenAI message in any supported format."""
    content = msg.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = [item["text"] for item in content if isinstance(item, dict) and "text" in item]
        if parts:
            return " ".join(parts)

    parts = msg.get("parts")
    if isinstance(parts, list):
        text_parts = []
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("content") or part.get("text", "")
            if text:
                text_parts.append(text)
        if text_parts:
            return " ".join(text_parts)

    return ""


def extract_tool_calls_from_message(msg: dict) -> list[dict[str, Any]]:
    """Extract tool calls from a GenAI message in any supported format.

    Returns a normalized list of:
        {"name": str, "id": str | None, "arguments": dict}
    """
    result = []

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") == "function" and "function" in tc:
                func = tc["function"]
                args = _parse_args(func.get("arguments", {}))
                result.append({
                    "name": func.get("name", ""),
                    "id": tc.get("id"),
                    "arguments": args,
                })

    if not result:
        parts = msg.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict) or part.get("type") != "tool_call":
                    continue
                args = _parse_args(part.get("arguments", {}))
                result.append({
                    "name": part.get("name", ""),
                    "id": part.get("id"),
                    "arguments": args,
                })

    return result


def extract_tool_call_args_from_messages(
    messages_raw: str | list | Any, tool_name: str,
) -> tuple[dict, str | None]:
    """Fallback: extract tool call args and ID from a messages attribute by matching *tool_name*.

    Used when a tool span lacks ``gen_ai.tool.call.arguments`` directly
    (e.g. Strands embeds the triggering tool_call in ``gen_ai.input.messages``).

    Returns ``(args_dict, tool_call_id_or_None)``.
    """
    messages = parse_json_attr(messages_raw, "gen_ai.input.messages")
    if not isinstance(messages, list):
        return {}, None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for tc in extract_tool_calls_from_message(msg):
            if tc["name"] == tool_name and tc["arguments"]:
                return tc["arguments"], tc.get("id")
    return {}, None


def _parse_args(args: Any) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            logger.warning("Failed to parse tool call arguments JSON: %s", args[:200])
    return {}
