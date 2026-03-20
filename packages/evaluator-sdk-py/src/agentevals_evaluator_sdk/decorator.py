"""The ``@evaluator`` decorator — turns a function into a stdin/stdout evaluator script."""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import traceback
from typing import Callable

from .types import EvalInput, EvalResult


def evaluator(fn: Callable[[EvalInput], EvalResult]) -> Callable[[EvalInput], EvalResult]:
    """Decorator that marks a function as a runnable evaluator.

    The decorated function can still be called normally (e.g. in tests).
    To run it as a stdin/stdout script, call ``.run()``::

        from agentevals_evaluator_sdk import evaluator, EvalInput, EvalResult


        @evaluator
        def format_check(input: EvalInput) -> EvalResult:
            score = 1.0
            for inv in input.invocations:
                if not inv.final_response:
                    score -= 0.5
            return EvalResult(score=max(0.0, score))


        if __name__ == "__main__":
            format_check.run()
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    def run() -> None:
        """Read EvalInput from stdin, call the evaluator, write EvalResult to stdout."""
        raw = sys.stdin.read()
        if not raw.strip():
            _write_error("No input received on stdin")
            sys.exit(1)

        try:
            eval_input = EvalInput.model_validate_json(raw)
        except Exception as exc:
            _write_error(f"Failed to parse input: {exc}")
            sys.exit(1)

        try:
            if inspect.iscoroutinefunction(fn):
                result = asyncio.run(fn(eval_input))
            else:
                result = fn(eval_input)
        except Exception as exc:
            _write_error(f"Evaluator function raised: {exc}\n{traceback.format_exc()}")
            sys.exit(1)

        if not isinstance(result, EvalResult):
            _write_error(f"Evaluator function must return EvalResult, got {type(result).__name__}")
            sys.exit(1)

        sys.stdout.write(result.model_dump_json())
        sys.stdout.write("\n")
        sys.stdout.flush()

    wrapper.run = run
    return wrapper


def _write_error(msg: str) -> None:
    """Write an error message to stderr."""
    print(msg, file=sys.stderr)
