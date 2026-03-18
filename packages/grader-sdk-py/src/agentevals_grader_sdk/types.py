"""Subprocess protocol types for custom grader evaluation.

These types define the JSON stdin/stdout contract between agentevals and
external grader scripts/containers.  They are intentionally simple and
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


class InvocationData(BaseModel):
    """Simplified representation of a single agent invocation (turn).

    This is a language-agnostic view of ADK's ``Invocation``, flattened into
    plain strings and dicts so that script/container authors don't need ADK.
    """

    invocation_id: str = ""
    user_content: str = ""
    final_response: Optional[str] = None
    tool_calls: list[ToolCallData] = Field(default_factory=list)
    tool_responses: list[ToolResponseData] = Field(default_factory=list)


class EvalInput(BaseModel):
    """Input payload sent to a custom grader script/container on stdin."""

    metric_name: str
    threshold: float = 0.5
    config: dict[str, Any] = Field(default_factory=dict)
    invocations: list[InvocationData] = Field(default_factory=list)
    expected_invocations: Optional[list[InvocationData]] = None


class EvalResult(BaseModel):
    """Output payload expected from a custom grader script/container on stdout."""

    score: float = Field(ge=0.0, le=1.0)
    status: Optional[str] = Field(
        default=None,
        description='One of "PASSED", "FAILED", "NOT_EVALUATED". Derived from score vs threshold if omitted.',
    )
    per_invocation_scores: list[Optional[float]] = Field(default_factory=list)
    details: Optional[dict[str, Any]] = None
