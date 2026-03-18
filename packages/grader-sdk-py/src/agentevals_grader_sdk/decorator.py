"""The ``@grader`` decorator — turns a function into a stdin/stdout grader script."""

from __future__ import annotations

import asyncio
import inspect
import sys
import traceback
from typing import Callable

from .types import EvalInput, EvalResult


def grader(fn: Callable[[EvalInput], EvalResult]) -> Callable[[EvalInput], EvalResult]:
    """Decorator that turns a grader function into a runnable stdin/stdout script.

    When the decorated module is executed (``python my_grader.py``), it:
    1. Reads JSON from stdin and parses it into an :class:`EvalInput`.
    2. Calls the decorated function with the parsed input.
    3. Serializes the returned :class:`EvalResult` to stdout as JSON.

    The decorated function can be sync or async.

    Example::

        from agentevals_grader_sdk import grader, EvalInput, EvalResult

        @grader
        def format_check(input: EvalInput) -> EvalResult:
            score = 1.0
            for inv in input.invocations:
                if not inv.final_response:
                    score -= 0.5
            return EvalResult(score=max(0.0, score))
    """

    def _run() -> None:
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
            _write_error(f"Grader function raised: {exc}\n{traceback.format_exc()}")
            sys.exit(1)

        if not isinstance(result, EvalResult):
            _write_error(
                f"Grader function must return EvalResult, got {type(result).__name__}"
            )
            sys.exit(1)

        sys.stdout.write(result.model_dump_json())
        sys.stdout.write("\n")
        sys.stdout.flush()

    import atexit
    atexit.register(_run)

    return fn


def _write_error(msg: str) -> None:
    """Write an error message to stderr."""
    print(msg, file=sys.stderr)
