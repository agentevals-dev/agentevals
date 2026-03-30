"""Pydantic response and event models for the agentevals API.

Provides a StandardResponse[T] envelope, typed REST response models,
SSE evaluation event models, and WebSocket/UI broadcast event models.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

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


class ConfigData(CamelModel):
    api_keys: ApiKeyStatus


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
    model: str | None = None
    start_time: int | None = None
    user_input_preview: str | None = None
    final_output_preview: str | None = None
    session_name: str | None = None


class TraceConversionEntry(CamelModel):
    trace_id: str
    invocations: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
    metadata: TraceConversionMetadata = Field(default_factory=TraceConversionMetadata)


class ConvertTracesData(CamelModel):
    traces: list[TraceConversionEntry]


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
# WebSocket / UI broadcast event models
# ---------------------------------------------------------------------------


class WSSessionStartedEvent(CamelModel):
    type: str = "session_started"
    session: SessionInfo


class WSSessionCompleteEvent(CamelModel):
    type: str = "session_complete"
    session_id: str
    invocations: list[dict[str, Any]]


class WSSpanReceivedEvent(CamelModel):
    type: str = "span_received"
    session_id: str
    span: dict[str, Any]


class WSUserInputEvent(CamelModel):
    type: str = "user_input"
    session_id: str
    invocation_id: str
    text: str
    timestamp: float


class WSAgentResponseEvent(CamelModel):
    type: str = "agent_response"
    session_id: str
    invocation_id: str
    text: str
    timestamp: float


class WSToolCallEvent(CamelModel):
    type: str = "tool_call"
    session_id: str
    invocation_id: str
    tool_call: dict[str, Any]
    timestamp: float


class WSTokenUpdateEvent(CamelModel):
    type: str = "token_update"
    session_id: str
    invocation_id: str | None = None
    input_tokens: int
    output_tokens: int
    model: str | None = None


class WSErrorEvent(CamelModel):
    type: str = "error"
    message: str
