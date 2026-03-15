"""OpenTelemetry SpanProcessor and LogRecordProcessor for streaming to agentevals dev server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

try:
    import websockets
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
    from opentelemetry.trace import SpanKind
except ImportError:
    websockets = None
    ReadableSpan = None
    SpanProcessor = None
    SpanKind = None

logger = logging.getLogger(__name__)


class AgentEvalsStreamingProcessor:
    """OTel span processor that streams spans to agentevals dev server via WebSocket."""

    def __init__(self, ws_url: str, session_id: str, trace_id: str):
        if websockets is None or SpanProcessor is None:
            raise ImportError(
                "websockets and opentelemetry-sdk required for streaming. "
                "Install with: pip install websockets opentelemetry-sdk"
            )

        self.ws_url = ws_url
        self.session_id = session_id
        self.trace_id = trace_id
        self.websocket: Optional[Any] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._span_buffer: list[dict] = []
        self._failed_spans: list[dict] = []
        self._pending_sends: set[asyncio.Task] = set()

    async def connect(self, eval_set_id: str | None = None, metadata: dict | None = None):
        try:
            self.websocket = await websockets.connect(self.ws_url)
            self.loop = asyncio.get_running_loop()

            await self.websocket.send(
                json.dumps(
                    {
                        "type": "session_start",
                        "session_id": self.session_id,
                        "trace_id": self.trace_id,
                        "eval_set_id": eval_set_id,
                        "metadata": metadata or {},
                    }
                )
            )

            self._connected = True
            logger.info("Connected to agentevals dev server: %s", self.session_id)

        except Exception as exc:
            logger.error("Failed to connect to agentevals server: %s", exc)
            self._connected = False

    def on_start(self, span: ReadableSpan, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if not self._connected or not self.websocket or not self.loop:
            logger.debug(f"Skipping span {span.name}: not connected")
            return

        try:
            otlp_span = self._span_to_otlp(span)
            self._span_buffer.append(otlp_span)

            future = asyncio.run_coroutine_threadsafe(self._send_span(otlp_span), self.loop)
            self._pending_sends.add(future)

            def handle_send_complete(fut):
                self._pending_sends.discard(fut)
                try:
                    fut.result()
                    logger.debug(f"Sent span: {span.name}")
                except Exception as exc:
                    logger.error(f"Failed to send span {span.name}: {exc}")
                    self._failed_spans.append(otlp_span)

            future.add_done_callback(handle_send_complete)

        except Exception as exc:
            logger.warning("Failed to convert span: %s", exc)

    def shutdown(self) -> None:
        pass

    async def shutdown_async(self) -> None:
        if self.websocket and self._connected:
            try:
                await self._send_session_end()
            except Exception as exc:
                logger.warning("Failed to shutdown cleanly: %s", exc)

    async def _send_span(self, otlp_span: dict) -> None:
        if not self.websocket:
            raise ConnectionError("WebSocket not connected")

        message = {
            "type": "span",
            "session_id": self.session_id,
            "span": otlp_span,
        }
        await self.websocket.send(json.dumps(message))
        logger.debug(f"Sent span: {otlp_span.get('name')}")

    async def _send_session_end(self) -> None:
        try:
            if not self.websocket:
                return

            if self._pending_sends:
                logger.info("Waiting for %d pending span sends to complete...", len(self._pending_sends))
                for future in list(self._pending_sends):
                    try:
                        await asyncio.wrap_future(future)
                    except Exception as exc:
                        logger.warning("Pending send failed during shutdown: %s", exc)
                logger.info("All pending sends completed")

            if self._failed_spans:
                logger.info("Retrying %d failed spans at shutdown", len(self._failed_spans))
                for otlp_span in self._failed_spans:
                    try:
                        await self._send_span(otlp_span)
                    except Exception as exc:
                        logger.error("Failed to send span even at shutdown: %s", exc)

            self._failed_spans.clear()
            self._span_buffer.clear()
            self._pending_sends.clear()

            await self.websocket.send(
                json.dumps(
                    {"type": "session_end", "session_id": self.session_id}
                )
            )

            await self.websocket.close()
            self._connected = False
        except Exception as exc:
            logger.error("Failed to send session_end: %s", exc)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def _span_to_otlp(self, span: ReadableSpan) -> dict:
        scope_name = span.instrumentation_scope.name if span.instrumentation_scope else ""
        scope_version = span.instrumentation_scope.version if span.instrumentation_scope else ""

        attributes = []
        if scope_name:
            attributes.append(
                {"key": "otel.scope.name", "value": {"stringValue": scope_name}}
            )
        if scope_version:
            attributes.append(
                {"key": "otel.scope.version", "value": {"stringValue": scope_version}}
            )

        if span.attributes:
            for key, value in span.attributes.items():
                attributes.append(self._to_otlp_attribute(key, value))

        self._promote_genai_event_attributes(span, attributes)

        parent_span_id = None
        if span.parent and hasattr(span.parent, "span_id"):
            parent_span_id = format(span.parent.span_id, "016x")

        return {
            "traceId": format(span.context.trace_id, "032x"),
            "spanId": format(span.context.span_id, "016x"),
            "parentSpanId": parent_span_id,
            "name": span.name,
            "kind": span.kind.value if span.kind else 1,
            "startTimeUnixNano": str(span.start_time),
            "endTimeUnixNano": str(span.end_time),
            "attributes": attributes,
            "status": {"code": span.status.status_code.value} if span.status else {},
        }

    def _promote_genai_event_attributes(self, span: ReadableSpan, attributes: list[dict]) -> None:
        """Promote OTel GenAI message attributes from span events to span attributes.

        OTel GenAI semantic convention frameworks may store message content
        (gen_ai.input.messages, gen_ai.output.messages) in span events rather
        than span attributes. This promotes those event attributes so downstream
        processors can access them uniformly from span attributes alone.
        """
        if not hasattr(span, "events") or not span.events:
            return

        from ..trace_attrs import OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_OUTPUT_MESSAGES
        _genai_event_keys = {OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_OUTPUT_MESSAGES}
        existing_keys = {a["key"] for a in attributes}

        for event in span.events:
            if not event.attributes:
                continue
            for key, value in event.attributes.items():
                if key in _genai_event_keys and key not in existing_keys:
                    attributes.append(self._to_otlp_attribute(key, value))
                    existing_keys.add(key)

    def _to_otlp_attribute(self, key: str, value: Any) -> dict:
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        elif isinstance(value, int):
            return {"key": key, "value": {"intValue": value}}
        elif isinstance(value, float):
            return {"key": key, "value": {"doubleValue": value}}
        else:
            return {"key": key, "value": {"stringValue": str(value)}}


class AgentEvalsLogStreamingProcessor:
    """OTel log processor that streams GenAI logs to agentevals dev server via WebSocket.

    This processor shares the same WebSocket connection and session as AgentEvalsStreamingProcessor.
    It extracts input/output messages from GenAI semantic convention logs and streams them.
    """

    def __init__(self, span_processor: AgentEvalsStreamingProcessor):
        """Initialize with a reference to the span processor for shared connection."""
        self.span_processor = span_processor

    def on_emit(self, log_data):
        """Called when a log record is emitted."""
        log_record = log_data.log_record

        # Only process GenAI message logs
        if not log_record.event_name or not log_record.event_name.startswith("gen_ai."):
            return

        logger.info(f"Log emitted: event={log_record.event_name}")

        if not self.span_processor._connected or not self.span_processor.websocket or not self.span_processor.loop:
            return

        try:
            log_json = {
                "event_name": log_record.event_name,
                "timestamp": str(log_record.timestamp) if log_record.timestamp else None,
                "body": log_record.body,
                "attributes": {},
            }

            if log_record.attributes:
                for key, value in log_record.attributes.items():
                    log_json["attributes"][key] = value

            future = asyncio.run_coroutine_threadsafe(
                self._send_log(log_json),
                self.span_processor.loop
            )

            def handle_send_complete(fut):
                try:
                    fut.result()
                except Exception as exc:
                    logger.error(f"Failed to send log: {exc}")

            future.add_done_callback(handle_send_complete)

        except Exception as exc:
            logger.warning("Failed to process log: %s", exc)

    async def _send_log(self, log_json: dict) -> None:
        if not self.span_processor.websocket:
            raise ConnectionError("WebSocket not connected")

        message = {
            "type": "log",
            "session_id": self.span_processor.session_id,
            "log": log_json,
        }
        await self.span_processor.websocket.send(json.dumps(message))

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000):
        return True
