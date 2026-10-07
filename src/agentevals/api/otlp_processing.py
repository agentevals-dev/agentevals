"""OTLP export decoding and ingestion, shared by the HTTP and gRPC receivers.

Bodies decode straight into envelopes (``otel.decode``), strictly: invalid ids are rejected item
by item and reported through ``partialSuccess`` together with what the live store refused.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest as LogsServiceRequestPB,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceResponse as LogsServiceResponsePB,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest as TraceServiceRequestPB,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse as TraceServiceResponsePB,
)

from ..otel.decode import DecodeResult, decode_json_document, decode_logs_proto, decode_traces_proto
from ..otel.store import IngestResult

if TYPE_CHECKING:
    from ..streaming.manager import LiveManager

logger = logging.getLogger(__name__)


@dataclass
class ExportResult:
    """Outcome of ingesting one Export<signal>ServiceRequest.

    ``rejected`` is what OTLP requires the receiver to report back in the response's
    ``partial_success`` field. ``overloaded`` is set when every item was refused for transient
    capacity (memory or session slots),
    which the receivers answer with a retryable 503 / ``UNAVAILABLE`` instead.
    """

    accepted: int = 0
    rejected: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    overloaded: bool = False

    def accept(self, n: int = 1) -> None:
        self.accepted += n

    def reject(self, reason: str, n: int = 1) -> None:
        if n:
            self.rejected += n
            self.rejected_reasons[reason] = self.rejected_reasons.get(reason, 0) + n

    def add_reasons(self, reasons: Mapping[str, int]) -> None:
        for reason, n in reasons.items():
            self.reject(reason, n)

    @property
    def error_message(self) -> str:
        """Human-readable English summary of what was rejected and why."""
        return "; ".join(f"{count} {reason}" for reason, count in self.rejected_reasons.items())


def _log_rejections(signal: str, result: ExportResult) -> None:
    """Warn once per export about dropped records, never per record."""
    if not result.rejected:
        return
    logger.warning(
        "OTLP %s export rejected %d of %d records: %s",
        signal,
        result.rejected,
        result.rejected + result.accepted,
        result.error_message,
    )


def _merge(decoded_rejected: int, decoded_reasons: Counter, outcome: IngestResult) -> ExportResult:
    result = ExportResult()
    result.add_reasons(decoded_reasons)
    result.accept(outcome.accepted)
    result.add_reasons(outcome.reasons)
    result.overloaded = outcome.overloaded and decoded_rejected == 0
    if outcome.dropped:
        logger.debug("OTLP export accepted but not stored: %s", dict(outcome.dropped))
    return result


async def ingest_traces(decoded: DecodeResult, manager: LiveManager) -> ExportResult:
    reasons = Counter({r: n for r, n in decoded.reasons.items() if r.startswith("span")})
    if decoded.warnings:
        logger.debug("OTLP trace export decode warnings: %s", dict(decoded.warnings))
    result = _merge(decoded.rejected_spans, reasons, await manager.ingest_spans(decoded))
    _log_rejections("trace", result)
    return result


async def ingest_logs(decoded: DecodeResult, manager: LiveManager) -> ExportResult:
    reasons = Counter({r: n for r, n in decoded.reasons.items() if r.startswith("log")})
    if decoded.warnings:
        logger.debug("OTLP log export decode warnings: %s", dict(decoded.warnings))
    result = _merge(decoded.rejected_logs, reasons, await manager.ingest_logs(decoded))
    _log_rejections("log", result)
    return result


def decode_traces_json(body: dict) -> DecodeResult:
    return decode_json_document({"resourceSpans": body.get("resourceSpans", [])}, strict=True)


def decode_logs_json(body: dict) -> DecodeResult:
    return decode_json_document({"resourceLogs": body.get("resourceLogs", [])}, strict=True)


def decode_traces_protobuf(raw: bytes) -> DecodeResult:
    """Raises ``google.protobuf.message.DecodeError`` on a malformed body."""
    request = TraceServiceRequestPB()
    request.ParseFromString(raw)
    return decode_traces_proto(request)


def decode_logs_protobuf(raw: bytes) -> DecodeResult:
    """Raises ``google.protobuf.message.DecodeError`` on a malformed body."""
    request = LogsServiceRequestPB()
    request.ParseFromString(raw)
    return decode_logs_proto(request)


def build_traces_response(result: ExportResult) -> TraceServiceResponsePB:
    """OTLP requires ``partial_success`` to be left unset on full success."""
    response = TraceServiceResponsePB()
    if result.rejected:
        response.partial_success.rejected_spans = result.rejected
        response.partial_success.error_message = result.error_message
    return response


def build_logs_response(result: ExportResult) -> LogsServiceResponsePB:
    response = LogsServiceResponsePB()
    if result.rejected:
        response.partial_success.rejected_log_records = result.rejected
        response.partial_success.error_message = result.error_message
    return response
