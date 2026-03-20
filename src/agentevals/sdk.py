"""High-level SDK for streaming agent traces to the agentevals UI.

Wraps OpenTelemetry, WebSocket, and processor boilerplate into a simple
context manager or decorator API.

Usage (context manager — primary API):

    from agentevals import AgentEvals

    app = AgentEvals()

    with app.session(eval_set_id="my-eval"):
        result = my_agent.invoke("Hello!")

Usage (decorator — shorthand for simple agents):

    app = AgentEvals(eval_set_id="my-eval")

    @app.agent
    def my_agent(prompt):
        return llm.invoke(prompt).content

    app.run(["Hello!", "Tell me a joke"])

Disabling streaming:
    Pass ``streaming=False`` to skip all WebSocket/OTel setup. The context
    managers become no-ops and your agent code runs without any agentevals
    connection. Useful for gating on an env var so the SDK stays wired up
    in code but only streams when the dev server is running::

        app = AgentEvals(streaming=os.getenv("AGENTEVALS_STREAM", "1") == "1")

Provider lifecycle:
    The SDK adds an ``AgentEvalsStreamingProcessor`` to the active
    ``TracerProvider`` for the duration of a session.  After shutdown the
    processor is inert (``on_end`` short-circuits) but remains registered
    because OTel's ``TracerProvider`` has no ``remove_span_processor``
    API.  This is harmless for typical dev workflows.  If you need a clean
    provider between sessions, pass a fresh ``TracerProvider`` via the
    ``tracer_provider`` parameter.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    from .streaming.processor import AgentEvalsLogStreamingProcessor, AgentEvalsStreamingProcessor

__all__ = ["AgentEvals"]

logger = logging.getLogger(__name__)

_DEFAULT_WS_URL = "ws://localhost:8001/ws/traces"


@dataclass(slots=True)
class _OtelSetup:
    tracer_provider: SdkTracerProvider
    processor: AgentEvalsStreamingProcessor
    logger_provider: LoggerProvider | None = field(default=None)
    log_processor: AgentEvalsLogStreamingProcessor | None = field(default=None)


class AgentEvals:
    """High-level SDK for streaming agent traces to the agentevals UI."""

    def __init__(
        self,
        ws_url: str = _DEFAULT_WS_URL,
        eval_set_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        auto_instrument: bool = True,
        capture_message_content: bool = True,
        streaming: bool = True,
    ):
        self.ws_url = ws_url
        self.eval_set_id = eval_set_id
        self.metadata = metadata or {}
        self.auto_instrument = auto_instrument
        self.capture_message_content = capture_message_content
        self.streaming = streaming

        self._agent_fn: Callable | None = None
        self._is_async: bool = False

    def agent(self, fn: Callable) -> Callable:
        """Decorator to register the agent entry point.

        The decorated function should accept a prompt string and return a result.
        Works with both sync and async functions.
        """
        self._agent_fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        return fn

    def run(
        self,
        prompts: list[str] | None = None,
        interactive: bool = False,
        eval_set_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Run the registered agent with streaming enabled.

        Args:
            prompts: List of prompts to run sequentially.
            interactive: If True, enter a REPL loop reading from stdin.
            eval_set_id: Override the eval_set_id from __init__.
            metadata: Additional metadata merged with __init__ metadata.

        Returns:
            List of agent results.
        """
        if self._agent_fn is None:
            raise RuntimeError("No agent registered. Use @app.agent to register one.")

        eff_eval_set_id = eval_set_id or self.eval_set_id
        eff_metadata = {**self.metadata, **(metadata or {})}

        if self._is_async:
            return asyncio.run(self._run_async(prompts, interactive, eff_eval_set_id, eff_metadata))
        else:
            return self._run_sync(prompts, interactive, eff_eval_set_id, eff_metadata)

    # --- Context managers (the core value) ---

    @contextmanager
    def session(
        self,
        eval_set_id: str | None = None,
        session_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tracer_provider: SdkTracerProvider | None = None,
    ):
        """Sync context manager that sets up OTel streaming.

        Args:
            eval_set_id: Evaluation set ID for matching against a golden session.
            session_name: Custom session name (auto-generated if omitted).
            metadata: Custom metadata sent with the session.
            tracer_provider: Explicit TracerProvider to use (e.g. from StrandsTelemetry).
                Falls back to the global provider, then creates a new one.
        """
        eff_session_name = session_name or self._generate_session_id()

        if not self.streaming:
            logger.debug("Streaming disabled, running without agentevals connection")
            yield eff_session_name
            return

        eff_eval_set_id = eval_set_id or self.eval_set_id
        eff_metadata = {**self.metadata, **(metadata or {})}

        setup = self._setup_otel(eff_session_name, tracer_provider)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
            daemon=True,
        )
        thread.start()

        try:
            future = asyncio.run_coroutine_threadsafe(
                setup.processor.connect(eval_set_id=eff_eval_set_id, metadata=eff_metadata),
                loop,
            )
            future.result(timeout=10)
        except Exception as exc:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            raise ConnectionError(
                f"[agentevals] Could not connect to {self.ws_url}. Is 'agentevals serve --dev' running?\n  {exc}"
            ) from exc

        setup.tracer_provider.add_span_processor(setup.processor)
        if setup.logger_provider and setup.log_processor:
            setup.logger_provider.add_log_record_processor(setup.log_processor)

        logger.info("Streaming to %s (session: %s)", self.ws_url, eff_session_name)

        try:
            yield eff_session_name
        finally:
            setup.tracer_provider.force_flush()
            if setup.logger_provider:
                setup.logger_provider.force_flush()
            fut = asyncio.run_coroutine_threadsafe(setup.processor.shutdown_async(), loop)
            try:
                fut.result(timeout=10)
            except Exception as exc:
                logger.warning("Shutdown error: %s", exc)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

    @asynccontextmanager
    async def session_async(
        self,
        eval_set_id: str | None = None,
        session_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tracer_provider: SdkTracerProvider | None = None,
    ):
        """Async context manager that sets up OTel streaming.

        Args:
            eval_set_id: Evaluation set ID for matching against a golden session.
            session_name: Custom session name (auto-generated if omitted).
            metadata: Custom metadata sent with the session.
            tracer_provider: Explicit TracerProvider to use. Falls back to the global
                provider, then creates a new one.
        """
        eff_session_name = session_name or self._generate_session_id()

        if not self.streaming:
            logger.debug("Streaming disabled, running without agentevals connection")
            yield eff_session_name
            return

        eff_eval_set_id = eval_set_id or self.eval_set_id
        eff_metadata = {**self.metadata, **(metadata or {})}

        setup = self._setup_otel(eff_session_name, tracer_provider)

        try:
            await setup.processor.connect(eval_set_id=eff_eval_set_id, metadata=eff_metadata)
        except Exception as exc:
            raise ConnectionError(
                f"[agentevals] Could not connect to {self.ws_url}. Is 'agentevals serve --dev' running?\n  {exc}"
            ) from exc

        setup.tracer_provider.add_span_processor(setup.processor)
        if setup.logger_provider and setup.log_processor:
            setup.logger_provider.add_log_record_processor(setup.log_processor)

        logger.info("Streaming to %s (session: %s)", self.ws_url, eff_session_name)

        try:
            yield eff_session_name
        finally:
            setup.tracer_provider.force_flush()
            if setup.logger_provider:
                setup.logger_provider.force_flush()
            try:
                await setup.processor.shutdown_async()
            except Exception as exc:
                logger.warning("Shutdown error: %s", exc)

    # --- Decorator run helpers ---

    async def _run_async(self, prompts, interactive, eval_set_id, metadata):
        async with self.session_async(eval_set_id=eval_set_id, metadata=metadata):
            return await self._execute_agent_async(prompts, interactive)

    async def _execute_agent_async(self, prompts, interactive):
        results = []
        if prompts:
            for i, prompt in enumerate(prompts, 1):
                print(f"[{i}/{len(prompts)}] > {prompt}")
                result = await self._agent_fn(prompt)
                print(f"  {result}")
                results.append(result)
        elif interactive:
            while True:
                try:
                    prompt = input("> ")  # noqa: ASYNC250
                except (EOFError, KeyboardInterrupt):
                    break
                result = await self._agent_fn(prompt)
                print(result)
                results.append(result)
        else:
            result = await self._agent_fn()
            results.append(result)
        return results

    def _run_sync(self, prompts, interactive, eval_set_id, metadata):
        with self.session(eval_set_id=eval_set_id, metadata=metadata):
            return self._execute_agent_sync(prompts, interactive)

    def _execute_agent_sync(self, prompts, interactive):
        results = []
        if prompts:
            for i, prompt in enumerate(prompts, 1):
                print(f"[{i}/{len(prompts)}] > {prompt}")
                result = self._agent_fn(prompt)
                print(f"  {result}")
                results.append(result)
        elif interactive:
            while True:
                try:
                    prompt = input("> ")
                except (EOFError, KeyboardInterrupt):
                    break
                result = self._agent_fn(prompt)
                print(result)
                results.append(result)
        else:
            result = self._agent_fn()
            results.append(result)
        return results

    # --- Internal helpers ---

    def _setup_otel(
        self,
        session_name: str,
        explicit_tracer_provider: SdkTracerProvider | None = None,
    ) -> _OtelSetup:
        """Configure OTel providers and create a streaming processor.

        Provider resolution order:
        1. ``explicit_tracer_provider`` if given
        2. Existing global ``TracerProvider`` (e.g. set by StrandsTelemetry)
        3. New ``TracerProvider`` created and set globally

        A ``LoggerProvider`` is only created when the OpenAI OTel instrumentor
        is installed, since it's the only pattern that emits message content
        via OTel log records rather than span events.
        """
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from .streaming.processor import AgentEvalsLogStreamingProcessor, AgentEvalsStreamingProcessor

        if self.capture_message_content:
            os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")

        if explicit_tracer_provider is not None:
            tracer_provider = explicit_tracer_provider
        else:
            tracer_provider = trace.get_tracer_provider()
            if not isinstance(tracer_provider, TracerProvider):
                tracer_provider = TracerProvider()
                trace.set_tracer_provider(tracer_provider)

        processor = AgentEvalsStreamingProcessor(
            ws_url=self.ws_url,
            session_id=session_name,
            trace_id=uuid.uuid4().hex,
        )

        logger_provider = None
        log_processor = None
        if self._should_setup_log_provider():
            try:
                from opentelemetry._logs import get_logger_provider, set_logger_provider
                from opentelemetry.sdk._logs import LoggerProvider

                existing_lp = get_logger_provider()
                if isinstance(existing_lp, LoggerProvider):
                    logger_provider = existing_lp
                else:
                    logger_provider = LoggerProvider()
                    set_logger_provider(logger_provider)

                log_processor = AgentEvalsLogStreamingProcessor(processor)
            except ImportError:
                pass

        if self.auto_instrument:
            self._auto_instrument()

        return _OtelSetup(
            tracer_provider=tracer_provider,
            processor=processor,
            logger_provider=logger_provider,
            log_processor=log_processor,
        )

    def _should_setup_log_provider(self) -> bool:
        """Check whether the OpenAI OTel instrumentor is installed.

        Only the logs-based GenAI semconv pattern (used by
        ``opentelemetry-instrumentation-openai-v2``) requires a
        ``LoggerProvider``.  Strands and ADK emit content via span
        events or native attributes and don't need one.
        """
        try:
            import opentelemetry.instrumentation.openai_v2  # noqa: F401

            return True
        except ImportError:
            return False

    def _auto_instrument(self) -> None:
        """Best-effort discovery and activation of OTel instrumentors.

        Silently skips anything that isn't installed.  Safe to call
        multiple times — OTel instrumentors track their own state and
        ``instrument()`` is idempotent.
        """
        found_instrumentor = False

        try:
            from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

            OpenAIInstrumentor().instrument()
            found_instrumentor = True
        except (ImportError, RuntimeError):
            pass

        try:
            import strands  # noqa: F401

            os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
            found_instrumentor = True
        except ImportError:
            pass

        if not found_instrumentor:
            logger.warning(
                "No OTel instrumentor found. LLM calls won't produce traces. "
                "Install one, e.g.: pip install opentelemetry-instrumentation-openai-v2"
            )

    def _generate_session_id(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"session-{timestamp}-{uuid.uuid4().hex[:6]}"
