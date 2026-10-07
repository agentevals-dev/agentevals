"""Tests for the AgentEvals SDK: session scoped OTLP export, isolated from the application pipeline.

The session exporters are replaced by in memory exporters, so the tests read what agentevals
would receive. The receiver preflight is stubbed except where a failure is under test.
"""

import asyncio
import threading
import warnings
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentevals.otel import sdk_export
from agentevals.sdk import AgentEvals


@pytest.fixture
def exported(monkeypatch):
    """Session exporters write to memory; the receiver preflight always succeeds."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(sdk_export, "_span_exporter", lambda endpoint: exporter)
    monkeypatch.setattr(sdk_export, "preflight", lambda endpoint: None)
    return exporter


def _resource(span) -> dict:
    return dict(span.resource.attributes)


# ---------------------------------------------------------------------------
# Constructor and decorator
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self):
        app = AgentEvals()
        assert app.ws_url == "ws://localhost:8001/ws/traces"
        assert app.endpoint is None
        assert app.eval_set_id is None
        assert app.metadata == {}
        assert app.auto_instrument is True
        assert app.streaming is True

    def test_custom_config(self):
        app = AgentEvals(endpoint="http://other:4318", eval_set_id="e1", metadata={"k": "v"}, auto_instrument=False)
        assert (app.endpoint, app.eval_set_id, app.metadata, app.auto_instrument) == (
            "http://other:4318",
            "e1",
            {"k": "v"},
            False,
        )

    def test_metadata_default_is_not_shared(self):
        a, b = AgentEvals(), AgentEvals()
        a.metadata["x"] = 1
        assert b.metadata == {}


class TestAgentDecorator:
    def test_registers_sync_function(self):
        app = AgentEvals()

        @app.agent
        def fn(prompt):
            return prompt

        assert app._agent_fn is fn
        assert app._is_async is False

    def test_registers_async_function(self):
        app = AgentEvals()

        @app.agent
        async def fn(prompt):
            return prompt

        assert app._is_async is True

    def test_run_without_agent_raises(self):
        with pytest.raises(RuntimeError, match="No agent registered"):
            AgentEvals().run(["hi"])


class TestSessionId:
    def test_format(self):
        assert AgentEvals()._generate_session_id().startswith("session-")

    def test_uniqueness(self):
        app = AgentEvals()
        assert len({app._generate_session_id() for _ in range(50)}) == 50


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


class TestEndpoint:
    def test_explicit_endpoint_wins(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_OTLP_ENDPOINT", "http://env:4318")
        assert sdk_export.resolve_endpoint("http://explicit:4318/", None) == "http://explicit:4318"

    def test_agentevals_env_next(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_OTLP_ENDPOINT", "http://env:4318")
        assert sdk_export.resolve_endpoint(None, None) == "http://env:4318"

    def test_legacy_ws_url_maps_to_otlp_http_with_a_warning(self, monkeypatch):
        monkeypatch.delenv("AGENTEVALS_OTLP_ENDPOINT", raising=False)
        with pytest.warns(DeprecationWarning, match="ws_url is deprecated"):
            assert sdk_export.resolve_endpoint(None, "ws://agent-host:8001/ws/traces") == "http://agent-host:4318"

    def test_default_ws_url_is_not_deprecated(self, monkeypatch):
        monkeypatch.delenv("AGENTEVALS_OTLP_ENDPOINT", raising=False)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert sdk_export.resolve_endpoint(None, sdk_export.LEGACY_WS_URL) == "http://localhost:4318"

    def test_otel_exporter_endpoint_is_never_read(self, monkeypatch):
        monkeypatch.delenv("AGENTEVALS_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://observability.example.com")
        assert sdk_export.resolve_endpoint(None, None) == "http://localhost:4318"


class TestExporterIsolation:
    """The OTLP exporters fill unset options from OTEL_EXPORTER_OTLP_*; none of it may apply."""

    def test_span_exporter_ignores_application_otlp_settings(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer app-token")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-api-key=app-key")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://observability.example.com")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/etc/app/client.key")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "/etc/app/client.crt")
        exporter = sdk_export._span_exporter("http://localhost:4318")
        assert exporter._endpoint == "http://localhost:4318/v1/traces"
        assert exporter._headers == {"x-agentevals-sdk": "1"}
        assert "authorization" not in {k.lower() for k in exporter._session.headers}
        assert exporter._client_key_file is None
        assert exporter._client_certificate_file is None
        assert exporter._session.trust_env is False

    def test_log_exporter_ignores_application_otlp_settings(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer app-token")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-api-key=app-key")
        exporter = sdk_export._log_exporter("http://localhost:4318")
        assert exporter._endpoint == "http://localhost:4318/v1/logs"
        assert exporter._headers == {"x-agentevals-sdk": "1"}
        assert exporter._session.trust_env is False


# ---------------------------------------------------------------------------
# Provider setup
# ---------------------------------------------------------------------------


class TestSetupOtel:
    def test_creates_tracer_provider_when_none_exists(self, exported):
        with patch.object(trace, "get_tracer_provider", return_value=trace.NoOpTracerProvider()):
            with patch.object(trace, "set_tracer_provider") as set_provider:
                setup = AgentEvals(auto_instrument=False)._setup_otel("s1")
        assert isinstance(setup.tracer_provider, TracerProvider)
        set_provider.assert_called_once_with(setup.tracer_provider)

    def test_uses_explicit_tracer_provider(self, exported):
        explicit = TracerProvider()
        setup = AgentEvals(auto_instrument=False)._setup_otel("s1", explicit_tracer_provider=explicit)
        assert setup.tracer_provider is explicit

    def test_reuses_existing_global_tracer_provider(self, exported):
        existing = TracerProvider()
        with patch.object(trace, "get_tracer_provider", return_value=existing):
            setup = AgentEvals(auto_instrument=False)._setup_otel("s1")
        assert setup.tracer_provider is existing

    def test_session_processor_is_registered_once_per_provider(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        first = app._setup_otel("s1", provider)
        second = app._setup_otel("s2", provider)
        assert first.export is second.export
        processors = provider._active_span_processor._span_processors
        assert sum(isinstance(p, sdk_export.SessionSpanProcessor) for p in processors) == 1

    def test_sets_capture_message_content_env_var(self, exported, monkeypatch):
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
        AgentEvals(auto_instrument=False, capture_message_content=True)._setup_otel("s1", TracerProvider())
        import os

        assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "true"

    def test_does_not_override_existing_capture_env_var(self, exported, monkeypatch):
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
        AgentEvals(auto_instrument=False, capture_message_content=True)._setup_otel("s1", TracerProvider())
        import os

        assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"


class TestAutoInstrument:
    def test_does_not_raise_when_nothing_installed(self):
        AgentEvals()._auto_instrument()


# ---------------------------------------------------------------------------
# Sync session
# ---------------------------------------------------------------------------


class TestSyncSession:
    def test_session_spans_carry_the_session_resource(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False, metadata={"a": 1})
        with app.session(eval_set_id="e1", session_name="s1", metadata={"b": "two"}, tracer_provider=provider):
            with provider.get_tracer("t").start_as_current_span("work"):
                pass
        spans = exported.get_finished_spans()
        assert [s.name for s in spans] == ["work"]
        resource = _resource(spans[0])
        assert resource["agentevals.session_name"] == "s1"
        assert resource["agentevals.eval_set_id"] == "e1"
        assert resource["agentevals.metadata.a"] == 1
        assert resource["agentevals.metadata.b"] == "two"
        assert len(resource["agentevals.session.run_id"]) == 32

    def test_eval_set_id_falls_back_to_instance(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False, eval_set_id="from-init")
        with app.session(session_name="s1", tracer_provider=provider):
            provider.get_tracer("t").start_span("work").end()
        assert _resource(exported.get_finished_spans()[0])["agentevals.eval_set_id"] == "from-init"

    def test_each_session_gets_its_own_run_id(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        for _ in range(2):
            with app.session(session_name="same", tracer_provider=provider):
                provider.get_tracer("t").start_span("work").end()
        runs = {_resource(s)["agentevals.session.run_id"] for s in exported.get_finished_spans()}
        assert len(runs) == 2

    def test_spans_outside_a_session_are_not_exported(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        with app.session(session_name="s1", tracer_provider=provider):
            pass
        provider.get_tracer("t").start_span("after").end()
        sdk_export.flush(sdk_export._exports[provider])
        assert exported.get_finished_spans() == ()

    def test_application_exporters_never_see_session_attributes(self, exported):
        provider = TracerProvider()
        own = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(own))
        app = AgentEvals(auto_instrument=False)
        with app.session(session_name="s1", tracer_provider=provider):
            provider.get_tracer("t").start_span("work").end()
        assert [s.name for s in own.get_finished_spans()] == ["work"]
        assert not any(k.startswith("agentevals.") for k in _resource(own.get_finished_spans()[0]))

    def test_spans_on_other_threads_join_the_only_active_session(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        with app.session(session_name="s1", tracer_provider=provider):
            worker = threading.Thread(target=lambda: provider.get_tracer("t").start_span("threaded").end())
            worker.start()
            worker.join()
        assert [_resource(s)["agentevals.session_name"] for s in exported.get_finished_spans()] == ["s1"]

    def test_yields_session_name(self, exported):
        with AgentEvals(auto_instrument=False).session(
            session_name="custom-name", tracer_provider=TracerProvider()
        ) as name:
            assert name == "custom-name"

    def test_generates_session_name_when_omitted(self, exported):
        with AgentEvals(auto_instrument=False).session(tracer_provider=TracerProvider()) as name:
            assert name.startswith("session-")

    def test_unreachable_receiver_raises_with_helpful_message(self, monkeypatch):
        def refuse(endpoint):
            raise ConnectionError("refused")

        monkeypatch.setattr(sdk_export, "preflight", refuse)
        with pytest.raises(ConnectionError, match="agentevals serve --dev"):
            with AgentEvals(auto_instrument=False).session(tracer_provider=TracerProvider()):
                pass

    def test_no_background_thread_is_left_running(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)
        app._setup_otel("warmup", provider)
        before = threading.active_count()
        with app.session(session_name="s1", tracer_provider=provider):
            pass
        assert threading.active_count() == before


# ---------------------------------------------------------------------------
# Async session
# ---------------------------------------------------------------------------


class TestAsyncSession:
    async def test_session_spans_carry_the_session_resource(self, exported):
        provider = TracerProvider()
        async with AgentEvals(auto_instrument=False).session_async(
            eval_set_id="e1", session_name="async-s1", tracer_provider=provider
        ) as name:
            assert name == "async-s1"
            provider.get_tracer("t").start_span("work").end()
        resource = _resource(exported.get_finished_spans()[0])
        assert (resource["agentevals.session_name"], resource["agentevals.eval_set_id"]) == ("async-s1", "e1")

    async def test_concurrent_sessions_stay_apart(self, exported):
        provider = TracerProvider()
        app = AgentEvals(auto_instrument=False)

        async def run(name):
            async with app.session_async(session_name=name, tracer_provider=provider):
                await asyncio.sleep(0.01)
                provider.get_tracer("t").start_span(f"work-{name}").end()

        await asyncio.gather(run("a"), run("b"))
        by_name = {s.name: _resource(s)["agentevals.session_name"] for s in exported.get_finished_spans()}
        assert by_name == {"work-a": "a", "work-b": "b"}

    async def test_unreachable_receiver_raises_with_helpful_message(self, monkeypatch):
        def refuse(endpoint):
            raise ConnectionError("refused")

        monkeypatch.setattr(sdk_export, "preflight", refuse)
        with pytest.raises(ConnectionError, match="agentevals serve --dev"):
            async with AgentEvals(auto_instrument=False).session_async(tracer_provider=TracerProvider()):
                pass


# ---------------------------------------------------------------------------
# streaming=False
# ---------------------------------------------------------------------------


class TestStreamingDisabled:
    def test_sync_session_is_noop(self):
        with patch.object(sdk_export, "install") as install:
            with AgentEvals(streaming=False, auto_instrument=False).session(
                eval_set_id="e1", session_name="s1"
            ) as name:
                assert name == "s1"
        install.assert_not_called()

    def test_sync_session_generates_session_name(self):
        with AgentEvals(streaming=False, auto_instrument=False).session() as name:
            assert name.startswith("session-")

    async def test_async_session_is_noop(self):
        with patch.object(sdk_export, "install") as install:
            async with AgentEvals(streaming=False, auto_instrument=False).session_async(session_name="s1") as name:
                assert name == "s1"
        install.assert_not_called()


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


# ---------------------------------------------------------------------------
# Log export
# ---------------------------------------------------------------------------


class TestLogExport:
    def _providers(self, monkeypatch):
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

        logs = InMemoryLogRecordExporter()
        monkeypatch.setattr(sdk_export, "_log_exporter", lambda endpoint: logs)
        tracer_provider, logger_provider = TracerProvider(), LoggerProvider()
        export = sdk_export.install(tracer_provider, "http://localhost:4318", logger_provider)
        return tracer_provider, logger_provider.get_logger("app"), export, logs

    def _emit(self, otel_logger, text: str) -> None:
        from opentelemetry._logs import LogRecord

        otel_logger.emit(LogRecord(event_name="gen_ai.user.message", body={"content": text}))

    def test_logs_inside_a_session_are_exported(self, exported, monkeypatch):
        tracer_provider, otel_logger, export, logs = self._providers(monkeypatch)
        with AgentEvals(auto_instrument=False).session(session_name="s1", tracer_provider=tracer_provider):
            with tracer_provider.get_tracer("t").start_as_current_span("work"):
                self._emit(otel_logger, "inside")
        sdk_export.flush(export)
        assert [r.log_record.body for r in logs.get_finished_logs()] == [{"content": "inside"}]

    def test_logs_outside_any_session_are_not_exported(self, exported, monkeypatch):
        tracer_provider, otel_logger, export, logs = self._providers(monkeypatch)
        self._emit(otel_logger, "outside")
        sdk_export.flush(export)
        assert logs.get_finished_logs() == ()

    def test_logs_on_other_threads_follow_their_trace(self, exported, monkeypatch):
        from opentelemetry import context as otel_context

        tracer_provider, otel_logger, export, logs = self._providers(monkeypatch)
        with AgentEvals(auto_instrument=False).session(session_name="s1", tracer_provider=tracer_provider):
            with tracer_provider.get_tracer("t").start_as_current_span("work"):
                ctx = otel_context.get_current()

        def worker():
            token = otel_context.attach(ctx)
            try:
                self._emit(otel_logger, "late, other thread")
            finally:
                otel_context.detach(token)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        sdk_export.flush(export)
        assert [r.log_record.body for r in logs.get_finished_logs()] == [{"content": "late, other thread"}]
