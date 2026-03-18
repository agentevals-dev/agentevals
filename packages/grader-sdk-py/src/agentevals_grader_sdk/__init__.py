"""agentevals-grader-sdk — lightweight types and helpers for custom grader authors.

Install standalone with ``pip install agentevals-grader-sdk`` (no heavy deps).

Quick start::

    from agentevals_grader_sdk import grader, EvalInput, EvalResult

    @grader
    def my_grader(input: EvalInput) -> EvalResult:
        score = 1.0
        for inv in input.invocations:
            if not inv.final_response:
                score -= 0.5
        return EvalResult(score=max(0.0, score))
"""

from .decorator import grader
from .types import (
    EvalInput,
    EvalResult,
    InvocationData,
    ToolCallData,
    ToolResponseData,
)

__all__ = [
    "grader",
    "EvalInput",
    "EvalResult",
    "InvocationData",
    "ToolCallData",
    "ToolResponseData",
]
