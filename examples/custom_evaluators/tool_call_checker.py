"""Example custom evaluator: checks that every invocation made at least one tool call.

Usage in eval_config.yaml:

    evaluators:
      - name: tool_call_checker
        type: code
        path: ./examples/custom_evaluators/tool_call_checker.py
        threshold: 1.0
        config:
          min_tool_calls: 1
"""

from agentevals_evaluator_sdk import EvalInput, EvalResult, evaluator


@evaluator
def tool_call_checker(input: EvalInput) -> EvalResult:
    min_calls = input.config.get("min_tool_calls", 1)
    scores: list[float] = []

    for inv in input.invocations:
        if len(inv.intermediate_steps.tool_calls) >= min_calls:
            scores.append(1.0)
        else:
            scores.append(0.0)

    overall = sum(scores) / len(scores) if scores else 0.0
    return EvalResult(
        score=overall,
        per_invocation_scores=scores,
    )


if __name__ == "__main__":
    tool_call_checker.run()
