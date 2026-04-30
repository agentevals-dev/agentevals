"""Tests for OTLP/JSON trace loader."""

import json
import os
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
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "my-agent"}}]},
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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
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


class TestLoadFromDict:
    """Tests for OtlpJsonLoader.load_from_dict()."""

    def test_load_from_dict_basic(self):
        loader = OtlpJsonLoader()
        data = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test-agent"}}]},
                    "scopeSpans": [
                        {
                            "scope": {"name": "gcp.vertex.agent", "version": "1.0"},
                            "spans": [
                                {
                                    "traceId": "abc123",
                                    "spanId": "span1",
                                    "name": "invoke_agent",
                                    "startTimeUnixNano": "1000000000",
                                    "endTimeUnixNano": "2000000000",
                                    "attributes": [{"key": "gen_ai.agent.name", "value": {"stringValue": "my_agent"}}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)

        assert len(traces) == 1
        assert traces[0].trace_id == "abc123"
        span = traces[0].all_spans[0]
        assert span.tags["gen_ai.agent.name"] == "my_agent"
        assert span.tags["service.name"] == "test-agent"
        assert span.tags["otel.scope.name"] == "gcp.vertex.agent"

    def test_load_from_dict_missing_resource_spans(self):
        loader = OtlpJsonLoader()
        with pytest.raises(ValueError, match="resourceSpans"):
            loader.load_from_dict({"foo": "bar"})

    def test_load_from_dict_empty_resource_spans(self):
        loader = OtlpJsonLoader()
        traces = loader.load_from_dict({"resourceSpans": []})
        assert traces == []


class TestFlatDictAttributes:
    """Tests for flat dict attribute format (e.g. from simplified producers)."""

    def test_span_attributes_as_flat_dict(self):
        loader = OtlpJsonLoader()
        data = {
            "resourceSpans": [
                {
                    "resource": {"attributes": {"service.name": "my-agent"}},
                    "scopeSpans": [
                        {
                            "scope": {"name": "test-scope"},
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "test",
                                    "startTimeUnixNano": "1000000000",
                                    "endTimeUnixNano": "2000000000",
                                    "attributes": {
                                        "gen_ai.operation.name": "chat",
                                        "gen_ai.usage.input_tokens": 167,
                                        "gen_ai.usage.output_tokens": 42,
                                        "enabled": True,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)
        span = traces[0].all_spans[0]

        assert span.tags["gen_ai.operation.name"] == "chat"
        assert span.tags["gen_ai.usage.input_tokens"] == 167
        assert span.tags["gen_ai.usage.output_tokens"] == 42
        assert span.tags["enabled"] is True
        assert span.tags["service.name"] == "my-agent"

    def test_resource_attributes_as_flat_dict(self):
        loader = OtlpJsonLoader()
        data = {
            "resourceSpans": [
                {
                    "resource": {"attributes": {"service.name": "agent", "k8s.namespace.name": "default"}},
                    "scopeSpans": [
                        {
                            "scope": {},
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "test",
                                    "startTimeUnixNano": "0",
                                    "endTimeUnixNano": "0",
                                    "attributes": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)
        span = traces[0].all_spans[0]
        assert span.tags["service.name"] == "agent"
        assert span.tags["k8s.namespace.name"] == "default"


class TestNestedDictAttributes:
    """Tests for ClickHouse JSON column format (nested dicts auto-flattened)."""

    def test_nested_dict_flattened_to_dot_notation(self):
        loader = OtlpJsonLoader()
        data = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": {
                            "service": {"name": "my-agent"},
                            "k8s": {"namespace": {"name": "prod"}},
                            "cluster_name": "mgmt",
                        }
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "strands.telemetry.tracer"},
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "invoke_agent",
                                    "startTimeUnixNano": "1000000000",
                                    "endTimeUnixNano": "2000000000",
                                    "attributes": {
                                        "gen_ai": {
                                            "operation": {"name": "invoke_agent"},
                                            "agent": {"name": "dice_agent"},
                                            "request": {"model": "gpt-4o"},
                                            "usage": {"input_tokens": 167, "output_tokens": 11},
                                        },
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)
        span = traces[0].all_spans[0]

        assert span.tags["gen_ai.operation.name"] == "invoke_agent"
        assert span.tags["gen_ai.agent.name"] == "dice_agent"
        assert span.tags["gen_ai.request.model"] == "gpt-4o"
        assert span.tags["gen_ai.usage.input_tokens"] == 167
        assert span.tags["gen_ai.usage.output_tokens"] == 11
        assert span.tags["service.name"] == "my-agent"
        assert span.tags["k8s.namespace.name"] == "prod"
        assert span.tags["cluster_name"] == "mgmt"

    def test_nested_event_attributes_flattened(self):
        loader = OtlpJsonLoader()
        messages_json = '[{"role": "user", "parts": [{"text": "Hello"}]}]'
        data = {
            "resourceSpans": [
                {
                    "resource": {"attributes": {}},
                    "scopeSpans": [
                        {
                            "scope": {},
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "chat",
                                    "startTimeUnixNano": "0",
                                    "endTimeUnixNano": "0",
                                    "attributes": {},
                                    "events": [
                                        {
                                            "timeUnixNano": "0",
                                            "name": "gen_ai.client.inference.operation.details",
                                            "attributes": {
                                                "gen_ai": {
                                                    "input": {"messages": messages_json},
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)
        span = traces[0].all_spans[0]
        assert span.tags["gen_ai.input.messages"] == messages_json

    def test_mixed_nested_and_flat_keys(self):
        """Keys that are already flat should pass through unchanged."""
        loader = OtlpJsonLoader()
        data = {
            "resourceSpans": [
                {
                    "resource": {"attributes": {}},
                    "scopeSpans": [
                        {
                            "scope": {},
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "test",
                                    "startTimeUnixNano": "0",
                                    "endTimeUnixNano": "0",
                                    "attributes": {
                                        "simple_key": "simple_value",
                                        "nested": {"deep": {"key": 42}},
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        traces = loader.load_from_dict(data)
        span = traces[0].all_spans[0]
        assert span.tags["simple_key"] == "simple_value"
        assert span.tags["nested.deep.key"] == 42


def _tempo_v1_export(span_overrides=None) -> dict:
    """Build a Tempo v1-style export with batches + instrumentationLibrarySpans.

    Tempo v1 (and many older OTLP exporters) use ``batches`` instead of
    ``resourceSpans`` and ``instrumentationLibrarySpans`` instead of
    ``scopeSpans``. The ``instrumentationLibrary`` field replaces ``scope``.
    """
    span = {
        "traceId": "tempo-trace",
        "spanId": "tempo-span",
        "name": "invoke_agent helm_agent",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
        ],
    }
    if span_overrides:
        span.update(span_overrides)

    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "helm_agent"}},
                    ]
                },
                "instrumentationLibrarySpans": [
                    {
                        "instrumentationLibrary": {
                            "name": "opentelemetry.instrumentation.httpx",
                            "version": "0.59b0",
                        },
                        "spans": [span],
                    }
                ],
            }
        ]
    }


class TestTempoShapeSupport:
    """Regression tests for the loader accepting Tempo-flavored OTLP exports.

    These shapes appeared after Tempo round-tripping and were the original
    cause of the offline-eval crash reported in issue #127.
    """

    def test_load_tempo_v1_batches(self):
        loader = OtlpJsonLoader()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_tempo_v1_export(), f)
            path = f.name
        try:
            traces = loader.load(path)
            assert len(traces) == 1
            assert traces[0].trace_id == "tempo-trace"
            assert len(traces[0].all_spans) == 1
        finally:
            Path(path).unlink()

    def test_tempo_v1_resource_attrs_propagate_to_spans(self):
        loader = OtlpJsonLoader()
        traces = loader.load_from_dict(_tempo_v1_export())
        span = traces[0].all_spans[0]
        assert span.tags["service.name"] == "helm_agent"

    def test_tempo_v1_instrumentation_library_maps_to_scope(self):
        loader = OtlpJsonLoader()
        traces = loader.load_from_dict(_tempo_v1_export())
        span = traces[0].all_spans[0]
        assert span.tags["otel.scope.name"] == "opentelemetry.instrumentation.httpx"
        assert span.tags["otel.scope.version"] == "0.59b0"

    def test_tempo_v2_trace_wrapper_unwrapped(self):
        loader = OtlpJsonLoader()
        wrapped = {"trace": _tempo_v1_export()}
        traces = loader.load_from_dict(wrapped)
        assert len(traces) == 1
        assert traces[0].trace_id == "tempo-trace"

    def test_tempo_v2_wrapper_around_resource_spans(self):
        loader = OtlpJsonLoader()
        inner = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": "wrapped-trace",
                                    "spanId": "wrapped-span",
                                    "name": "op",
                                    "startTimeUnixNano": "0",
                                    "endTimeUnixNano": "1000",
                                    "attributes": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        traces = loader.load_from_dict({"trace": inner})
        assert len(traces) == 1
        assert traces[0].trace_id == "wrapped-trace"

    def test_load_from_dict_rejects_unknown_shape(self):
        loader = OtlpJsonLoader()
        with pytest.raises(ValueError, match="resourceSpans"):
            loader.load_from_dict({"unrecognized": True})

    def test_real_tempo_fixture_loads_with_expected_span_count(self):
        loader = OtlpJsonLoader()
        fixture = os.path.join(os.path.dirname(__file__), "..", "samples", "tempo_export_with_batches.json")
        traces = loader.load(fixture)
        assert len(traces) == 1
        assert traces[0].trace_id == "dd547580319ab0312cee07f1def50dad"
        assert len(traces[0].all_spans) == 86
