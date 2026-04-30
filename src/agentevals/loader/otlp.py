"""OTLP/JSON trace loader for native OpenTelemetry format."""

from __future__ import annotations

import json
import logging

from ..trace_attrs import (
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_SCOPE,
    OTEL_SCOPE_VERSION,
)
from .base import Span, Trace, TraceLoader

logger = logging.getLogger(__name__)


class OtlpJsonLoader(TraceLoader):
    """Loads traces from OTLP/JSON format (native OpenTelemetry format).

    Supports several shapes:
    1. Standard OTLP export: ``{"resourceSpans": [...]}``
    2. Legacy/Tempo v1 export: ``{"batches": [...]}`` with
       ``instrumentationLibrarySpans`` instead of ``scopeSpans``
    3. Tempo v2 wrapper: ``{"trace": {"resourceSpans": [...]}}``
    4. JSONL: one span per line (for streaming use cases)

    OTLP uses nanosecond timestamps - these are converted to microseconds
    to match the internal Span representation.
    """

    def format_name(self) -> str:
        return "otlp-json"

    def load(self, source: str) -> list[Trace]:
        """Load OTLP JSON file or JSONL (one span per line)."""
        with open(source) as f:
            content = f.read().strip()

        if not content:
            logger.warning("Empty trace file: %s", source)
            return []

        if content.startswith("{"):
            try:
                data = json.loads(content)
                if self._is_otlp_export(data):
                    traces = self._parse_otlp_export(data)
                else:
                    raise ValueError("Not a full OTLP export, trying JSONL")
            except (json.JSONDecodeError, ValueError):
                spans_list = [json.loads(line) for line in content.split("\n") if line.strip()]
                traces = self._parse_otlp_spans(spans_list)
        else:
            spans_list = [json.loads(line) for line in content.split("\n") if line.strip()]
            traces = self._parse_otlp_spans(spans_list)

        logger.info("Loaded %d trace(s) from %s", len(traces), source)
        return traces

    def load_from_dict(self, data: dict) -> list[Trace]:
        """Load traces from an OTLP JSON dict.

        Accepts ``resourceSpans``, legacy ``batches``, and Tempo v2's
        ``{"trace": {...}}`` wrapper.
        """
        if not self._is_otlp_export(data):
            raise ValueError("Expected OTLP JSON with 'resourceSpans' or 'batches' key (or wrapped under 'trace')")
        return self._parse_otlp_export(data)

    @staticmethod
    def _is_otlp_export(data: dict) -> bool:
        """Return True if ``data`` looks like a full OTLP export in any of the
        supported shapes (resourceSpans, batches, or Tempo v2 trace wrapper)."""
        if "resourceSpans" in data or "batches" in data:
            return True
        inner = data.get("trace")
        if isinstance(inner, dict) and ("resourceSpans" in inner or "batches" in inner):
            return True
        return False

    def _parse_otlp_export(self, data: dict) -> list[Trace]:
        """Parse full OTLP export structure (resourceSpans / batches / Tempo wrapper)."""
        # Tempo v2 wraps OTLP under {"trace": {...}}; unwrap before parsing.
        inner = data.get("trace")
        if isinstance(inner, dict) and ("resourceSpans" in inner or "batches" in inner):
            data = inner

        # Older OTLP/Tempo exports use "batches" instead of "resourceSpans".
        resource_spans = data.get("resourceSpans") or data.get("batches", [])

        all_spans = []
        for resource_span in resource_spans:
            resource_attrs = self._extract_attributes(resource_span.get("resource", {}).get("attributes", []))
            scope_spans = resource_span.get("scopeSpans") or resource_span.get("instrumentationLibrarySpans", [])
            for scope_span in scope_spans:
                scope = scope_span.get("scope") or scope_span.get("instrumentationLibrary") or {}
                scope_name = scope.get("name", "")
                scope_version = scope.get("version", "")

                for span_data in scope_span.get("spans", []):
                    span = self._parse_span(span_data, resource_attrs, scope_name, scope_version)
                    all_spans.append(span)

        return self._build_traces(all_spans)

    def _parse_otlp_spans(self, spans_data: list[dict]) -> list[Trace]:
        """Parse flat list of OTLP spans (JSONL format for streaming)."""
        all_spans = [self._parse_span(span_data, {}, "", "") for span_data in spans_data]
        return self._build_traces(all_spans)

    _GENAI_EVENT_KEYS = {OTEL_GENAI_INPUT_MESSAGES, OTEL_GENAI_OUTPUT_MESSAGES}

    def _parse_span(
        self,
        span_data: dict,
        resource_attrs: dict,
        scope_name: str,
        scope_version: str,
    ) -> Span:
        """Convert OTLP span to normalized Span object."""
        attributes = self._extract_attributes(span_data.get("attributes", []))

        if scope_name:
            attributes[OTEL_SCOPE] = scope_name
        if scope_version:
            attributes[OTEL_SCOPE_VERSION] = scope_version

        self._promote_genai_event_attributes(span_data, attributes)

        attributes.update(resource_attrs)

        start_time_ns = int(span_data.get("startTimeUnixNano", "0"))
        end_time_ns = int(span_data.get("endTimeUnixNano", "0"))
        start_time_us = start_time_ns // 1000
        duration_us = (end_time_ns - start_time_ns) // 1000

        parent_span_id = span_data.get("parentSpanId") or None

        return Span(
            trace_id=span_data.get("traceId", ""),
            span_id=span_data.get("spanId", ""),
            parent_span_id=parent_span_id,
            operation_name=span_data.get("name", ""),
            start_time=start_time_us,
            duration=duration_us,
            tags=attributes,
        )

    def _promote_genai_event_attributes(self, span_data: dict, attributes: dict) -> None:
        """Promote gen_ai.input/output.messages from span events to attributes.

        Some SDKs (e.g. Strands) store message content in span events rather
        than span attributes. This promotes those values so the converter can
        find them via normal attribute lookups.

        Accepts events in OTLP array format or flat/nested dict format.
        """
        for event in span_data.get("events", []):
            event_attrs = event.get("attributes", [])
            if isinstance(event_attrs, dict):
                flat = self._flatten_nested_dict(event_attrs)
                for key in self._GENAI_EVENT_KEYS:
                    if key in flat and key not in attributes:
                        attributes[key] = flat[key]
            else:
                for attr in event_attrs:
                    key = attr.get("key", "")
                    if key in self._GENAI_EVENT_KEYS and key not in attributes:
                        value_obj = attr.get("value", {})
                        if "stringValue" in value_obj:
                            attributes[key] = value_obj["stringValue"]

    def _extract_attributes(self, attrs) -> dict:
        """Convert attributes to a flat ``{key: value}`` dict.

        Accepts three formats:
        1. OTLP array: ``[{key, value: {stringValue|intValue|...}}]``
        2. Flat dict: ``{"gen_ai.operation.name": "chat"}``
        3. Nested dict (ClickHouse JSON column): ``{"gen_ai": {"operation": {"name": "chat"}}}``

        Formats 2 and 3 are auto-detected by checking whether *attrs* is a dict.
        Nested dicts are recursively flattened to dot-notation keys.
        """
        if isinstance(attrs, dict):
            return self._flatten_nested_dict(attrs)

        result = {}
        for attr in attrs:
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
            elif "arrayValue" in value_obj:
                result[key] = json.dumps(value_obj["arrayValue"])
            elif "kvlistValue" in value_obj:
                result[key] = json.dumps(value_obj["kvlistValue"])

        return result

    @staticmethod
    def _flatten_nested_dict(d: dict, prefix: str = "") -> dict:
        """Recursively flatten a nested dict to dot-notation keys.

        ``{"gen_ai": {"operation": {"name": "chat"}}}``
        becomes ``{"gen_ai.operation.name": "chat"}``.

        Already-flat keys (e.g. ``{"service.name": "agent"}``) pass through
        unchanged.
        """
        result = {}
        for key, value in d.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict):
                result.update(OtlpJsonLoader._flatten_nested_dict(value, full_key))
            else:
                result[full_key] = value
        return result

    def _build_traces(self, all_spans: list[Span]) -> list[Trace]:
        """Group spans by trace_id and build parent-child relationships."""
        traces_by_id: dict[str, list[Span]] = {}

        for span in all_spans:
            if span.trace_id not in traces_by_id:
                traces_by_id[span.trace_id] = []
            traces_by_id[span.trace_id].append(span)

        traces = []
        for trace_id, spans in traces_by_id.items():
            spans_by_id = {s.span_id: s for s in spans}
            root_spans = []

            for span in spans:
                if span.parent_span_id and span.parent_span_id in spans_by_id:
                    spans_by_id[span.parent_span_id].children.append(span)
                else:
                    root_spans.append(span)

            for span in spans:
                span.children.sort(key=lambda s: s.start_time)

            root_spans.sort(key=lambda s: s.start_time)

            traces.append(
                Trace(
                    trace_id=trace_id,
                    root_spans=root_spans,
                    all_spans=spans,
                )
            )

        return traces
