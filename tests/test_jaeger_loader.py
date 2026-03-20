import json
import os

import pytest

from agentevals.loader.base import Span, Trace
from agentevals.loader.jaeger import JaegerJsonLoader

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


@pytest.fixture
def loader():
    return JaegerJsonLoader()


@pytest.fixture
def minimal_jaeger_json():
    return {
        "data": [
            {
                "traceID": "abc123",
                "spans": [
                    {
                        "traceID": "abc123",
                        "spanID": "span1",
                        "operationName": "root_op",
                        "references": [],
                        "startTime": 1000000,
                        "duration": 5000000,
                        "tags": [{"key": "key1", "type": "string", "value": "val1"}],
                    },
                    {
                        "traceID": "abc123",
                        "spanID": "span2",
                        "operationName": "child_op",
                        "references": [{"refType": "CHILD_OF", "spanID": "span1"}],
                        "startTime": 2000000,
                        "duration": 1000000,
                        "tags": [],
                    },
                ],
            }
        ]
    }


class TestJaegerJsonLoader:
    def test_format_name(self, loader):
        assert loader.format_name() == "jaeger-json"

    def test_load_minimal(self, loader, minimal_jaeger_json, tmp_path):
        path = tmp_path / "test.json"
        path.write_text(json.dumps(minimal_jaeger_json))

        traces = loader.load(str(path))
        assert len(traces) == 1

        trace = traces[0]
        assert trace.trace_id == "abc123"
        assert len(trace.all_spans) == 2
        assert len(trace.root_spans) == 1

    def test_span_tree_structure(self, loader, minimal_jaeger_json, tmp_path):
        path = tmp_path / "test.json"
        path.write_text(json.dumps(minimal_jaeger_json))

        trace = loader.load(str(path))[0]

        root = trace.root_spans[0]
        assert root.span_id == "span1"
        assert root.operation_name == "root_op"
        assert len(root.children) == 1

        child = root.children[0]
        assert child.span_id == "span2"
        assert child.operation_name == "child_op"
        assert child.parent_span_id == "span1"

    def test_tags_parsed(self, loader, minimal_jaeger_json, tmp_path):
        path = tmp_path / "test.json"
        path.write_text(json.dumps(minimal_jaeger_json))

        trace = loader.load(str(path))[0]
        root = trace.root_spans[0]
        assert root.get_tag("key1") == "val1"
        assert root.get_tag("missing") is None
        assert root.get_tag("missing", "default") == "default"

    def test_invalid_format_raises(self, loader, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"not_data": []}))

        with pytest.raises(ValueError, match="Invalid Jaeger JSON format"):
            loader.load(str(path))

    def test_empty_spans_skipped(self, loader, tmp_path):
        data = {"data": [{"traceID": "empty", "spans": []}]}
        path = tmp_path / "empty.json"
        path.write_text(json.dumps(data))

        traces = loader.load(str(path))
        assert len(traces) == 0

    def test_find_spans_by_operation(self, loader, minimal_jaeger_json, tmp_path):
        path = tmp_path / "test.json"
        path.write_text(json.dumps(minimal_jaeger_json))

        trace = loader.load(str(path))[0]
        found = trace.find_spans_by_operation("child")
        assert len(found) == 1
        assert found[0].operation_name == "child_op"

    def test_find_spans_by_tag(self, loader, minimal_jaeger_json, tmp_path):
        path = tmp_path / "test.json"
        path.write_text(json.dumps(minimal_jaeger_json))

        trace = loader.load(str(path))[0]
        found = trace.find_spans_by_tag("key1", "val1")
        assert len(found) == 1
        assert found[0].span_id == "span1"

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(SAMPLES_DIR, "helm.json")),
        reason="Sample file not available",
    )
    def test_load_helm_sample(self, loader):
        traces = loader.load(os.path.join(SAMPLES_DIR, "helm.json"))
        assert len(traces) == 1
        trace = traces[0]
        assert trace.trace_id == "3e289017fe03ffd7c4145316d2eb3d0d"
        assert len(trace.all_spans) > 0

        adk_spans = trace.find_spans_by_tag("otel.scope.name", "gcp.vertex.agent")
        assert len(adk_spans) >= 3  # invoke_agent, call_llm x2, execute_tool

    def test_span_end_time(self):
        span = Span(
            trace_id="t",
            span_id="s",
            parent_span_id=None,
            operation_name="op",
            start_time=1000,
            duration=500,
        )
        assert span.end_time == 1500
