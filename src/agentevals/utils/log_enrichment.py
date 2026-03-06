"""Utilities for enriching OTel spans with GenAI log message content."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def enrich_spans_with_logs(
    spans: list[dict],
    logs: list[dict],
    session_id: str | None = None
) -> list[dict]:
    """Enrich spans with message content from GenAI logs.

    This reconstructs gen_ai.input.messages and gen_ai.output.messages attributes
    from log events so the converter can extract message content.

    Args:
        spans: List of OTLP span dictionaries
        logs: List of GenAI log event dictionaries
        session_id: Optional session ID to add as agent.name attribute

    Returns:
        List of enriched span dictionaries with message attributes added
    """
    if not logs:
        return spans

    logger.debug("Enriching %d spans with %d logs", len(spans), len(logs))

    input_messages = []
    output_messages = []
    seen_user_messages = set()
    seen_assistant_messages = set()

    assistant_log_count = sum(
        1 for log in logs
        if log.get("event_name") in ("gen_ai.assistant.message", "gen_ai.choice")
    )
    logger.debug("Found %d assistant log events", assistant_log_count)

    for log in logs:
        event_name = log.get("event_name", "")
        body = log.get("body", {})

        if not isinstance(body, dict):
            continue

        if event_name == "gen_ai.user.message":
            user_content = body.get("content", "")
            if user_content and user_content not in seen_user_messages:
                user_msg = {
                    "role": "user",
                    "content": user_content
                }
                input_messages.append(user_msg)
                seen_user_messages.add(user_content)

        elif event_name in ("gen_ai.assistant.message", "gen_ai.choice"):
            if event_name == "gen_ai.choice":
                # gen_ai.choice from openai-v2 nests content under body["message"].
                # Only extract text content here — tool_calls come from gen_ai.assistant.message
                # events emitted by subsequent LLM calls' input context.
                nested = body.get("message", {}) if isinstance(body.get("message"), dict) else {}
                assistant_content = body.get("content") or nested.get("content") or ""
                tool_calls = []
            else:
                assistant_content = body.get("content") or ""
                tool_calls = body.get("tool_calls", [])

            message_key = f"{assistant_content}:{json.dumps(tool_calls) if tool_calls else ''}"

            if assistant_content or tool_calls:
                if message_key not in seen_assistant_messages:
                    assistant_msg = {
                        "role": "assistant",
                        "content": assistant_content
                    }
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    output_messages.append(assistant_msg)
                    seen_assistant_messages.add(message_key)

    if not (input_messages or output_messages):
        logger.warning("No messages extracted from logs")
        return spans

    logger.debug(
        "Deduplicated: %d user, %d assistant messages",
        len(input_messages), len(output_messages)
    )
    for i, msg in enumerate(output_messages):
        logger.debug(
            "  Output message %d: content_len=%d, has_tool_calls=%s",
            i, len(msg.get("content", "")), bool(msg.get("tool_calls"))
        )

    enriched_spans = []
    for span in spans:
        span_copy = span.copy()

        if "attributes" not in span_copy:
            span_copy["attributes"] = []

        attrs = span_copy["attributes"]

        if input_messages:
            attrs.append({
                "key": "gen_ai.input.messages",
                "value": {"stringValue": json.dumps(input_messages)}
            })

        if output_messages:
            attrs.append({
                "key": "gen_ai.output.messages",
                "value": {"stringValue": json.dumps(output_messages)}
            })

        if session_id:
            attrs.append({
                "key": "gen_ai.agent.name",
                "value": {"stringValue": session_id}
            })

        enriched_spans.append(span_copy)

    return enriched_spans
