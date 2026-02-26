"""OTLP/JSON trace loader for native OpenTelemetry format."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Span, Trace, TraceLoader

logger = logging.getLogger(__name__)


class OtlpJsonLoader(TraceLoader):
    """Loads traces from OTLP/JSON format (native OpenTelemetry format).

    Supports two formats:
    1. Full OTLP export with resourceSpans structure
    2. JSONL format - one span per line (for streaming use cases)

    OTLP uses nanosecond timestamps - these are converted to microseconds
    to match the internal Span representation.
    """

    def format_name(self) -> str:
        return "otlp-json"

    def load(self, source: str) -> list[Trace]:
        """Load OTLP JSON file or JSONL (one span per line)."""
        with open(source, "r") as f:
            content = f.read().strip()

        if not content:
            logger.warning("Empty trace file: %s", source)
            return []

        if content.startswith("{"):
            try:
                data = json.loads(content)
                if "resourceSpans" in data:
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

    def _parse_otlp_export(self, data: dict) -> list[Trace]:
        """Parse full OTLP export structure with resourceSpans."""
        all_spans = []

        for resource_span in data.get("resourceSpans", []):
            resource_attrs = self._extract_attributes(
                resource_span.get("resource", {}).get("attributes", [])
            )

            for scope_span in resource_span.get("scopeSpans", []):
                scope = scope_span.get("scope", {})
                scope_name = scope.get("name", "")
                scope_version = scope.get("version", "")

                for span_data in scope_span.get("spans", []):
                    span = self._parse_span(
                        span_data, resource_attrs, scope_name, scope_version
                    )
                    all_spans.append(span)

        return self._build_traces(all_spans)

    def _parse_otlp_spans(self, spans_data: list[dict]) -> list[Trace]:
        """Parse flat list of OTLP spans (JSONL format for streaming)."""
        all_spans = [
            self._parse_span(span_data, {}, "", "") for span_data in spans_data
        ]
        return self._build_traces(all_spans)

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
            attributes["otel.scope.name"] = scope_name
        if scope_version:
            attributes["otel.scope.version"] = scope_version

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

    def _extract_attributes(self, attrs_list: list[dict]) -> dict:
        """Convert OTLP attributes array to flat dict.

        OTLP attributes are [{key, value: {stringValue|intValue|...}}]
        We flatten to {key: value} for easier use.
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
            elif "arrayValue" in value_obj:
                result[key] = json.dumps(value_obj["arrayValue"])
            elif "kvlistValue" in value_obj:
                result[key] = json.dumps(value_obj["kvlistValue"])

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
