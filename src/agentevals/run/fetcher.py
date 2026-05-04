"""Trace fetchers — resolve a run spec's ``target`` into a list of Trace objects.

Two implementations ship: ``inline`` (the JSON payload is embedded in the
spec) and ``http`` (the worker GETs ``{base_url}/{trace_id}`` with headers
sourced from ``context.headers``). Auth headers are pass-through; this layer
does not validate them.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from ..loader import load_traces
from ..loader.base import Trace
from ..storage.models import TraceTarget

logger = logging.getLogger(__name__)


class TraceFetcher(Protocol):
    async def fetch(self, target: TraceTarget, context: dict) -> list[Trace]: ...


class InlineTraceFetcher:
    """Materializes inline JSON to a temp file and parses it via the existing loader.

    The temp file dance reuses :func:`agentevals.loader.load_traces` (which
    auto-detects format) without a special-case in the loader for dict input.
    """

    async def fetch(self, target: TraceTarget, context: dict) -> list[Trace]:
        if not target.inline:
            raise ValueError("InlineTraceFetcher requires target.inline to be set")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(target.inline, f)
            path = Path(f.name)
        try:
            return load_traces(str(path), format=target.trace_format)
        finally:
            path.unlink(missing_ok=True)  # noqa: ASYNC240


class HttpTraceFetcher:
    """Fetches the trace JSON over HTTP. Auth is opaque header pass-through."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self._timeout_s = timeout_s

    async def fetch(self, target: TraceTarget, context: dict) -> list[Trace]:
        if not target.base_url or not target.trace_id:
            raise ValueError("HttpTraceFetcher requires target.base_url and target.trace_id")
        url = target.base_url.rstrip("/") + "/" + target.trace_id
        headers = (context.get("headers") if isinstance(context, dict) else {}) or {}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            path = Path(f.name)
        try:
            return load_traces(str(path), format=target.trace_format)
        finally:
            path.unlink(missing_ok=True)  # noqa: ASYNC240


def resolve_fetcher(target: TraceTarget) -> TraceFetcher:
    if target.kind == "inline":
        return InlineTraceFetcher()
    if target.kind == "http":
        return HttpTraceFetcher()
    if target.kind == "uploaded":
        raise ValueError(
            "target kind 'uploaded' records a synchronous /api/evaluate call and cannot be "
            "re-executed by the worker; the run already completed at submission time"
        )
    raise ValueError(f"unknown trace target kind '{target.kind}'")
