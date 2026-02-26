"""OpenTelemetry SpanProcessor for streaming spans to agentevals dev server."""

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
        """Connect to WebSocket server and send session_start."""
        try:
            self.websocket = await websockets.connect(self.ws_url)
            self.loop = asyncio.get_event_loop()

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
        """Called when span starts (not used for streaming)."""
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """Called when span ends - send immediately for real-time streaming."""
        if not self._connected or not self.websocket or not self.loop:
            return

        try:
            otlp_span = self._span_to_otlp(span)
            self._span_buffer.append(otlp_span)

            task = self.loop.create_task(self._send_span(otlp_span))
            self._pending_sends.add(task)

            def handle_send_complete(future: asyncio.Task) -> None:
                self._pending_sends.discard(future)
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Failed to send span in real-time: %s", exc)
                    self._failed_spans.append(otlp_span)

            task.add_done_callback(handle_send_complete)

        except Exception as exc:
            logger.warning("Failed to convert span: %s", exc)

    def shutdown(self) -> None:
        """Shutdown processor (sync version for OTel SDK compatibility)."""
        pass

    async def shutdown_async(self) -> None:
        """Shutdown processor and close connection (async version)."""
        if self.websocket and self._connected:
            try:
                await self._send_session_end()
            except Exception as exc:
                logger.warning("Failed to shutdown cleanly: %s", exc)

    async def _send_span(self, otlp_span: dict) -> None:
        """Send a single span to the server."""
        if not self.websocket:
            raise ConnectionError("WebSocket not connected")

        await self.websocket.send(
            json.dumps(
                {
                    "type": "span",
                    "session_id": self.session_id,
                    "span": otlp_span,
                }
            )
        )

    async def _send_session_end(self) -> None:
        """Wait for pending sends, retry failed spans, send session_end, and close connection."""
        try:
            if not self.websocket:
                return

            if self._pending_sends:
                logger.info("Waiting for %d pending span sends to complete...", len(self._pending_sends))
                await asyncio.gather(*self._pending_sends, return_exceptions=True)
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
        """Force flush (no-op for streaming)."""
        return True

    def _span_to_otlp(self, span: ReadableSpan) -> dict:
        """Convert OTel ReadableSpan to OTLP/JSON format."""
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

    def _to_otlp_attribute(self, key: str, value: Any) -> dict:
        """Convert Python value to OTLP attribute format."""
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        elif isinstance(value, int):
            return {"key": key, "value": {"intValue": value}}
        elif isinstance(value, float):
            return {"key": key, "value": {"doubleValue": value}}
        else:
            return {"key": key, "value": {"stringValue": str(value)}}
