"""Tests for OTLP/JSON trace loader."""

import json
import tempfile
from pathlib import Path

import pytest

from agentevals.loader.otlp import OtlpJsonLoader


@pytest.fixture
def sample_otlp_span():
    """Sample OTLP span in JSON format."""
    return {
        "traceId": "3e289017fe03ffd7c4145316d2eb3d0d",
        "spanId": "1f9762ca1e03e2d2",
        "parentSpanId": "e3daa973379bbe3b",
        "name": "invoke_agent hello_world",
        "kind": 1,
        "startTimeUnixNano": "1771237534577907000",
        "endTimeUnixNano": "1771237534583417000",
        "attributes": [
            {"key": "otel.scope.name", "value": {"stringValue": "gcp.vertex.agent"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
            {"key": "gen_ai.agent.name", "value": {"stringValue": "hello_world"}},
            {"key": "count", "value": {"intValue": 42}},
            {"key": "score", "value": {"doubleValue": 0.95}},
            {"key": "enabled", "value": {"boolValue": True}},
        ],
        "status": {"code": 1},
    }


def test_otlp_loader_format_name():
    """Test loader returns correct format name."""
    loader = OtlpJsonLoader()
    assert loader.format_name() == "otlp-json"


def test_otlp_loader_jsonl_format(sample_otlp_span):
    """Test loading JSONL format (one span per line)."""
    loader = OtlpJsonLoader()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(sample_otlp_span) + "\n")
        temp_path = f.name

    try:
        traces = loader.load(temp_path)

        assert len(traces) == 1
        trace = traces[0]

        assert trace.trace_id == "3e289017fe03ffd7c4145316d2eb3d0d"
        assert len(trace.all_spans) == 1

        span = trace.all_spans[0]
        assert span.span_id == "1f9762ca1e03e2d2"
        assert span.parent_span_id == "e3daa973379bbe3b"
        assert span.operation_name == "invoke_agent hello_world"

        assert span.start_time == 1771237534577907000 // 1000
        assert span.duration == (1771237534583417000 - 1771237534577907000) // 1000

        assert span.tags["otel.scope.name"] == "gcp.vertex.agent"
        assert span.tags["gen_ai.operation.name"] == "invoke_agent"
        assert span.tags["gen_ai.agent.name"] == "hello_world"
        assert span.tags["count"] == 42
        assert span.tags["score"] == 0.95
        assert span.tags["enabled"] is True

    finally:
        Path(temp_path).unlink()


def test_otlp_loader_full_export():
    """Test loading full OTLP export with resourceSpans structure."""
    loader = OtlpJsonLoader()

    otlp_export = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "my-agent"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "gcp.vertex.agent",
                            "version": "1.0.0",
                        },
                        "spans": [
                            {
                                "traceId": "abc123",
                                "spanId": "span1",
                                "name": "test_span",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {
                                        "key": "test_attr",
                                        "value": {"stringValue": "test_value"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(otlp_export, f)
        temp_path = f.name

    try:
        traces = loader.load(temp_path)

        assert len(traces) == 1
        trace = traces[0]

        assert trace.trace_id == "abc123"
        assert len(trace.all_spans) == 1

        span = trace.all_spans[0]
        assert span.span_id == "span1"
        assert span.operation_name == "test_span"

        assert span.tags["otel.scope.name"] == "gcp.vertex.agent"
        assert span.tags["otel.scope.version"] == "1.0.0"
        assert span.tags["service.name"] == "my-agent"
        assert span.tags["test_attr"] == "test_value"

    finally:
        Path(temp_path).unlink()


def test_otlp_loader_parent_child_relationships():
    """Test that parent-child relationships are built correctly."""
    loader = OtlpJsonLoader()

    spans = [
        {
            "traceId": "trace1",
            "spanId": "root",
            "name": "root_span",
            "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "5000000000",
            "attributes": [],
        },
        {
            "traceId": "trace1",
            "spanId": "child1",
            "parentSpanId": "root",
            "name": "child_span_1",
            "startTimeUnixNano": "2000000000",
            "endTimeUnixNano": "3000000000",
            "attributes": [],
        },
        {
            "traceId": "trace1",
            "spanId": "child2",
            "parentSpanId": "root",
            "name": "child_span_2",
            "startTimeUnixNano": "3000000000",
            "endTimeUnixNano": "4000000000",
            "attributes": [],
        },
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for span in spans:
            f.write(json.dumps(span) + "\n")
        temp_path = f.name

    try:
        traces = loader.load(temp_path)

        assert len(traces) == 1
        trace = traces[0]

        assert len(trace.root_spans) == 1
        root = trace.root_spans[0]

        assert root.span_id == "root"
        assert len(root.children) == 2

        assert root.children[0].span_id == "child1"
        assert root.children[1].span_id == "child2"

        assert root.children[0].parent_span_id == "root"
        assert root.children[1].parent_span_id == "root"

    finally:
        Path(temp_path).unlink()


def test_otlp_loader_empty_file():
    """Test loading an empty file."""
    loader = OtlpJsonLoader()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        temp_path = f.name

    try:
        traces = loader.load(temp_path)
        assert traces == []

    finally:
        Path(temp_path).unlink()
