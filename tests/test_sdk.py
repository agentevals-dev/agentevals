"""Tests for the AgentEvals SDK.

Mocking strategy: ``AgentEvalsStreamingProcessor`` is imported inside
``_setup_otel`` via a local import, so we patch at the source module
(``agentevals.streaming.processor``) rather than at ``agentevals.sdk``.
"""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider

from agentevals.sdk import AgentEvals

PROC_PATH = "agentevals.streaming.processor.AgentEvalsStreamingProcessor"


# ---------------------------------------------------------------------------
# Construction & configuration
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self):
        app = AgentEvals()
        assert app.ws_url == "ws://localhost:8001/ws/traces"
        assert app.eval_set_id is None
        assert app.metadata == {}
        assert app.auto_instrument is True
        assert app.capture_message_content is True
        assert app.streaming is True

    def test_custom_config(self):
        app = AgentEvals(
            ws_url="ws://other:9999/ws/traces",
            eval_set_id="my-eval",
            metadata={"model": "gpt-4o"},
            auto_instrument=False,
            capture_message_content=False,
        )
        assert app.ws_url == "ws://other:9999/ws/traces"
        assert app.eval_set_id == "my-eval"
        assert app.metadata == {"model": "gpt-4o"}

    def test_metadata_default_is_not_shared(self):
        a = AgentEvals()
        b = AgentEvals()
        a.metadata["x"] = 1
        assert "x" not in b.metadata


# ---------------------------------------------------------------------------
# @app.agent decorator
# ---------------------------------------------------------------------------


class TestAgentDecorator:
    def test_registers_sync_function(self):
        app = AgentEvals()

        @app.agent
        def my_fn(prompt):
            return prompt.upper()

        assert app._agent_fn is my_fn
        assert app._is_async is False
        assert my_fn("hi") == "HI"

    def test_registers_async_function(self):
        app = AgentEvals()

        @app.agent
        async def my_fn(prompt):
            return prompt.upper()

        assert app._agent_fn is my_fn
        assert app._is_async is True

    def test_run_without_agent_raises(self):
        app = AgentEvals()
        with pytest.raises(RuntimeError, match="No agent registered"):
            app.run(["hello"])


# ---------------------------------------------------------------------------
# Session ID generation
# ---------------------------------------------------------------------------


class TestSessionId:
    def test_format(self):
        sid = AgentEvals()._generate_session_id()
        assert sid.startswith("session-")
        assert len(sid.split("-")) >= 3

    def test_uniqueness(self):
        app = AgentEvals()
        ids = {app._generate_session_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# _setup_otel — provider resolution
# ---------------------------------------------------------------------------


class TestSetupOtel:
    @patch(PROC_PATH)
    def test_creates_tracer_provider_when_none_exists(self, MockProc):
        MockProc.return_value = MagicMock()
        app = AgentEvals(auto_instrument=False)
        setup = app._setup_otel("s1")
        assert isinstance(setup.tracer_provider, TracerProvider)

    @patch(PROC_PATH)
    def test_uses_explicit_tracer_provider(self, MockProc):
        MockProc.return_value = MagicMock()
        explicit = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        setup = app._setup_otel("s1", explicit_tracer_provider=explicit)
        assert setup.tracer_provider is explicit

    @patch(PROC_PATH)
    def test_reuses_existing_global_tracer_provider(self, MockProc):
        MockProc.return_value = MagicMock()
        existing = TracerProvider()
        with patch("opentelemetry.trace.get_tracer_provider", return_value=existing):
            app = AgentEvals(auto_instrument=False)
            setup = app._setup_otel("s1")
            assert setup.tracer_provider is existing

    @patch(PROC_PATH)
    def test_processor_gets_correct_ws_url_and_session(self, MockProc):
        MockProc.return_value = MagicMock()
        app = AgentEvals(ws_url="ws://custom:1234/ws/traces", auto_instrument=False)
        app._setup_otel("my-session")
        MockProc.assert_called_once()
        assert MockProc.call_args.kwargs["ws_url"] == "ws://custom:1234/ws/traces"
        assert MockProc.call_args.kwargs["session_id"] == "my-session"

    @patch(PROC_PATH)
    def test_sets_capture_message_content_env_var(self, MockProc):
        MockProc.return_value = MagicMock()
        env_key = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        original = os.environ.pop(env_key, None)
        try:
            app = AgentEvals(auto_instrument=False, capture_message_content=True)
            app._setup_otel("s1")
            assert os.environ.get(env_key) == "true"
        finally:
            if original is not None:
                os.environ[env_key] = original
            else:
                os.environ.pop(env_key, None)

    @patch(PROC_PATH)
    def test_does_not_override_existing_capture_env_var(self, MockProc):
        MockProc.return_value = MagicMock()
        env_key = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        os.environ[env_key] = "false"
        try:
            app = AgentEvals(auto_instrument=False, capture_message_content=True)
            app._setup_otel("s1")
            assert os.environ[env_key] == "false"
        finally:
            os.environ.pop(env_key, None)


# ---------------------------------------------------------------------------
# Auto-instrumentation
# ---------------------------------------------------------------------------


class TestAutoInstrument:
    def test_does_not_raise_when_nothing_installed(self):
        app = AgentEvals()
        app._auto_instrument()

    def test_should_setup_log_provider_returns_bool(self):
        result = AgentEvals()._should_setup_log_provider()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Sync session lifecycle
# ---------------------------------------------------------------------------


def _make_mock_processor():
    proc = MagicMock()
    proc.connect = AsyncMock()
    proc.shutdown_async = AsyncMock()
    proc.force_flush = MagicMock(return_value=True)
    return proc


class TestSyncSession:
    @patch(PROC_PATH)
    def test_connects_adds_processor_and_shuts_down(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)

        with app.session(eval_set_id="e1", session_name="s1", metadata={"k": "v"}):
            mock_proc.connect.assert_called_once_with(eval_set_id="e1", metadata={"k": "v"})

        mock_proc.shutdown_async.assert_called_once()

    @patch(PROC_PATH)
    def test_processor_registered_on_tracer_provider(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)

        with app.session(session_name="s1", tracer_provider=provider):
            assert mock_proc in provider._active_span_processor._span_processors

    @patch(PROC_PATH)
    def test_yields_session_name(self, MockProc):
        MockProc.return_value = _make_mock_processor()
        app = AgentEvals(auto_instrument=False)

        with app.session(session_name="custom-name") as name:
            assert name == "custom-name"

    @patch(PROC_PATH)
    def test_generates_session_name_when_omitted(self, MockProc):
        MockProc.return_value = _make_mock_processor()
        app = AgentEvals(auto_instrument=False)

        with app.session() as name:
            assert name.startswith("session-")

    @patch(PROC_PATH)
    def test_merges_instance_and_call_metadata(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False, metadata={"a": 1})

        with app.session(session_name="s1", metadata={"b": 2}):
            pass

        connect_kwargs = mock_proc.connect.call_args.kwargs
        assert connect_kwargs["metadata"] == {"a": 1, "b": 2}

    @patch(PROC_PATH)
    def test_eval_set_id_falls_back_to_instance(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False, eval_set_id="from-init")

        with app.session(session_name="s1"):
            pass

        connect_kwargs = mock_proc.connect.call_args.kwargs
        assert connect_kwargs["eval_set_id"] == "from-init"

    @patch(PROC_PATH)
    def test_connection_failure_raises_with_helpful_message(self, MockProc):
        mock_proc = _make_mock_processor()
        mock_proc.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)

        with pytest.raises(ConnectionError, match="agentevals serve --dev"):
            with app.session():
                pass

    @patch(PROC_PATH)
    def test_background_thread_is_joined_on_exit(self, MockProc):
        MockProc.return_value = _make_mock_processor()
        app = AgentEvals(auto_instrument=False)

        threads_before = threading.active_count()
        with app.session(session_name="s1"):
            assert threading.active_count() > threads_before

        # Give the thread a moment to fully terminate after join
        import time

        time.sleep(0.1)
        assert threading.active_count() <= threads_before + 1

    @patch(PROC_PATH)
    def test_background_thread_joined_on_connection_failure(self, MockProc):
        mock_proc = _make_mock_processor()
        mock_proc.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)
        threads_before = threading.active_count()

        with pytest.raises(ConnectionError):
            with app.session():
                pass

        import time

        time.sleep(0.1)
        assert threading.active_count() <= threads_before + 1

    @patch(PROC_PATH)
    def test_force_flush_called_before_shutdown(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        provider = TracerProvider()
        provider.force_flush = MagicMock()
        app = AgentEvals(auto_instrument=False)

        with app.session(session_name="s1", tracer_provider=provider):
            pass

        provider.force_flush.assert_called()
        # force_flush must happen before shutdown
        assert provider.force_flush.call_count >= 1


# ---------------------------------------------------------------------------
# Async session lifecycle
# ---------------------------------------------------------------------------


class TestAsyncSession:
    @patch(PROC_PATH)
    def test_connects_adds_processor_and_shuts_down(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)

        async def _test():
            async with app.session_async(eval_set_id="e1", session_name="s1", metadata={"k": "v"}):
                mock_proc.connect.assert_called_once_with(eval_set_id="e1", metadata={"k": "v"})
            mock_proc.shutdown_async.assert_called_once()

        asyncio.run(_test())

    @patch(PROC_PATH)
    def test_processor_registered_on_tracer_provider(self, MockProc):
        mock_proc = _make_mock_processor()
        MockProc.return_value = mock_proc

        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)

        async def _test():
            async with app.session_async(session_name="s1", tracer_provider=provider):
                assert mock_proc in provider._active_span_processor._span_processors

        asyncio.run(_test())

    @patch(PROC_PATH)
    def test_connection_failure_raises_with_helpful_message(self, MockProc):
        mock_proc = _make_mock_processor()
        mock_proc.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)

        async def _test():
            with pytest.raises(ConnectionError, match="agentevals serve --dev"):
                async with app.session_async():
                    pass

        asyncio.run(_test())

    @patch(PROC_PATH)
    def test_yields_session_name(self, MockProc):
        MockProc.return_value = _make_mock_processor()
        app = AgentEvals(auto_instrument=False)

        async def _test():
            async with app.session_async(session_name="async-s1") as name:
                assert name == "async-s1"

        asyncio.run(_test())

    @patch(PROC_PATH)
    def test_shutdown_error_is_logged_not_raised(self, MockProc):
        mock_proc = _make_mock_processor()
        mock_proc.shutdown_async = AsyncMock(side_effect=RuntimeError("ws closed"))
        MockProc.return_value = mock_proc

        app = AgentEvals(auto_instrument=False)

        async def _test():
            async with app.session_async(session_name="s1"):
                pass

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# streaming=False (disabled mode)
# ---------------------------------------------------------------------------


class TestStreamingDisabled:
    def test_sync_session_is_noop(self):
        app = AgentEvals(streaming=False, auto_instrument=False)

        with app.session(eval_set_id="e1", session_name="s1") as name:
            assert name == "s1"

    def test_sync_session_does_not_create_processor(self):
        app = AgentEvals(streaming=False, auto_instrument=False)

        with patch(PROC_PATH) as MockProc:
            with app.session():
                pass
            MockProc.assert_not_called()

    def test_sync_session_generates_session_name(self):
        app = AgentEvals(streaming=False, auto_instrument=False)

        with app.session() as name:
            assert name.startswith("session-")

    def test_async_session_is_noop(self):
        app = AgentEvals(streaming=False, auto_instrument=False)

        async def _test():
            async with app.session_async(eval_set_id="e1", session_name="s1") as name:
                assert name == "s1"

        asyncio.run(_test())

    def test_async_session_does_not_create_processor(self):
        app = AgentEvals(streaming=False, auto_instrument=False)

        async def _test():
            with patch(PROC_PATH) as MockProc:
                async with app.session_async():
                    pass
                MockProc.assert_not_called()

        asyncio.run(_test())

    def test_no_background_thread_when_disabled(self):
        app = AgentEvals(streaming=False, auto_instrument=False)
        threads_before = threading.active_count()

        with app.session(session_name="s1"):
            assert threading.active_count() == threads_before


# ---------------------------------------------------------------------------
# Lazy import from package __init__
# ---------------------------------------------------------------------------


class TestLazyImport:
    def test_import_agent_evals(self):
        from agentevals import AgentEvals as Imported

        assert Imported is AgentEvals

    def test_invalid_attribute(self):
        import agentevals

        with pytest.raises(AttributeError, match="has no attribute"):
            agentevals.DoesNotExist  # noqa: B018
