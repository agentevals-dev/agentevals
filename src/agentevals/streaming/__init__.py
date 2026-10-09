"""Live streaming support for agentevals.

``enable_streaming`` and ``enable_streaming_sync`` are deprecated wrappers kept for existing
callers; use :class:`agentevals.AgentEvals` sessions instead.
"""

from __future__ import annotations

import warnings
from contextlib import asynccontextmanager
from typing import Any

from ..otel.sdk_export import LEGACY_WS_URL


def _deprecated(name: str) -> None:
    warnings.warn(
        f"{name} is deprecated and will be removed in a future version. "
        "Use AgentEvals().session() or AgentEvals().session_async() instead.",
        DeprecationWarning,
        stacklevel=3,
    )


@asynccontextmanager
async def enable_streaming(
    ws_url: str = LEGACY_WS_URL,
    eval_set_id: str | None = None,
    session_name: str | None = None,
):
    """Deprecated: stream the spans produced inside the block to the agentevals dev server."""
    from ..sdk import AgentEvals

    _deprecated("enable_streaming")
    async with AgentEvals(ws_url=ws_url, auto_instrument=False).session_async(
        eval_set_id=eval_set_id, session_name=session_name
    ) as name:
        yield name


class StreamingHandle:
    """An open session started by :func:`enable_streaming_sync`; call :meth:`shutdown` to end it."""

    def __init__(self, cm: Any, session_id: str):
        self._cm = cm
        self.session_id = session_id

    def shutdown(self) -> None:
        if self._cm is not None:
            cm, self._cm = self._cm, None
            cm.__exit__(None, None, None)


def enable_streaming_sync(
    ws_url: str = LEGACY_WS_URL,
    eval_set_id: str | None = None,
    session_name: str | None = None,
) -> StreamingHandle:
    """Deprecated: start a session in the current context and return a handle that ends it."""
    from ..sdk import AgentEvals

    _deprecated("enable_streaming_sync")
    cm = AgentEvals(ws_url=ws_url, auto_instrument=False).session(eval_set_id=eval_set_id, session_name=session_name)
    return StreamingHandle(cm, cm.__enter__())


__all__ = ["StreamingHandle", "enable_streaming", "enable_streaming_sync"]
