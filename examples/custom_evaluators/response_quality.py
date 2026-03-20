"""Example custom evaluator: checks that every invocation has a non-empty response
and that responses don't just parrot back the user input.

Install the SDK standalone:  pip install agentevals-evaluator-sdk

Usage in eval_config.yaml:

    evaluators:
      - name: response_quality
        type: code
        path: ./examples/custom_evaluators/response_quality.py
        threshold: 0.7
        config:
          min_response_length: 20
"""

from agentevals_evaluator_sdk import EvalInput, EvalResult, evaluator


@evaluator
def response_quality(input: EvalInput) -> EvalResult:
    min_len = input.config.get("min_response_length", 10)
    scores: list[float] = []
    issues: list[str] = []

    for inv in input.invocations:
        score = 1.0

        if not inv.final_response:
            score = 0.0
            issues.append(f"{inv.invocation_id}: no response")
            scores.append(score)
            continue

        if len(inv.final_response.strip()) < min_len:
            score -= 0.3
            issues.append(
                f"{inv.invocation_id}: response too short ({len(inv.final_response.strip())} < {min_len} chars)"
            )

        if inv.user_content and inv.final_response.strip().lower() == inv.user_content.strip().lower():
            score -= 0.5
            issues.append(f"{inv.invocation_id}: response is just the user input echoed back")

        scores.append(max(0.0, score))

    overall = sum(scores) / len(scores) if scores else 0.0

    return EvalResult(
        score=overall,
        per_invocation_scores=scores,
        details={"issues": issues} if issues else None,
    )


if __name__ == "__main__":
    response_quality.run()
