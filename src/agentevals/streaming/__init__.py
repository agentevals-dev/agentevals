"""Live streaming support for agentevals."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def enable_streaming(
    ws_url: str = "ws://localhost:8001/ws/traces",
    eval_set_id: str | None = None,
    session_name: str | None = None,
):
    """Enable live streaming of OTel spans to agentevals dev server.

    Usage:
        from agentevals.streaming import enable_streaming

        async with enable_streaming("ws://localhost:8001/ws/traces", eval_set_id="my-eval"):
            # Your agent code here
            agent.invoke("...")
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from .processor import AgentEvalsStreamingProcessor
    except ImportError as exc:
        logger.error(
            "opentelemetry-sdk required for streaming. Install with: "
            "pip install opentelemetry-sdk websockets"
        )
        raise

    session_id = session_name or f"session-{uuid.uuid4().hex[:8]}"
    trace_id = uuid.uuid4().hex

    processor = AgentEvalsStreamingProcessor(ws_url, session_id, trace_id)
    await processor.connect(eval_set_id=eval_set_id)

    tracer_provider = trace.get_tracer_provider()
    if isinstance(tracer_provider, TracerProvider):
        tracer_provider.add_span_processor(processor)
    else:
        logger.warning(
            "No TracerProvider found. Streaming may not work. "
            "Ensure OpenTelemetry is configured in your agent."
        )

    try:
        yield session_id
    finally:
        await processor.shutdown_async()


def enable_streaming_sync(
    ws_url: str = "ws://localhost:8001/ws/traces",
    eval_set_id: str | None = None,
    session_name: str | None = None,
):
    """Synchronous wrapper for enable_streaming (sets up processor but doesn't manage lifecycle).

    .. deprecated:: 0.2.0
        Use the async :func:`enable_streaming` context manager instead.
        This function modifies the global event loop and can interfere with existing async code.

    For use in non-async code. Note: You need to manually manage the event loop.

    Args:
        ws_url: WebSocket URL of the agentevals dev server
        eval_set_id: Optional ID of eval set to use for evaluation
        session_name: Optional custom session name

    Returns:
        AgentEvalsStreamingProcessor instance that must be manually shut down

    Warning:
        This function is deprecated and will be removed in a future version.
        Prefer using the async version for better compatibility.
    """
    import warnings
    warnings.warn(
        "enable_streaming_sync is deprecated and will be removed in a future version. "
        "Use the async enable_streaming() context manager instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from .processor import AgentEvalsStreamingProcessor
    except ImportError:
        logger.error(
            "opentelemetry-sdk required for streaming. "
            "Install with: pip install opentelemetry-sdk websockets"
        )
        return

    session_id = session_name or f"session-{uuid.uuid4().hex[:8]}"
    trace_id = uuid.uuid4().hex

    processor = AgentEvalsStreamingProcessor(ws_url, session_id, trace_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(processor.connect(eval_set_id=eval_set_id))

    tracer_provider = trace.get_tracer_provider()
    if isinstance(tracer_provider, TracerProvider):
        tracer_provider.add_span_processor(processor)

    print(f"[agentevals] Connected to dev server")
    print(f"[agentevals] Session: {session_id}")
    if eval_set_id:
        print(f"[agentevals] Eval set: {eval_set_id}")

    return processor


__all__ = ["enable_streaming", "enable_streaming_sync"]
