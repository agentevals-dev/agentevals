"""Result sinks — best-effort fan-out of run results.

The :class:`agentevals.storage.repos.ResultRepository` is always written;
sinks are an additional delivery channel. Sink failures are logged with
``run_id`` / ``result_id`` but do not fail the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx

from ..storage.models import Result

logger = logging.getLogger(__name__)


class ResultSink(Protocol):
    async def emit_partial(self, run_id: UUID, results: list[Result], attempt: int) -> None: ...
    async def emit_final(self, run_id: UUID, summary: dict, attempt: int) -> None: ...
    async def emit_error(self, run_id: UUID, error: str, attempt: int) -> None: ...


def _result_payload(r: Result) -> dict:
    return r.model_dump(mode="json", by_alias=True)


class StdoutSink:
    async def emit_partial(self, run_id: UUID, results: list[Result], attempt: int) -> None:
        for r in results:
            sys.stdout.write(
                json.dumps({"phase": "partial", "run_id": str(run_id), "result": _result_payload(r)}) + "\n"
            )
        sys.stdout.flush()

    async def emit_final(self, run_id: UUID, summary: dict, attempt: int) -> None:
        sys.stdout.write(json.dumps({"phase": "final", "run_id": str(run_id), "summary": summary}) + "\n")
        sys.stdout.flush()

    async def emit_error(self, run_id: UUID, error: str, attempt: int) -> None:
        sys.stdout.write(json.dumps({"phase": "error", "run_id": str(run_id), "error": error}) + "\n")
        sys.stdout.flush()


class FileSink:
    """Append-only newline-delimited JSON. Each event is one line."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def _write(self, payload: dict) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:  # noqa: ASYNC230
                f.write(json.dumps(payload) + "\n")

    async def emit_partial(self, run_id: UUID, results: list[Result], attempt: int) -> None:
        for r in results:
            await self._write({"phase": "partial", "run_id": str(run_id), "result": _result_payload(r)})

    async def emit_final(self, run_id: UUID, summary: dict, attempt: int) -> None:
        await self._write({"phase": "final", "run_id": str(run_id), "summary": summary})

    async def emit_error(self, run_id: UUID, error: str, attempt: int) -> None:
        await self._write({"phase": "error", "run_id": str(run_id), "error": error})


class HttpWebhookSink:
    """POST JSON to a URL with retries.

    Auth headers come from the spec via ``headers`` (literal values) or
    ``headers_from_env`` (env var names whose values are read at emit time).
    Reading at emit time means a host can rotate the env var without
    restarting agentevals.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        headers_from_env: dict[str, str] | None = None,
        timeout_s: float = 10.0,
        max_attempts: int = 5,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._headers_from_env = headers_from_env or {}
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts

    def _resolve_headers(self) -> dict[str, str]:
        merged = dict(self._headers)
        for header, env_var in self._headers_from_env.items():
            value = os.environ.get(env_var)
            if value is not None:
                merged[header] = value
        merged.setdefault("Content-Type", "application/json")
        return merged

    async def _post(self, payload: dict) -> None:
        delay = 0.5
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.post(self._url, json=payload, headers=self._resolve_headers())
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        logger.warning(
                            "Webhook %s returned %d: %s (run_id=%s)",
                            self._url,
                            resp.status_code,
                            resp.text[:200],
                            payload.get("run_id"),
                        )
                    return
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except (httpx.HTTPError, RuntimeError) as exc:
                last_exc = exc
            if attempt < self._max_attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
        logger.error(
            "Webhook %s failed after %d attempts: %s (run_id=%s)",
            self._url,
            self._max_attempts,
            last_exc,
            payload.get("run_id"),
        )

    async def emit_partial(self, run_id: UUID, results: list[Result], attempt: int) -> None:
        await self._post(
            {
                "phase": "partial",
                "run_id": str(run_id),
                "attempt": attempt,
                "results": [_result_payload(r) for r in results],
            }
        )

    async def emit_final(self, run_id: UUID, summary: dict, attempt: int) -> None:
        await self._post({"phase": "final", "run_id": str(run_id), "attempt": attempt, "summary": summary})

    async def emit_error(self, run_id: UUID, error: str, attempt: int) -> None:
        await self._post({"phase": "error", "run_id": str(run_id), "attempt": attempt, "error": error})


class SinkFanout:
    """Runs sinks in parallel. Failures are isolated per sink."""

    def __init__(self, sinks: list[ResultSink]) -> None:
        self._sinks = sinks

    async def emit_partial(self, run_id: UUID, results: list[Result], attempt: int) -> None:
        await asyncio.gather(
            *(self._guard(s.emit_partial(run_id, results, attempt), "partial") for s in self._sinks),
            return_exceptions=False,
        )

    async def emit_final(self, run_id: UUID, summary: dict, attempt: int) -> None:
        await asyncio.gather(
            *(self._guard(s.emit_final(run_id, summary, attempt), "final") for s in self._sinks),
            return_exceptions=False,
        )

    async def emit_error(self, run_id: UUID, error: str, attempt: int) -> None:
        await asyncio.gather(
            *(self._guard(s.emit_error(run_id, error, attempt), "error") for s in self._sinks),
            return_exceptions=False,
        )

    @staticmethod
    async def _guard(coro: Any, phase: str) -> None:
        try:
            await coro
        except Exception:
            logger.exception("sink delivery failed in phase=%s", phase)


def build_sinks(specs: list[dict]) -> SinkFanout:
    """Construct a fan-out from the run spec's ``sinks`` array.

    Each spec is a dict with ``kind`` plus kind-specific args. Unknown kinds
    are skipped with a warning so a future kind added by a host doesn't
    break older agentevals replicas mid-rollout.
    """
    sinks: list[ResultSink] = []
    for spec in specs:
        kind = spec.get("kind")
        if kind == "stdout":
            sinks.append(StdoutSink())
        elif kind == "file":
            sinks.append(FileSink(spec["path"]))
        elif kind == "http_webhook":
            sinks.append(
                HttpWebhookSink(
                    url=spec["url"],
                    headers=spec.get("headers"),
                    headers_from_env=spec.get("headers_from_env") or _extract_env_headers(spec.get("auth")),
                    timeout_s=float(spec.get("timeout_s", 10.0)),
                    max_attempts=int(spec.get("max_attempts", 5)),
                )
            )
        else:
            logger.warning("unknown sink kind '%s'; skipping", kind)
    return SinkFanout(sinks)


def _extract_env_headers(auth: Any) -> dict[str, str]:
    """Map the design-doc shape ``auth.headers.<name>.from_env`` to env-var lookups."""
    result: dict[str, str] = {}
    if not isinstance(auth, dict):
        return result
    headers = auth.get("headers") if auth.get("kind") == "headers" else None
    if not isinstance(headers, dict):
        return result
    for header_name, value in headers.items():
        if isinstance(value, dict) and "from_env" in value:
            result[header_name] = value["from_env"]
    return result
