"""Tests for the auto-detection trace loader entrypoint."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from agentevals.loader import detect_format, load_traces

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")
TEMPO_FIXTURE = os.path.join(SAMPLES_DIR, "tempo_export_with_batches.json")


def _write_tmp(content: str, suffix: str = ".json") -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name


def _otlp_doc(trace_id: str = "t1") -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "test"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": "s1",
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


def _tempo_v1_doc(trace_id: str = "t1") -> dict:
    return {
        "batches": [
            {
                "resource": {"attributes": []},
                "instrumentationLibrarySpans": [
                    {
                        "instrumentationLibrary": {"name": "test"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": "s1",
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


def _jaeger_doc() -> dict:
    return {
        "data": [
            {
                "traceID": "abc",
                "spans": [
                    {
                        "traceID": "abc",
                        "spanID": "s1",
                        "operationName": "op",
                        "startTime": 0,
                        "duration": 1000,
                        "tags": [],
                        "references": [],
                    }
                ],
                "processes": {},
            }
        ]
    }


class TestDetectFormat:
    def test_jsonl_extension_implies_otlp(self):
        path = _write_tmp("not even json", suffix=".jsonl")
        try:
            assert detect_format(path) == "otlp-json"
        finally:
            os.unlink(path)

    def test_otlp_resource_spans(self):
        path = _write_tmp(json.dumps(_otlp_doc()))
        try:
            assert detect_format(path) == "otlp-json"
        finally:
            os.unlink(path)

    def test_tempo_v1_batches(self):
        path = _write_tmp(json.dumps(_tempo_v1_doc()))
        try:
            assert detect_format(path) == "otlp-json"
        finally:
            os.unlink(path)

    def test_tempo_v2_trace_wrapper(self):
        wrapped = {"trace": _otlp_doc()}
        path = _write_tmp(json.dumps(wrapped))
        try:
            assert detect_format(path) == "otlp-json"
        finally:
            os.unlink(path)

    def test_tempo_v2_trace_wrapper_with_batches(self):
        wrapped = {"trace": _tempo_v1_doc()}
        path = _write_tmp(json.dumps(wrapped))
        try:
            assert detect_format(path) == "otlp-json"
        finally:
            os.unlink(path)

    def test_jaeger_data(self):
        path = _write_tmp(json.dumps(_jaeger_doc()))
        try:
            assert detect_format(path) == "jaeger-json"
        finally:
            os.unlink(path)

    def test_invalid_json_returns_none(self):
        path = _write_tmp("not json")
        try:
            assert detect_format(path) is None
        finally:
            os.unlink(path)

    def test_missing_file_returns_none(self):
        assert detect_format("/nonexistent/path/that/should/not/exist.json") is None

    def test_unrecognized_shape_returns_none(self):
        path = _write_tmp(json.dumps({"foo": "bar"}))
        try:
            assert detect_format(path) is None
        finally:
            os.unlink(path)

    def test_top_level_array_returns_none(self):
        path = _write_tmp(json.dumps([{"resourceSpans": []}]))
        try:
            assert detect_format(path) is None
        finally:
            os.unlink(path)

    def test_real_tempo_export_detected_as_otlp(self):
        assert detect_format(TEMPO_FIXTURE) == "otlp-json"


class TestLoadTraces:
    def test_auto_loads_otlp_doc(self):
        path = _write_tmp(json.dumps(_otlp_doc(trace_id="auto-otlp")))
        try:
            traces = load_traces(path)
            assert len(traces) == 1
            assert traces[0].trace_id == "auto-otlp"
        finally:
            os.unlink(path)

    def test_auto_loads_tempo_v1_batches(self):
        path = _write_tmp(json.dumps(_tempo_v1_doc(trace_id="auto-tempo")))
        try:
            traces = load_traces(path)
            assert len(traces) == 1
            assert traces[0].trace_id == "auto-tempo"
        finally:
            os.unlink(path)

    def test_auto_loads_tempo_v2_wrapper(self):
        path = _write_tmp(json.dumps({"trace": _otlp_doc(trace_id="auto-wrapped")}))
        try:
            traces = load_traces(path)
            assert len(traces) == 1
            assert traces[0].trace_id == "auto-wrapped"
        finally:
            os.unlink(path)

    def test_auto_loads_jaeger(self):
        path = _write_tmp(json.dumps(_jaeger_doc()))
        try:
            traces = load_traces(path)
            assert len(traces) == 1
            assert traces[0].trace_id == "abc"
        finally:
            os.unlink(path)

    def test_explicit_format_override(self):
        path = _write_tmp(json.dumps(_otlp_doc(trace_id="forced")))
        try:
            traces = load_traces(path, format="otlp-json")
            assert len(traces) == 1
            assert traces[0].trace_id == "forced"
        finally:
            os.unlink(path)

    def test_unknown_format_override_raises(self):
        path = _write_tmp(json.dumps(_otlp_doc()))
        try:
            with pytest.raises(ValueError, match="Unknown trace format"):
                load_traces(path, format="not-a-real-format")
        finally:
            os.unlink(path)

    def test_unrecognized_shape_raises(self):
        path = _write_tmp(json.dumps({"unrecognized": True}))
        try:
            with pytest.raises(ValueError, match="Could not detect trace format"):
                load_traces(path)
        finally:
            os.unlink(path)

    def test_real_tempo_fixture_loads(self):
        traces = load_traces(TEMPO_FIXTURE)
        assert len(traces) == 1
        assert traces[0].trace_id == "dd547580319ab0312cee07f1def50dad"
        assert len(traces[0].all_spans) == 86
