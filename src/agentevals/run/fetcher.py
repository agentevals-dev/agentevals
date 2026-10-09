"""Trace fetchers — resolve a run spec's ``target`` into a list of Trace objects.

Two implementations ship: ``inline`` (the JSON payload is embedded in the
spec) and ``http`` (the worker GETs ``{base_url}/{trace_id}`` with headers
sourced from ``context.headers``). Auth headers are pass-through; this layer
does not validate them.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from ..loader import load_traces_from_obj
from ..otel.model import Trace
from ..storage.models import TraceTarget

logger = logging.getLogger(__name__)


class TraceFetcher(Protocol):
    async def fetch(self, target: TraceTarget, context: dict) -> list[Trace]: ...


class InlineTraceFetcher:
    """Decodes the JSON document embedded in the run spec."""

    async def fetch(self, target: TraceTarget, context: dict) -> list[Trace]:
        if not target.inline:
            raise ValueError("InlineTraceFetcher requires target.inline to be set")
        return load_traces_from_obj(target.inline, format=target.trace_format)


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
        return load_traces_from_obj(payload, format=target.trace_format)


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
