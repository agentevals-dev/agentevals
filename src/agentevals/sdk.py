"""High-level SDK for streaming agent traces to the agentevals UI.

Each session exports the spans and GenAI log events produced inside it to the agentevals OTLP
receiver (``agentevals serve``, port 4318), stamped with the session name, eval set id and run
id. The application's own OpenTelemetry exporters are unaffected.

Usage (context manager, the primary API):

    from agentevals import AgentEvals

    app = AgentEvals()

    with app.session(eval_set_id="my-eval"):
        result = my_agent.invoke("Hello!")

Usage (decorator, shorthand for simple agents):

    app = AgentEvals(eval_set_id="my-eval")

    @app.agent
    def my_agent(prompt):
        return llm.invoke(prompt).content

    app.run(["Hello!", "Tell me a joke"])

Disabling streaming:
    Pass ``streaming=False`` to skip all export setup. The context managers become no-ops and
    your agent code runs without any agentevals connection::

        app = AgentEvals(streaming=os.getenv("AGENTEVALS_STREAM", "1") == "1")

Endpoint:
    ``endpoint=`` wins, then ``AGENTEVALS_OTLP_ENDPOINT``, then ``http://localhost:4318``.
    ``OTEL_EXPORTER_OTLP_*`` is never read, so credentials meant for another backend are never
    sent to agentevals.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .otel import sdk_export
from .otel.sdk_export import LEGACY_WS_URL

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

__all__ = ["AgentEvals"]

logger = logging.getLogger(__name__)

_DEFAULT_WS_URL = LEGACY_WS_URL


def _content_capture_default() -> str:
    """The content capture value the instrumentations accept in the current semconv mode.

    In ``gen_ai_latest_experimental`` mode util-genai based instrumentations only accept a mode
    name and fall back to no content for ``true``. In the default mode openai-v2 only accepts
    ``true``. ``SPAN_ONLY`` puts content where agentevals reads it first, without a duplicate
    copy in log events."""
    opt_in = os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN", "")
    if "gen_ai_latest_experimental" in {value.strip() for value in opt_in.split(",")}:
        return "SPAN_ONLY"
    return "true"


@dataclass(slots=True)
class _OtelSetup:
    tracer_provider: SdkTracerProvider
    export: Any
    logger_provider: LoggerProvider | None = field(default=None)


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
        endpoint: str | None = None,
    ):
        self.ws_url = ws_url
        self.endpoint = endpoint
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
        """Sync context manager that exports the spans and GenAI events produced inside it.

        Args:
            eval_set_id: Evaluation set ID for matching against a golden session.
            session_name: Custom session name (auto-generated if omitted).
            metadata: Custom metadata shown with the session.
            tracer_provider: Explicit TracerProvider to use (e.g. from StrandsTelemetry).
                Falls back to the global provider, then creates a new one.
        """
        eff_session_name = session_name or self._generate_session_id()
        if not self.streaming:
            logger.debug("Streaming disabled, running without agentevals connection")
            yield eff_session_name
            return

        context, setup = self._open(eff_session_name, eval_set_id, metadata, tracer_provider)
        token = sdk_export.enter(context)
        try:
            yield eff_session_name
        finally:
            sdk_export.leave(context, token)
            sdk_export.flush(setup.export)

    @asynccontextmanager
    async def session_async(
        self,
        eval_set_id: str | None = None,
        session_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tracer_provider: SdkTracerProvider | None = None,
    ):
        """Async context manager that exports the spans and GenAI events produced inside it.

        Args:
            eval_set_id: Evaluation set ID for matching against a golden session.
            session_name: Custom session name (auto-generated if omitted).
            metadata: Custom metadata shown with the session.
            tracer_provider: Explicit TracerProvider to use. Falls back to the global
                provider, then creates a new one.
        """
        eff_session_name = session_name or self._generate_session_id()
        if not self.streaming:
            logger.debug("Streaming disabled, running without agentevals connection")
            yield eff_session_name
            return

        context, setup = await asyncio.to_thread(self._open, eff_session_name, eval_set_id, metadata, tracer_provider)
        token = sdk_export.enter(context)
        try:
            yield eff_session_name
        finally:
            sdk_export.leave(context, token)
            await asyncio.to_thread(sdk_export.flush, setup.export)

    def _open(
        self,
        session_name: str,
        eval_set_id: str | None,
        metadata: dict[str, Any] | None,
        tracer_provider: SdkTracerProvider | None,
    ) -> tuple[sdk_export.SessionContext, _OtelSetup]:
        endpoint = sdk_export.resolve_endpoint(self.endpoint, self.ws_url)
        try:
            sdk_export.preflight(endpoint)
        except ConnectionError as exc:
            raise ConnectionError(
                f"[agentevals] Could not reach the OTLP receiver at {endpoint}. "
                f"Is 'agentevals serve --dev' running?\n  {exc}"
            ) from exc
        setup = self._setup_otel(session_name, tracer_provider, endpoint=endpoint)
        context = sdk_export.SessionContext(
            name=session_name,
            run_id=uuid.uuid4().hex,
            eval_set_id=eval_set_id or self.eval_set_id,
            metadata={**self.metadata, **(metadata or {})},
        )
        logger.info("Streaming to %s (session: %s)", endpoint, session_name)
        return context, setup

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
        *,
        endpoint: str | None = None,
    ) -> _OtelSetup:
        """Resolve the providers and register the session exporters on them (once per provider).

        Provider resolution order:
        1. ``explicit_tracer_provider`` if given
        2. Existing global ``TracerProvider`` (e.g. set by StrandsTelemetry)
        3. New ``TracerProvider`` created and set globally

        The global SDK ``LoggerProvider`` is reused, or created and set, so GenAI events emitted
        as log records (OpenAI v2, util-genai based instrumentations) reach agentevals too.
        """
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        if self.capture_message_content:
            os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", _content_capture_default())

        if explicit_tracer_provider is not None:
            tracer_provider = explicit_tracer_provider
        else:
            tracer_provider = trace.get_tracer_provider()
            if not isinstance(tracer_provider, TracerProvider):
                tracer_provider = TracerProvider()
                trace.set_tracer_provider(tracer_provider)

        logger_provider = None
        try:
            from opentelemetry._logs import get_logger_provider, set_logger_provider
            from opentelemetry.sdk._logs import LoggerProvider

            existing = get_logger_provider()
            if isinstance(existing, LoggerProvider):
                logger_provider = existing
            else:
                logger_provider = LoggerProvider()
                set_logger_provider(logger_provider)
        except ImportError:
            pass

        export = sdk_export.install(
            tracer_provider,
            endpoint or sdk_export.resolve_endpoint(self.endpoint, self.ws_url),
            logger_provider,
        )

        if self.auto_instrument:
            self._auto_instrument()

        return _OtelSetup(tracer_provider=tracer_provider, export=export, logger_provider=logger_provider)

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
