"""Pydantic response and event models for the agentevals API.

Provides a StandardResponse[T] envelope, typed REST response models,
SSE evaluation event models, and live UI broadcast event models.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..config import EvalParams

T = TypeVar("T")


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class StandardResponse(CamelModel, Generic[T]):
    data: T
    error: str | None = None


# ---------------------------------------------------------------------------
# REST response data models
# ---------------------------------------------------------------------------


class HealthData(CamelModel):
    status: str
    version: str


class ApiKeyStatus(CamelModel):
    google: bool
    anthropic: bool
    openai: bool


class OtlpReceiverPorts(CamelModel):
    http_port: int
    grpc_port: int


class ConfigData(CamelModel):
    api_keys: ApiKeyStatus
    otlp: OtlpReceiverPorts | None = None


class MetricInfo(CamelModel):
    name: str
    category: str
    requires_eval_set: bool
    requires_llm: bool = Field(alias="requiresLLM")
    requires_gcp: bool = Field(alias="requiresGCP")
    requires_rubrics: bool
    description: str
    working: bool


class EvalSetValidation(CamelModel):
    valid: bool
    eval_set_id: str | None = None
    num_cases: int | None = None
    errors: list[str] = Field(default_factory=list)


class SessionInfo(CamelModel):
    session_id: str
    trace_id: str
    eval_set_id: str | None = None
    span_count: int
    is_complete: bool
    started_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    invocations: list[dict[str, Any]] | None = None


class CreateEvalSetData(CamelModel):
    eval_set: dict[str, Any]
    num_invocations: int


class SessionEvalResult(CamelModel):
    session_id: str
    trace_id: str | None = None
    num_invocations: int | None = None
    metric_results: list[dict[str, Any]] | None = None
    error: str | None = None


class EvaluateSessionsData(CamelModel):
    golden_session_id: str
    eval_set_id: str
    results: list[SessionEvalResult]


class PrepareEvaluationData(CamelModel):
    eval_set_url: str
    trace_urls: list[str]
    num_traces: int


class GetTraceData(CamelModel):
    session_id: str
    trace_content: str
    num_spans: int


class DebugLoadData(CamelModel):
    loaded_sessions: list[str]
    count: int


class TraceConversionMetadata(CamelModel):
    agent_name: str | None = None
    agent_id: str | None = None
    model: str | None = None
    response_model: str | None = None
    provider: str | None = None
    schema_version: str | None = Field(
        default=None,
        description=(
            "Version segment parsed from the emitting scope's schema URL "
            "(the path component after the schema family prefix). Per the "
            "OpenTelemetry schema URL spec, this version is only meaningful "
            "within its schema family — the host/path prefix before the "
            "version. It is comparable across traces only if they share a "
            "family, and equals the OpenTelemetry Semantic Conventions "
            "version only for the OTel family "
            "(https://opentelemetry.io/schemas/<version>). For traces from "
            "a custom or mirrored schema family, this is that family's own "
            "version number, not an OTel semconv version. The full schema "
            "URL (otel.schema_url) is preserved separately wherever the "
            "family needs to be disambiguated; this field is informational "
            "only and does not drive extraction or alias-resolution logic."
        ),
    )
    start_time: int | None = None
    user_input_preview: str | None = None
    final_output_preview: str | None = None
    session_name: str | None = None


class TraceConversionEntry(CamelModel):
    trace_id: str
    trace_ids: list[str] = Field(default_factory=list)
    invocations: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
    metadata: TraceConversionMetadata = Field(default_factory=TraceConversionMetadata)


class ConvertTracesData(CamelModel):
    traces: list[TraceConversionEntry]


class EvaluateJsonRequest(CamelModel):
    """Request body for JSON-based trace evaluation (``POST /evaluate/json``)."""

    traces: dict = Field(description="OTLP JSON export with resourceSpans structure.")
    config: EvalParams = Field(default_factory=EvalParams, description="Evaluation parameters.")
    eval_set: dict | None = Field(default=None, description="Optional ADK EvalSet JSON.")
    credential_refs: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Map of logical credential name to a secret reference dict. Each reference has a "
            "'kind' (the resolver to use) plus that kind's locator fields. Resolved per call to its "
            "secret value; never written to the process environment. How a value is used (e.g. which "
            "judge provider it authenticates) is configured on the consumer, not the reference."
        ),
    )


# ---------------------------------------------------------------------------
# SSE evaluation event models
# ---------------------------------------------------------------------------


class SSEProgressEvent(CamelModel):
    message: str


class SSETraceProgress(CamelModel):
    trace_id: str
    partial_result: dict[str, Any]


class SSETraceProgressEvent(CamelModel):
    trace_progress: SSETraceProgress


class SSEPerformanceMetricsEvent(CamelModel):
    trace_id: str
    performance_metrics: dict[str, Any]
    trace_metadata: dict[str, Any] | None = None


class SSEDoneEvent(CamelModel):
    done: bool = True
    result: dict[str, Any]


class SSEErrorEvent(CamelModel):
    error: str


# ---------------------------------------------------------------------------
# Live UI broadcast event models
# ---------------------------------------------------------------------------


class WSSessionStartedEvent(CamelModel):
    type: str = "session_started"
    session: SessionInfo


class WSSessionCompleteEvent(CamelModel):
    type: str = "session_complete"
    session_id: str
    invocations: list[dict[str, Any]]


class WSSessionRemovedEvent(CamelModel):
    type: str = "session_removed"
    session_id: str
    absorbed_by: str | None = None
