"""OTLP payload decoding and processing for traces/logs."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest as LogsServiceRequestPB,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest as TraceServiceRequestPB,
)

from ..extraction import flatten_otlp_attributes
from ..trace_attrs import (
    OTEL_GENAI_CONVERSATION_ID,
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_SCOPE,
    OTEL_SCOPE_VERSION,
)
from .models import WSSpanReceivedEvent

if TYPE_CHECKING:
    from ..streaming.ws_server import StreamingTraceManager

logger = logging.getLogger(__name__)

AGENTEVALS_EVAL_SET_ID = "agentevals.eval_set_id"
AGENTEVALS_SESSION_NAME = "agentevals.session_name"


async def process_traces(body: dict, manager: StreamingTraceManager) -> None:
    """Parse ExportTraceServiceRequest and feed spans to the pipeline."""
    for resource_span in body.get("resourceSpans", []):
        resource_attrs = resource_span.get("resource", {}).get("attributes", [])
        metadata = _extract_agentevals_metadata(resource_attrs)

        if not metadata.get("conversation_id"):
            metadata["conversation_id"] = _prescan_conversation_id(resource_span)

        for scope_span in resource_span.get("scopeSpans", []):
            scope = scope_span.get("scope", {})
            scope_name = scope.get("name", "")
            scope_version = scope.get("version", "")

            for span_data in scope_span.get("spans", []):
                span = _normalize_span(span_data, scope_name, scope_version)
                trace_id = span.get("traceId", "")

                if not trace_id:
                    continue

                if not metadata.get("conversation_id"):
                    conversation_id = _extract_conversation_id(span.get("attributes", []))
                    if conversation_id:
                        metadata["conversation_id"] = conversation_id

                session = await manager.get_or_create_otlp_session(trace_id, metadata)

                if not session.can_accept_span():
                    logger.warning("Session %s at span limit", session.session_id)
                    continue

                session.spans.append(span)

                extractor = manager.incremental_extractors.get(session.session_id)
                if extractor:
                    updates = extractor.process_span(span)
                    for update in updates:
                        update["sessionId"] = session.session_id
                        await manager.broadcast_to_ui(update)

                await manager.broadcast_to_ui(
                    WSSpanReceivedEvent(
                        session_id=session.session_id,
                        span=span,
                    ).model_dump(by_alias=True)
                )

                manager.reset_idle_timer(session.session_id)

                if not span.get("parentSpanId"):
                    session.has_root_span = True
                    manager.schedule_session_completion(session.session_id)


async def process_logs(body: dict, manager: StreamingTraceManager) -> None:
    """Parse ExportLogsServiceRequest and feed logs to sessions.

    Logs and spans arrive via separate OTLP exporters (BatchLogRecordProcessor
    and BatchSpanProcessor) and may arrive in any order. When a log's traceId
    isn't yet registered in a session's trace_ids set, we fall back to matching
    by session_name from resource attributes.

    Logs may arrive after span-triggered session completion (the
    BatchLogRecordProcessor and BatchSpanProcessor flush independently).
    Late-arriving logs are accepted and trigger re-extraction of invocations.
    """
    sessions_needing_reextraction: set[str] = set()

    for resource_log in body.get("resourceLogs", []):
        resource_attrs = resource_log.get("resource", {}).get("attributes", [])
        metadata = _extract_agentevals_metadata(resource_attrs)
        session_name = metadata.get("session_name")

        for scope_log in resource_log.get("scopeLogs", []):
            for log_record in scope_log.get("logRecords", []):
                log_event = _convert_otlp_log_record(log_record)
                if not log_event:
                    continue

                trace_id = log_record.get("traceId", "")
                if not trace_id:
                    continue

                session = manager.find_session_by_trace_id(trace_id)

                if not session and session_name:
                    active_id = manager._active_session_for_name.get(session_name)
                    candidate = manager.sessions.get(active_id) if active_id else None
                    if candidate and not candidate.is_complete:
                        candidate.trace_ids.add(trace_id)
                        session = candidate

                if not session:
                    manager.buffer_orphan_log(trace_id, session_name, log_event)
                    logger.debug(
                        "Buffered orphan log trace_id=%s session_name=%s",
                        trace_id[:12],
                        session_name,
                    )
                    continue

                if not session.can_accept_log():
                    continue

                session.logs.append(log_event)

                if session.is_complete:
                    sessions_needing_reextraction.add(session.session_id)
                else:
                    manager.reset_idle_timer(session.session_id)

                    extractor = manager.incremental_extractors.get(session.session_id)
                    if extractor:
                        updates = extractor.process_log(log_event)
                        for update in updates:
                            update["sessionId"] = session.session_id
                            await manager.broadcast_to_ui(update)

    for session_id in sessions_needing_reextraction:
        manager.schedule_log_reextraction(session_id)


def decode_protobuf_traces(raw: bytes) -> dict:
    """Decode ExportTraceServiceRequest protobuf to OTLP JSON dict."""
    msg = TraceServiceRequestPB()
    msg.ParseFromString(raw)
    data = MessageToDict(msg, preserving_proto_field_name=False)
    fix_protobuf_id_fields(data)
    return data


def decode_protobuf_logs(raw: bytes) -> dict:
    """Decode ExportLogsServiceRequest protobuf to OTLP JSON dict."""
    msg = LogsServiceRequestPB()
    msg.ParseFromString(raw)
    data = MessageToDict(msg, preserving_proto_field_name=False)
    fix_protobuf_id_fields(data)
    return data


def fix_protobuf_id_fields(data) -> None:
    """Convert base64-encoded bytes fields to hex strings in-place.

    MessageToDict base64-encodes protobuf bytes fields, but OTLP JSON
    uses hex-encoded strings for traceId, spanId, and parentSpanId.
    """
    if isinstance(data, dict):
        for key in ("traceId", "spanId", "parentSpanId"):
            if key in data and isinstance(data[key], str):
                try:
                    raw = base64.b64decode(data[key])
                    data[key] = raw.hex()
                except Exception:
                    pass
        for value in data.values():
            if isinstance(value, (dict, list)):
                fix_protobuf_id_fields(value)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                fix_protobuf_id_fields(item)


_GENAI_EVENT_KEYS = {OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_OUTPUT_MESSAGES}


def _normalize_span(span_data: dict, scope_name: str, scope_version: str) -> dict:
    """Normalize an OTLP span for the downstream pipeline.

    Performs two transformations:
    1. Injects otel.scope.name/version from the scopeSpans level into span
       attributes (the pipeline expects them there).
    2. Promotes gen_ai.input.messages and gen_ai.output.messages from span
       events to span attributes. Some SDKs (e.g. Strands with
       OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental) store
       message content in span events, but the converter reads attributes.
    """
    span = dict(span_data)
    attrs = list(span.get("attributes", []))

    existing_keys = {a.get("key") for a in attrs}

    if scope_name and OTEL_SCOPE not in existing_keys:
        attrs.append({"key": OTEL_SCOPE, "value": {"stringValue": scope_name}})
        existing_keys.add(OTEL_SCOPE)
    if scope_version and OTEL_SCOPE_VERSION not in existing_keys:
        attrs.append({"key": OTEL_SCOPE_VERSION, "value": {"stringValue": scope_version}})
        existing_keys.add(OTEL_SCOPE_VERSION)

    for event in span.get("events", []):
        for attr in event.get("attributes", []):
            key = attr.get("key", "")
            if key in _GENAI_EVENT_KEYS and key not in existing_keys:
                attrs.append(attr)
                existing_keys.add(key)

    span["attributes"] = attrs
    return span


def _extract_agentevals_metadata(resource_attrs: list[dict]) -> dict:
    """Extract agentevals-specific metadata from OTLP resource attributes."""
    flat = flatten_otlp_attributes(resource_attrs)
    return {
        "eval_set_id": flat.get(AGENTEVALS_EVAL_SET_ID),
        "session_name": flat.get(AGENTEVALS_SESSION_NAME),
        "service_name": flat.get("service.name"),
        "resource_attrs": flat,
    }


def _prescan_conversation_id(resource_span: dict) -> str | None:
    """Pre-scan all spans in a resourceSpan batch for gen_ai.conversation.id.

    Within a single OTLP batch, some scopes (e.g. A2A server instrumentation)
    may lack conversation_id while others (agent instrumentation) have it.
    Scanning upfront ensures ALL spans in the batch route to the same session.
    """
    for scope_span in resource_span.get("scopeSpans", []):
        for span_data in scope_span.get("spans", []):
            conv_id = _extract_conversation_id(span_data.get("attributes", []))
            if conv_id:
                return conv_id
    return None


def _extract_conversation_id(attrs_list: list[dict]) -> str | None:
    """Extract gen_ai.conversation.id from OTLP span attributes."""
    for attr in attrs_list:
        if attr.get("key") == OTEL_GENAI_CONVERSATION_ID:
            return attr.get("value", {}).get("stringValue")
    return None


def _convert_otlp_log_record(log_record: dict) -> dict | None:
    """Convert OTLP log record to internal log event format.

    Internal format (used by IncrementalInvocationExtractor.process_log()):
        {"event_name": "gen_ai.user.message", "timestamp": ..., "body": {...}, "attributes": {...}}

    Handles two event-name conventions:
    - Newer OTel SDKs: top-level ``eventName`` field (LogRecord.event_name proto)
    - Older convention: ``event.name`` stored as a regular attribute
    """
    attrs = flatten_otlp_attributes(log_record.get("attributes", []))
    event_name = log_record.get("eventName") or attrs.get("event.name", "")

    if not event_name or not event_name.startswith("gen_ai."):
        return None

    body_raw = log_record.get("body", {})
    body = _parse_otlp_body(body_raw)

    timestamp = log_record.get("timeUnixNano") or log_record.get("observedTimeUnixNano")

    result = {
        "event_name": event_name,
        "timestamp": timestamp,
        "body": body,
        "attributes": attrs,
    }

    span_id = log_record.get("spanId", "")
    if span_id:
        result["span_id"] = span_id

    return result


def _parse_otlp_any_value(value_obj: dict):
    """Recursively parse an OTLP AnyValue to native Python types.

    Handles the full AnyValue union: stringValue, intValue, doubleValue,
    boolValue, kvlistValue (→ dict), arrayValue (→ list), bytesValue.
    """
    if "stringValue" in value_obj:
        return value_obj["stringValue"]
    if "intValue" in value_obj:
        return int(value_obj["intValue"])
    if "doubleValue" in value_obj:
        return float(value_obj["doubleValue"])
    if "boolValue" in value_obj:
        return value_obj["boolValue"]
    if "kvlistValue" in value_obj:
        kv = value_obj["kvlistValue"]
        return {item.get("key", ""): _parse_otlp_any_value(item.get("value", {})) for item in kv.get("values", [])}
    if "arrayValue" in value_obj:
        arr = value_obj["arrayValue"]
        return [_parse_otlp_any_value(v) for v in arr.get("values", [])]
    if "bytesValue" in value_obj:
        return value_obj["bytesValue"]
    return value_obj


def _parse_otlp_body(body_raw: dict) -> dict | str:
    """Parse OTLP log record body value.

    Top-level stringValue bodies are JSON-decoded (Strands-style logs store
    message content as JSON strings). All other AnyValue types are parsed
    recursively via ``_parse_otlp_any_value`` (handles the nested kvlistValue /
    arrayValue structures used by the OpenAI instrumentor).
    """
    if "stringValue" in body_raw:
        import json

        raw = body_raw["stringValue"]
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return _parse_otlp_any_value(body_raw)
