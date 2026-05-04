"""Pydantic models for persisted Run and Result rows.

These shapes are the durable, host-facing contract returned by ``/api/runs``
and emitted via :class:`ResultSink`. They are deliberately distinct from the
in-pipeline :class:`agentevals.runner.MetricResult` so renaming the persisted
fields (``status``, ``error_text``, ``latency_ms``) does not break the existing
``/api/evaluate`` SSE consumers.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResultStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    SKIPPED = "skipped"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_result_id(run_id: UUID | str, eval_set_item_id: str, evaluator_name: str) -> str:
    """Canonical SHA-256 of ``{run_id}|{eval_set_item_id}|{evaluator_name}``.

    Deterministic so both retried webhook posts and retried executor runs
    deduplicate cleanly via INSERT ... ON CONFLICT (result_id) DO UPDATE.
    """
    run_id_str = str(run_id).lower() if isinstance(run_id, UUID) else str(run_id).lower()
    payload = f"{run_id_str}|{eval_set_item_id}|{evaluator_name}".encode()
    return hashlib.sha256(payload).hexdigest()


class TraceTarget(BaseModel):
    """Where a run gets its trace from.

    Discriminated by ``kind``:
    - ``inline``: the OTLP/Jaeger JSON dict is embedded directly in the spec.
    - ``http``: a TraceFetcher GETs from ``base_url + "/" + trace_id`` using
      the run's ``context.headers``.
    - ``uploaded``: synthesis-only kind written by ``/api/evaluate`` after a
      synchronous UI/multipart upload completes. Records ``trace_count`` and
      ``trace_files`` for audit but the trace bytes themselves are not
      retained, so an ``uploaded`` run cannot be re-executed by the worker.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    kind: Literal["inline", "http", "uploaded"]
    inline: dict[str, Any] | None = None
    base_url: str | None = None
    trace_id: str | None = None
    trace_format: Literal["jaeger-json", "otlp-json"] | None = None
    trace_count: int | None = None
    trace_files: list[str] | None = None


class RunSpec(BaseModel):
    """Validated submission body. Stored verbatim in ``agentevals.run.spec``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    approach: Literal["trace_replay"] = "trace_replay"
    target: TraceTarget
    eval_set: dict[str, Any] | None = None
    eval_config: dict[str, Any] = Field(default_factory=dict)
    sinks: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    run_id: UUID
    status: RunStatus
    spec: RunSpec
    attempt: int = 0
    worker_id: str | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False


class Result(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    result_id: str
    run_id: UUID
    eval_set_item_id: str
    eval_set_item_name: str
    evaluator_name: str
    evaluator_type: Literal["builtin", "code", "remote", "openai_eval"]
    status: ResultStatus
    score: float | None = None
    per_invocation_scores: list[float | None] = Field(default_factory=list)
    trace_id: str | None = None
    span_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error_text: str | None = None
    tokens_used: dict[str, Any] | None = None
    latency_ms: int | None = None
    created_at: datetime = Field(default_factory=_now)

    @classmethod
    def from_metric_result(
        cls,
        *,
        run_id: UUID,
        eval_set_item_id: str,
        eval_set_item_name: str,
        trace_id: str | None,
        evaluator_type: Literal["builtin", "code", "remote", "openai_eval"],
        metric_result: Any,
    ) -> Result:
        """Project an in-pipeline MetricResult onto the persisted shape.

        ADK emits ``eval_status`` strings ``PASSED`` / ``FAILED`` /
        ``NOT_EVALUATED``; we additionally map presence of ``error`` to
        ``errored`` so downstream consumers don't have to special-case
        evaluator failures.
        """
        if metric_result.error:
            status = ResultStatus.ERRORED
        else:
            raw = (metric_result.eval_status or "NOT_EVALUATED").upper()
            status = {
                "PASSED": ResultStatus.PASSED,
                "FAILED": ResultStatus.FAILED,
            }.get(raw, ResultStatus.SKIPPED)

        scores: list[float | None] = list(metric_result.per_invocation_scores or [])
        latency_ms = int(metric_result.duration_ms) if metric_result.duration_ms is not None else None

        return cls(
            result_id=compute_result_id(run_id, eval_set_item_id, metric_result.metric_name),
            run_id=run_id,
            eval_set_item_id=eval_set_item_id,
            eval_set_item_name=eval_set_item_name,
            evaluator_name=metric_result.metric_name,
            evaluator_type=evaluator_type,
            status=status,
            score=metric_result.score,
            per_invocation_scores=scores,
            trace_id=trace_id,
            details=metric_result.details or {},
            error_text=metric_result.error,
            latency_ms=latency_ms,
        )
