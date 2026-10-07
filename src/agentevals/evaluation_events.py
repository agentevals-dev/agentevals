"""Evaluation results as OpenTelemetry ``gen_ai.evaluation.result`` log events.

Each record is parented to the GenAI span it judges, so a backend shows the score next to the
operation, and carries no content: no user input, model output or tool arguments. Metrics with
per turn scores give one record per turn; conversation level metrics give one record on the last
turn. Labels are ``pass``, ``fail`` and ``not_evaluated``; an evaluator error gives ``error.type``
and no score or label.

Off unless turned on, because it sends results to an external backend. Precedence:

1. ``OTEL_SDK_DISABLED=true`` or ``OTEL_LOGS_EXPORTER=none`` turns it off (the standard switches win);
2. otherwise ``--emit-otel`` (``run``, ``serve``) or ``AGENTEVALS_EVALUATION_EVENTS=true`` turns it on;
3. otherwise it stays off.

``AGENTEVALS_EVALUATION_EVENTS`` and ``AGENTEVALS_EVALUATION_EVENTS_EXPLANATION`` accept
true/false/1/0/yes/no/on/off; anything else is an error. The exporter is configured only by the
standard ``OTEL_EXPORTER_OTLP_*`` variables (logs specific ones first).
A dedicated ``LoggerProvider`` is used, never the global one, and this is the only module that
touches ``opentelemetry._logs`` for emitting.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .otel.identity import EMITTER_SCOPE
from .otel.model import Span, attr_str

if TYPE_CHECKING:
    from .config import EvaluatorDef
    from .genai.model import Conversation, Turn
    from .runner import MetricResult

logger = logging.getLogger(__name__)

EVENT_NAME = "gen_ai.evaluation.result"
SCOPE_NAME = EMITTER_SCOPE
ENABLE_ENV = "AGENTEVALS_EVALUATION_EVENTS"
EXPLANATION_ENV = "AGENTEVALS_EVALUATION_EVENTS_EXPLANATION"
MAX_EXPLANATION_CHARS = 2048
FAILURE_LOG_INTERVAL_SECONDS = 60.0

LABEL_PASS = "pass"
LABEL_FAIL = "fail"
LABEL_NOT_EVALUATED = "not_evaluated"
_LABELS = {"PASSED": LABEL_PASS, "FAILED": LABEL_FAIL, "NOT_EVALUATED": LABEL_NOT_EVALUATED}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_PROCESS_INSTANCE_ID = str(uuid.uuid4())


def _env_true(name: str) -> bool:
    """Lenient read for standard OTel switches, which other tools also set."""
    return os.environ.get(name, "").strip().lower() in _TRUE


def _env_flag(name: str) -> bool | None:
    """Strict read for agentevals switches: ``None`` when unset, ``ValueError`` when unrecognized."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be one of true/false/1/0/yes/no/on/off (got: {raw!r})")


def disabled_by_otel() -> bool:
    """The standard switches that silence telemetry for the whole process."""
    return _env_true("OTEL_SDK_DISABLED") or os.environ.get("OTEL_LOGS_EXPORTER", "").strip().lower() == "none"


def validate_env() -> None:
    """Fail fast on an unrecognized switch value (called when the CLI and the server start)."""
    _env_flag(ENABLE_ENV)
    _env_flag(EXPLANATION_ENV)


def enabled() -> bool:
    if disabled_by_otel():
        return False
    return _requested or bool(_env_flag(ENABLE_ENV))


def _package_version() -> str:
    from . import __version__

    return __version__


def build_resource() -> Any:
    """Service identity as one unit; ``OTEL_RESOURCE_ATTRIBUTES`` and ``OTEL_SERVICE_NAME`` win."""
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    ours = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or "agentevals",
            "service.version": _package_version(),
            "service.instance.id": _PROCESS_INSTANCE_ID,
        }
    )
    return ours.merge(OTELResourceDetector().detect())


def instance_id() -> str:
    """The ``service.instance.id`` this process emits with (used by the live feedback guard)."""
    value = build_resource().attributes.get("service.instance.id")
    return str(value) if value else _PROCESS_INSTANCE_ID


def _protocol() -> str:
    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL") or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or ""
    ).strip()
    if raw in ("", "http/protobuf"):
        return "http/protobuf"
    if raw == "grpc":
        return "grpc"
    logger.warning("OTLP protocol %r is not supported for evaluation events; using http/protobuf", raw)
    return "http/protobuf"


def _exporter_from_env() -> Any:
    if _protocol() == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    return OTLPLogExporter()


def _evaluator_type(evaluator: EvaluatorDef | None) -> str | None:
    if evaluator is None or evaluator.type != "builtin":
        return None
    from .builtin_metrics import METRICS_NEEDING_GCP, METRICS_NEEDING_LLM

    return "llm_judge" if evaluator.name in METRICS_NEEDING_LLM | METRICS_NEEDING_GCP else "deterministic"


def _explanation(metric: MetricResult) -> str | None:
    details = metric.details or {}
    for key in ("explanation", "rationale", "reason"):
        value = details.get(key)
        if isinstance(value, str) and value:
            return value[:MAX_EXPLANATION_CHARS]
    return None


def _evaluated_span(turn: Turn) -> tuple[str, str]:
    """The GenAI span a turn's results attach to: its agent or workflow anchor, else its final call.

    A root anchored turn's anchor can be a non GenAI span (an HTTP server span, say); the event
    should hang off the GenAI operation being evaluated.
    """
    calls = turn.llm_calls
    if turn.anchor_kind != "root" or not calls:
        return turn.ref.trace_id, turn.ref.span_id
    call_refs = {(c.ref.trace_id, c.ref.span_id) for c in calls}
    call_refs |= {(c.wrapper_ref.trace_id, c.wrapper_ref.span_id) for c in calls if c.wrapper_ref}
    if (turn.ref.trace_id, turn.ref.span_id) in call_refs:
        return turn.ref.trace_id, turn.ref.span_id
    final = max(calls, key=lambda c: (c.end_ns, c.start_ns))
    return final.ref.trace_id, final.ref.span_id


class EvaluationEmitter:
    def __init__(self, exporter: Any | None = None, *, explanation: bool | None = None):
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        self.resource = build_resource()
        self.explanation = bool(_env_flag(EXPLANATION_ENV)) if explanation is None else explanation
        self._provider = LoggerProvider(resource=self.resource)
        self._provider.add_log_record_processor(BatchLogRecordProcessor(exporter or _exporter_from_env()))
        self._logger = self._provider.get_logger(SCOPE_NAME, _package_version())
        self._last_failure_log: float | None = None
        self._lock = threading.Lock()

    def _record(self, span: tuple[str, str], attributes: Mapping[str, Any]) -> None:
        """Emit one record in the evaluated span's context.

        The record is always marked sampled: the span reached us, and SDK pipelines export only
        sampled spans. The OTLP span ``flags`` cannot tell otherwise, since common encoders write
        only the parent is remote bits there and leave the W3C trace flags at zero.
        """
        from opentelemetry._logs import LogRecord, SeverityNumber
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

        trace_flags = TraceFlags(TraceFlags.SAMPLED)
        context = set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=int(span[0], 16), span_id=int(span[1], 16), is_remote=True, trace_flags=trace_flags
                )
            )
        )
        now = time.time_ns()
        self._logger.emit(
            LogRecord(
                timestamp=now,
                observed_timestamp=now,
                context=context,
                severity_text="INFO",
                severity_number=SeverityNumber.INFO,
                body=None,
                attributes=dict(attributes),
                event_name=EVENT_NAME,
            )
        )

    def emit(
        self,
        *,
        conversation: Conversation,
        metrics: Sequence[MetricResult],
        evaluators: Iterable[EvaluatorDef],
        spans: Mapping[tuple[str, str], Span],
        eval_case_id: str | None = None,
        eval_set_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        """Emit the records for one evaluated group. Returns how many were emitted; never raises."""
        try:
            return self._emit(conversation, metrics, evaluators, spans, eval_case_id, eval_set_id, run_id)
        except Exception:
            self._log_failure("Could not emit evaluation result events")
            return 0

    def _emit(self, conversation, metrics, evaluators, spans, eval_case_id, eval_set_id, run_id) -> int:
        turns = conversation.turns
        if not turns:
            return 0
        by_name = {e.name: e for e in evaluators}
        common: dict[str, Any] = {}
        for key, value in (
            ("agentevals.eval.run.id", run_id),
            ("agentevals.eval.case.id", eval_case_id),
            ("agentevals.eval_set.id", eval_set_id),
        ):
            if value:
                common[key] = value

        emitted = 0
        for metric in metrics:
            if metric.metric_name not in by_name:
                continue
            base = {"gen_ai.evaluation.name": metric.metric_name, **common}
            evaluator_type = _evaluator_type(by_name.get(metric.metric_name))
            if evaluator_type:
                base["agentevals.evaluator.type"] = evaluator_type
            explanation = _explanation(metric) if self.explanation else None

            per_turn = metric.per_invocation_scores
            if metric.error:
                items = [(turns[-1], None, None)]
            elif per_turn and len(per_turn) <= len(turns):
                statuses = metric.per_invocation_statuses
                items = [
                    (turns[i], score, statuses[i] if i < len(statuses) else None) for i, score in enumerate(per_turn)
                ]
            else:
                items = [(turns[-1], metric.score, metric.eval_status)]

            for turn, score, status in items:
                ref = _evaluated_span(turn)
                span = spans.get(ref)
                attrs = dict(base)
                if metric.error:
                    attrs["error.type"] = metric.error_type or "_OTHER"
                else:
                    if score is not None:
                        attrs["gen_ai.evaluation.score.value"] = float(score)
                    label = LABEL_NOT_EVALUATED if score is None else _LABELS.get(status or "")
                    if label:
                        attrs["gen_ai.evaluation.score.label"] = label
                if explanation:
                    attrs["gen_ai.evaluation.explanation"] = explanation
                if span is not None:
                    for key in ("gen_ai.agent.name", "gen_ai.conversation.id"):
                        value = attr_str(span.attributes, key)
                        if value:
                            attrs[key] = value
                response_id = next((c.response_id for c in reversed(turn.llm_calls) if c.response_id), None)
                if response_id:
                    attrs["gen_ai.response.id"] = response_id
                self._record(ref, attrs)
                emitted += 1
        return emitted

    def _log_failure(self, message: str) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_failure_log is not None and now - self._last_failure_log < FAILURE_LOG_INTERVAL_SECONDS:
                return
            self._last_failure_log = now
        logger.warning(message, exc_info=True)

    def flush(self, timeout_millis: int = 10_000) -> None:
        try:
            self._provider.force_flush(timeout_millis)
        except Exception:
            self._log_failure("Could not flush evaluation result events")

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:
            self._log_failure("Could not shut down the evaluation result exporter")


_emitter: EvaluationEmitter | None = None
_requested = False
_emitter_lock = threading.Lock()


def enable() -> None:
    """Turn emitting on for this process (the ``--emit-otel`` flag)."""
    global _requested
    _requested = True


def get_emitter() -> EvaluationEmitter | None:
    """The process emitter when emitting is on, created on first use; ``None`` otherwise."""
    global _emitter
    if not enabled():
        return None
    with _emitter_lock:
        if _emitter is None:
            _emitter = EvaluationEmitter()
        return _emitter


def flush() -> None:
    if _emitter is not None:
        _emitter.flush()


def shutdown() -> None:
    global _emitter
    with _emitter_lock:
        emitter, _emitter = _emitter, None
    if emitter is not None:
        emitter.shutdown()
