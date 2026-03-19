# agentevals-evaluator-sdk

Lightweight SDK for building custom [agentevals](https://github.com/agentevals-dev/agentevals) evaluators.

An evaluator is a standalone program that scores agent traces. It reads `EvalInput` JSON from stdin and writes `EvalResult` JSON to stdout. This SDK provides the Python types and a `@evaluator` decorator that handles all the plumbing.

## Installation

```bash
pip install agentevals-evaluator-sdk
```

## Usage

```python
from agentevals_evaluator_sdk import evaluator, EvalInput, EvalResult

@evaluator
def my_evaluator(input: EvalInput) -> EvalResult:
    scores = []
    for inv in input.invocations:
        score = 1.0 if inv.final_response else 0.0
        scores.append(score)

    return EvalResult(
        score=sum(scores) / len(scores) if scores else 0.0,
        per_invocation_scores=scores,
    )

if __name__ == "__main__":
    my_evaluator.run()
```

The `@evaluator` decorator marks your function as a runnable evaluator. Call `.run()` to execute it as a stdin/stdout script -- it reads JSON from stdin, calls your function, and writes the result to stdout. The decorated function can still be called directly in tests.

## Types

- **`EvalInput`** -- input payload with `metric_name`, `threshold`, `config`, `invocations`, and optional `expected_invocations`
- **`EvalResult`** -- output payload with `score` (0.0-1.0), optional `status`, `per_invocation_scores`, and `details` (dict)
- **`InvocationData`** -- a single agent turn with `user_content`, `final_response`, and `intermediate_steps`
- **`IntermediateStepData`** -- the steps between user input and final response: `tool_calls` and `tool_responses`

## Documentation

See the [custom evaluators documentation](https://github.com/agentevals-dev/agentevals/blob/main/docs/custom-evaluators.md) for the full protocol reference and examples in other languages.
