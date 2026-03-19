"""CLI-internal protocol types for the custom evaluator JSON wire format.

These mirror the types in ``agentevals_evaluator_sdk.types`` but are owned by
the CLI so that the CLI and SDK packages can be versioned independently.  The
JSON schema produced/consumed by these models is the contract — not the Python
types themselves.

Protocol versioning rules:
- ``protocol_version`` uses ``"MAJOR.MINOR"`` format.
- MINOR bumps are additive-only (new fields with defaults).  Old deserializers
  silently ignore unknown fields.
- MAJOR bumps signal breaking changes (removed/renamed fields, type changes).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0"


class ToolCallData(BaseModel):
    """A single tool call made by the agent."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResponseData(BaseModel):
    """A single tool response received by the agent."""

    name: str
    output: str = ""


class IntermediateStepData(BaseModel):
    """Intermediate steps between user input and final response."""

    tool_calls: list[ToolCallData] = Field(default_factory=list)
    tool_responses: list[ToolResponseData] = Field(default_factory=list)


class InvocationData(BaseModel):
    """Simplified, language-agnostic representation of a single agent turn."""

    invocation_id: str = ""
    user_content: str = ""
    final_response: Optional[str] = None
    intermediate_steps: IntermediateStepData = Field(default_factory=IntermediateStepData)


class EvalInput(BaseModel):
    """Input payload sent to a custom evaluator on stdin."""

    protocol_version: str = PROTOCOL_VERSION
    metric_name: str
    threshold: float = 0.5
    config: dict[str, Any] = Field(default_factory=dict)
    invocations: list[InvocationData] = Field(default_factory=list)
    expected_invocations: Optional[list[InvocationData]] = None


class EvalResult(BaseModel):
    """Output payload expected from a custom evaluator on stdout."""

    score: float = Field(ge=0.0, le=1.0)
    status: Optional[str] = Field(
        default=None,
        description='One of "PASSED", "FAILED", "NOT_EVALUATED". Derived from score vs threshold if omitted.',
    )
    per_invocation_scores: list[Optional[float]] = Field(default_factory=list)
    details: Optional[dict[str, Any]] = None
