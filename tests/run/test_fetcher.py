"""Trace fetcher dispatch + InlineTraceFetcher behavior."""

from __future__ import annotations

import json

import pytest

from agentevals.run.fetcher import HttpTraceFetcher, InlineTraceFetcher, resolve_fetcher
from agentevals.storage.models import TraceTarget


class TestResolveFetcher:
    def test_inline_returns_inline_fetcher(self):
        f = resolve_fetcher(TraceTarget(kind="inline", inline={}))
        assert isinstance(f, InlineTraceFetcher)

    def test_http_returns_http_fetcher(self):
        f = resolve_fetcher(TraceTarget(kind="http", base_url="https://x", trace_id="abc"))
        assert isinstance(f, HttpTraceFetcher)

    def test_uploaded_rejected_with_clear_error(self):
        """Uploaded targets cannot be re-executed by the worker; they only
        record audit metadata for /api/evaluate calls. resolve_fetcher must
        raise rather than silently returning None or a fallback fetcher."""
        with pytest.raises(ValueError, match="cannot be re-executed"):
            resolve_fetcher(TraceTarget(kind="uploaded"))


class TestInlineTraceFetcher:
    async def test_loads_jaeger_format(self, tmp_path):
        sample = {
            "data": [
                {
                    "traceID": "1234",
                    "spans": [
                        {
                            "traceID": "1234",
                            "spanID": "abcd",
                            "operationName": "op",
                            "startTime": 1000,
                            "duration": 100,
                            "tags": [],
                            "logs": [],
                            "references": [],
                            "processID": "p1",
                        }
                    ],
                    "processes": {"p1": {"serviceName": "svc"}},
                }
            ]
        }
        fetcher = InlineTraceFetcher()
        traces = await fetcher.fetch(
            TraceTarget(kind="inline", inline=sample),
            context={},
        )
        assert len(traces) >= 1

    async def test_missing_inline_raises(self):
        fetcher = InlineTraceFetcher()
        with pytest.raises(ValueError, match="target.inline"):
            await fetcher.fetch(TraceTarget(kind="inline"), context={})


class TestHttpTraceFetcher:
    """HttpTraceFetcher hits the network; we test the validation path that
    runs before any HTTP traffic. End-to-end HTTP behavior is covered by
    the run-flow integration test."""

    async def test_missing_base_url_raises(self):
        fetcher = HttpTraceFetcher()
        with pytest.raises(ValueError, match="base_url"):
            await fetcher.fetch(TraceTarget(kind="http", trace_id="abc"), context={})

    async def test_missing_trace_id_raises(self):
        fetcher = HttpTraceFetcher()
        with pytest.raises(ValueError, match="base_url"):
            await fetcher.fetch(TraceTarget(kind="http"), context={})
