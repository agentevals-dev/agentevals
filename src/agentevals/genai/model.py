"""Canonical conversation model extracted from GenAI telemetry.

Every turn, logical LLM call and tool call is keyed by the span it came from (``SpanRef``),
so results can be attached back to the exact telemetry they judged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..otel.model import SpanRef

Status = Literal["unset", "ok", "error"]
AnchorKind = Literal["agent", "workflow", "root"]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in Usage.model_fields})


class LlmCall(BaseModel):
    """One logical model call: a leaf inference span, collapsed with its single-leaf wrappers."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: SpanRef
    wrapper_ref: SpanRef | None = None
    agent_name: str | None = None
    operation: str
    provider: str | None = None
    request_model: str | None = None
    response_model: str | None = None
    response_id: str | None = None
    usage: Usage | None = None
    finish_reasons: list[str] = Field(default_factory=list)
    system_instructions: list[dict[str, Any]] = Field(default_factory=list)
    input_messages: list[dict[str, Any]] = Field(default_factory=list)
    output_messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_definitions: list[dict[str, Any]] = Field(default_factory=list)
    status: Status = "unset"
    error_type: str | None = None
    delegated: bool = False
    start_ns: int = 0
    end_ns: int = 0


class ToolCall(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: SpanRef | None = None
    agent_name: str | None = None
    name: str
    call_id: str | None = None
    arguments: Any = None
    result: Any = None
    is_error: bool = False
    error_type: str | None = None
    start_ns: int | None = None


class ExternalEvaluation(BaseModel):
    """A ``gen_ai.evaluation.result`` produced by someone else and found in the telemetry."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: SpanRef
    name: str
    score_value: float | None = None
    score_label: str | None = None
    explanation: str | None = None
    source_service: str | None = None


class Turn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: SpanRef
    anchor_kind: AnchorKind = "agent"
    index: int = 0
    label: str | None = None
    agent_name: str | None = None
    agents: list[str] = Field(default_factory=list)
    user_input: list[dict[str, Any]] = Field(default_factory=list)
    final_output: list[dict[str, Any]] = Field(default_factory=list)
    intermediate_outputs: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[LlmCall] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    start_ns: int = 0
    end_ns: int = 0
    status: Status = "unset"
    error_type: str | None = None
    content_captured: bool = False
    conversation_id: str | None = None
    framework_ids: dict[str, str] = Field(default_factory=dict)
    external_evaluations: list[ExternalEvaluation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Conversation(BaseModel):
    key: str
    conversation_id: str | None = None
    trace_ids: list[str] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
