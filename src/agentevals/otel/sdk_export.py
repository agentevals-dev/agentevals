"""OTLP export of SDK sessions to the agentevals receiver, isolated from the application's pipeline.

Sessions run one after another on a shared ``TracerProvider`` whose resource is fixed, so the
session cannot live on the provider resource. Instead one :class:`SessionSpanProcessor` per
provider decides which session a span belongs to when it starts, and when it ends forwards a copy
whose resource carries the session identity (``agentevals.session_name``,
``agentevals.eval_set_id``, ``agentevals.session.run_id``, ``agentevals.metadata.*``) to its own
OTLP exporter. Spans outside every session are not exported, and the application's own
exporters never see these attributes. Baggage is not used, because it would propagate into
outbound requests.

The exporters are built with an explicit endpoint, explicit headers, an explicit session and
gzip, and never read ``OTEL_EXPORTER_OTLP_*``: the OTLP exporters fall back to those variables for
anything left unset, which would send the application's backend credentials to agentevals.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import os
import threading
import warnings
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry.sdk.trace import SpanProcessor

from .identity import EVAL_SET_ID, METADATA_PREFIX, SESSION_NAME, SESSION_RUN_ID

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:4318"
ENDPOINT_ENV = "AGENTEVALS_OTLP_ENDPOINT"
LEGACY_WS_URL = "ws://localhost:8001/ws/traces"
TRACE_MAP_SIZE = 10_000
FLUSH_TIMEOUT_MILLIS = 5_000
PREFLIGHT_TIMEOUT_SECONDS = 2.0
EXPORT_TIMEOUT_SECONDS = 10.0
_SDK_HEADER = {"x-agentevals-sdk": "1"}


@dataclass(frozen=True, slots=True)
class SessionContext:
    name: str
    run_id: str
    eval_set_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def resource_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {SESSION_NAME: self.name, SESSION_RUN_ID: self.run_id}
        if self.eval_set_id:
            attrs[EVAL_SET_ID] = self.eval_set_id
        for key, value in self.metadata.items():
            attrs[f"{METADATA_PREFIX}{key}"] = _attribute_value(value)
        return attrs


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, (str, bool, int, float)) for v in value):
        return list(value)
    return str(value)


_current: contextvars.ContextVar[SessionContext | None] = contextvars.ContextVar("agentevals_session", default=None)


class _Registry:
    """Active sessions and the trace to session map, shared by every processor in the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, SessionContext] = {}
        self._traces: OrderedDict[int, SessionContext] = OrderedDict()

    def activate(self, session: SessionContext) -> None:
        with self._lock:
            self._active[session.run_id] = session

    def deactivate(self, session: SessionContext) -> None:
        with self._lock:
            self._active.pop(session.run_id, None)

    def resolve(self, trace_id: int) -> SessionContext | None:
        """The contextvar session, else the trace's session, else the only active session."""
        session = _current.get()
        with self._lock:
            if session is None:
                session = self._traces.get(trace_id)
            if session is None and len(self._active) == 1:
                session = next(iter(self._active.values()))
            if session is not None:
                self._traces[trace_id] = session
                self._traces.move_to_end(trace_id)
                while len(self._traces) > TRACE_MAP_SIZE:
                    self._traces.popitem(last=False)
            return session

    def of_trace(self, trace_id: int) -> SessionContext | None:
        with self._lock:
            return self._traces.get(trace_id)


registry = _Registry()


def resolve_endpoint(endpoint: str | None = None, ws_url: str | None = None) -> str:
    """``endpoint``, then ``AGENTEVALS_OTLP_ENDPOINT``, then a non default ``ws_url`` mapped to
    OTLP/HTTP on port 4318 (deprecated), then ``http://localhost:4318``."""
    if endpoint:
        return endpoint.rstrip("/")
    from_env = os.environ.get(ENDPOINT_ENV)
    if from_env:
        return from_env.rstrip("/")
    if ws_url and ws_url != LEGACY_WS_URL:
        warnings.warn(
            "ws_url is deprecated: agentevals now receives OTLP. Pass endpoint='http://host:4318' instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        parts = urlsplit(ws_url)
        scheme = "https" if parts.scheme == "wss" else "http"
        return urlunsplit((scheme, f"{parts.hostname or 'localhost'}:4318", "", "", ""))
    return DEFAULT_ENDPOINT


def _isolated(exporter: Any) -> Any:
    """Never present the application's TLS client identity, which the exporter reads from the
    environment when it is not passed explicitly."""
    for attr in ("_client_key_file", "_client_certificate_file"):
        if hasattr(exporter, attr):
            setattr(exporter, attr, None)
    return exporter


def _http_session() -> Any:
    """A requests session that ignores ``.netrc`` and proxy variables, so nothing configured for
    other hosts is applied to the agentevals endpoint."""
    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _span_exporter(endpoint: str) -> Any:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return _isolated(
        OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces",
            headers=dict(_SDK_HEADER),
            timeout=EXPORT_TIMEOUT_SECONDS,
            compression=Compression.Gzip,
            session=_http_session(),
        )
    )


def _log_exporter(endpoint: str) -> Any:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    return _isolated(
        OTLPLogExporter(
            endpoint=f"{endpoint}/v1/logs",
            headers=dict(_SDK_HEADER),
            timeout=EXPORT_TIMEOUT_SECONDS,
            compression=Compression.Gzip,
            session=_http_session(),
        )
    )


def preflight(endpoint: str) -> None:
    """POST an empty trace export; raise ``ConnectionError`` unless the receiver answers 200."""
    import requests

    try:
        response = _http_session().post(
            f"{endpoint}/v1/traces",
            data=b"",
            headers={"Content-Type": "application/x-protobuf", **_SDK_HEADER},
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ConnectionError(str(exc)) from exc
    if response.status_code != 200:
        raise ConnectionError(f"OTLP receiver at {endpoint} answered {response.status_code}")


class SessionSpanProcessor(SpanProcessor):
    """Assigns spans to sessions and exports a session stamped copy of each."""

    def __init__(self, endpoint: str, exporter: Any | None = None):
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.endpoint = endpoint
        self._batch = BatchSpanProcessor(exporter or _span_exporter(endpoint))
        self._resources: dict[tuple[int, str], Any] = {}
        self._lock = threading.Lock()

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        context = span.get_span_context()
        if context is not None:
            registry.resolve(context.trace_id)

    def _resource(self, base: Any, session: SessionContext) -> Any:
        from opentelemetry.sdk.resources import Resource

        key = (id(base), session.run_id)
        with self._lock:
            cached = self._resources.get(key)
            if cached is None:
                cached = (base or Resource.get_empty()).merge(Resource(session.resource_attributes()))
                if len(self._resources) > 256:
                    self._resources.clear()
                self._resources[key] = cached
            return cached

    def on_end(self, span: Any) -> None:
        context = span.get_span_context()
        session = registry.of_trace(context.trace_id) if context is not None else None
        if session is None:
            return
        from opentelemetry.sdk.trace import ReadableSpan

        self._batch.on_end(
            ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=self._resource(span.resource, session),
                attributes=span.attributes,
                events=span.events,
                links=span.links,
                kind=span.kind,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )
        )

    def force_flush(self, timeout_millis: int = FLUSH_TIMEOUT_MILLIS) -> bool:
        return self._batch.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._batch.shutdown()


class SessionLogProcessor(LogRecordProcessor):
    """Exports log records that belong to a session (by contextvar or trace id) to agentevals."""

    def __init__(self, endpoint: str, exporter: Any | None = None):
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        self._batch = BatchLogRecordProcessor(exporter or _log_exporter(endpoint))

    @staticmethod
    def _trace_id(record: Any) -> int | None:
        inner = getattr(record, "log_record", record)
        trace_id = getattr(inner, "trace_id", None)
        return trace_id if isinstance(trace_id, int) and trace_id else None

    def on_emit(self, record: Any) -> None:
        trace_id = self._trace_id(record)
        if _current.get() is None and (trace_id is None or registry.of_trace(trace_id) is None):
            return
        self._batch.on_emit(record)

    def emit(self, record: Any) -> None:
        self.on_emit(record)

    def force_flush(self, timeout_millis: int = FLUSH_TIMEOUT_MILLIS) -> bool:
        return self._batch.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._batch.shutdown()


@dataclass(slots=True)
class _Export:
    endpoint: str
    span_processor: SessionSpanProcessor
    log_processor: SessionLogProcessor | None


_exports: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_exports_lock = threading.Lock()
_shutdown_registered = False


def _shutdown_all() -> None:
    for export in list(_exports.values()):
        try:
            export.span_processor.shutdown()
            if export.log_processor is not None:
                export.log_processor.shutdown()
        except Exception:
            logger.debug("agentevals exporter shutdown failed", exc_info=True)


def install(tracer_provider: Any, endpoint: str, logger_provider: Any | None = None) -> _Export:
    """Register the session processors on ``tracer_provider`` (once per provider and endpoint)."""
    global _shutdown_registered
    with _exports_lock:
        export = _exports.get(tracer_provider)
        if export is not None and export.endpoint == endpoint:
            if export.log_processor is None and logger_provider is not None:
                export.log_processor = SessionLogProcessor(endpoint)
                logger_provider.add_log_record_processor(export.log_processor)
            return export
        span_processor = SessionSpanProcessor(endpoint)
        tracer_provider.add_span_processor(span_processor)
        log_processor = None
        if logger_provider is not None:
            log_processor = SessionLogProcessor(endpoint)
            logger_provider.add_log_record_processor(log_processor)
        export = _Export(endpoint, span_processor, log_processor)
        _exports[tracer_provider] = export
        if not _shutdown_registered:
            atexit.register(_shutdown_all)
            _shutdown_registered = True
        return export


def flush(export: _Export) -> None:
    export.span_processor.force_flush(FLUSH_TIMEOUT_MILLIS)
    if export.log_processor is not None:
        export.log_processor.force_flush(FLUSH_TIMEOUT_MILLIS)


def enter(session: SessionContext) -> contextvars.Token:
    registry.activate(session)
    return _current.set(session)


def leave(session: SessionContext, token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        _current.set(None)
    registry.deactivate(session)
