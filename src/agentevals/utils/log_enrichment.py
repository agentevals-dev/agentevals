"""Utilities for enriching OTel spans with GenAI log message content."""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from ..trace_attrs import (
    OTEL_GENAI_AGENT_NAME,
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OUTPUT_MESSAGES,
)

logger = logging.getLogger(__name__)


def enrich_spans_with_logs(
    spans: list[dict],
    logs: list[dict],
    session_id: str | None = None
) -> list[dict]:
    """Enrich spans with message content from GenAI logs.

    This reconstructs gen_ai.input.messages and gen_ai.output.messages attributes
    from log events so the converter can extract message content.

    When logs carry a ``span_id`` (OTLP path), each span is enriched only with
    its own logs. When logs lack ``span_id`` (WebSocket SDK path), all messages
    are injected into every span (legacy behavior).

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

    has_span_ids = any(log.get("span_id") for log in logs)

    if has_span_ids:
        return _enrich_per_span(spans, logs, session_id)
    return _enrich_broadcast(spans, logs, session_id)


def _extract_messages_from_logs(
    logs: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Extract deduplicated input/output messages from a list of log events."""
    input_messages = []
    output_messages = []
    seen_user = set()
    seen_assistant = set()

    for log in logs:
        event_name = log.get("event_name", "")
        body = log.get("body", {})

        if not isinstance(body, dict):
            continue

        if event_name == "gen_ai.user.message":
            user_content = body.get("content", "")
            if user_content and user_content not in seen_user:
                input_messages.append({"role": "user", "content": user_content})
                seen_user.add(user_content)

        elif event_name in ("gen_ai.assistant.message", "gen_ai.choice"):
            if event_name == "gen_ai.choice":
                nested = body.get("message", {}) if isinstance(body.get("message"), dict) else {}
                assistant_content = body.get("content") or nested.get("content") or ""
                tool_calls = nested.get("tool_calls", [])
            else:
                assistant_content = body.get("content") or ""
                tool_calls = body.get("tool_calls", [])

            message_key = f"{assistant_content}:{json.dumps(tool_calls) if tool_calls else ''}"

            if (assistant_content or tool_calls) and message_key not in seen_assistant:
                assistant_msg = {"role": "assistant", "content": assistant_content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                output_messages.append(assistant_msg)
                seen_assistant.add(message_key)

    return input_messages, output_messages


def _inject_messages(
    span: dict,
    input_messages: list[dict],
    output_messages: list[dict],
    session_id: str | None,
) -> dict:
    """Create a copy of *span* with message attributes injected."""
    span_copy = span.copy()

    if "attributes" not in span_copy:
        span_copy["attributes"] = []

    attrs = span_copy["attributes"]

    if input_messages:
        attrs.append({
            "key": OTEL_GENAI_INPUT_MESSAGES,
            "value": {"stringValue": json.dumps(input_messages)},
        })
    if output_messages:
        attrs.append({
            "key": OTEL_GENAI_OUTPUT_MESSAGES,
            "value": {"stringValue": json.dumps(output_messages)},
        })
    if session_id:
        attrs.append({
            "key": OTEL_GENAI_AGENT_NAME,
            "value": {"stringValue": session_id},
        })

    return span_copy


def _enrich_per_span(
    spans: list[dict],
    logs: list[dict],
    session_id: str | None,
) -> list[dict]:
    """Enrich each span with only the logs emitted within that span's context."""
    logs_by_span: dict[str, list[dict]] = defaultdict(list)
    for log in logs:
        sid = log.get("span_id", "")
        if sid:
            logs_by_span[sid].append(log)

    enriched = []
    for span in spans:
        span_id = span.get("spanId", "")
        span_logs = logs_by_span.get(span_id, [])

        if span_logs:
            input_msgs, output_msgs = _extract_messages_from_logs(span_logs)
            enriched.append(_inject_messages(span, input_msgs, output_msgs, session_id))
        else:
            span_copy = span.copy()
            if session_id:
                if "attributes" not in span_copy:
                    span_copy["attributes"] = []
                span_copy["attributes"].append({
                    "key": OTEL_GENAI_AGENT_NAME,
                    "value": {"stringValue": session_id},
                })
            enriched.append(span_copy)

    matched = sum(1 for sid in logs_by_span if any(s.get("spanId") == sid for s in spans))
    logger.debug(
        "Per-span enrichment: %d log groups, %d matched to spans",
        len(logs_by_span), matched,
    )
    return enriched


def _enrich_broadcast(
    spans: list[dict],
    logs: list[dict],
    session_id: str | None,
) -> list[dict]:
    """Legacy enrichment: inject all messages into every span."""
    input_messages, output_messages = _extract_messages_from_logs(logs)

    if not (input_messages or output_messages):
        logger.warning("No messages extracted from logs")
        return spans

    logger.debug(
        "Broadcast enrichment: %d user, %d assistant messages",
        len(input_messages), len(output_messages),
    )

    return [_inject_messages(span, input_messages, output_messages, session_id) for span in spans]
