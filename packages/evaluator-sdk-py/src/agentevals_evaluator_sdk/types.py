"""Subprocess protocol types for custom evaluator evaluation.

These types define the JSON stdin/stdout contract between agentevals and
external evaluator scripts/containers.  They are intentionally simple and
free of ADK-specific types so they can be used from any language.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ToolCallData(BaseModel):
    """A single tool call made by the agent."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResponseData(BaseModel):
    """A single tool response received by the agent."""

    name: str
    output: str = ""


class IntermediateStepData(BaseModel):
    """The intermediate steps an agent took between receiving user input and
    producing a final response — tool calls, tool responses, and (in the
    future) reasoning traces, memory lookups, sub-agent calls, etc.

    Mirrors the semantic role of ADK's ``IntermediateData`` without depending
    on ADK types.
    """

    tool_calls: list[ToolCallData] = Field(default_factory=list)
    tool_responses: list[ToolResponseData] = Field(default_factory=list)


class InvocationData(BaseModel):
    """Simplified representation of a single agent invocation (turn).

    This is a language-agnostic view of ADK's ``Invocation`` so that
    script/container authors don't need ADK.
    """

    invocation_id: str = ""
    user_content: str = ""
    final_response: Optional[str] = None
    intermediate_steps: IntermediateStepData = Field(default_factory=IntermediateStepData)


class EvalInput(BaseModel):
    """Input payload sent to a custom evaluator script/container on stdin."""

    protocol_version: str = "1.0"
    metric_name: str
    threshold: float = 0.5
    config: dict[str, Any] = Field(default_factory=dict)
    invocations: list[InvocationData] = Field(default_factory=list)
    expected_invocations: Optional[list[InvocationData]] = None


class EvalResult(BaseModel):
    """Output payload expected from a custom evaluator script/container on stdout."""

    score: float = Field(ge=0.0, le=1.0)
    status: Optional[str] = Field(
        default=None,
        description='One of "PASSED", "FAILED", "NOT_EVALUATED". Derived from score vs threshold if omitted.',
    )
    per_invocation_scores: list[Optional[float]] = Field(default_factory=list)
    details: Optional[dict[str, Any]] = None
