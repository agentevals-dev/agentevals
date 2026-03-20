"""agentevals-evaluator-sdk — lightweight types and helpers for custom evaluator authors.

Install standalone with ``pip install agentevals-evaluator-sdk`` (no heavy deps).

Quick start::

    from agentevals_evaluator_sdk import evaluator, EvalInput, EvalResult


    @evaluator
    def my_evaluator(input: EvalInput) -> EvalResult:
        score = 1.0
        for inv in input.invocations:
            if not inv.final_response:
                score -= 0.5
        return EvalResult(score=max(0.0, score))


    if __name__ == "__main__":
        my_evaluator.run()
"""

from .decorator import evaluator
from .types import (
    EvalInput,
    EvalResult,
    IntermediateStepData,
    InvocationData,
    ToolCallData,
    ToolResponseData,
)

__all__ = [
    "evaluator",
    "EvalInput",
    "EvalResult",
    "IntermediateStepData",
    "InvocationData",
    "ToolCallData",
    "ToolResponseData",
]
