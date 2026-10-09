"""Custom evaluators that run evaluators via pluggable backends.

Every backend implements the same protocol: accept :class:`EvalInput` (JSON)
and return :class:`EvalResult` (JSON).  The transport varies — local
subprocess, HTTP, Docker container, etc.

The protocol types live in :mod:`agentevals._protocol` (CLI-internal) and are
JSON-wire-compatible with the types in the ``agentevals-evaluator-sdk`` package.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentevals._protocol import (
    EvalInput,
    EvalResult,
    IntermediateStepData,
    InvocationData,
    ToolCallData,
    ToolResponseData,
)
from agentevals.genai.matching import ExpectedConversation, ExpectedTurn, align
from agentevals.genai.messages import text_of
from agentevals.genai.model import Conversation, Turn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EvaluatorBackend — primary abstraction
# ---------------------------------------------------------------------------


class EvaluatorBackend(abc.ABC):
    """Delivers :class:`EvalInput` to an evaluator and returns :class:`EvalResult`.

    Subclasses encapsulate the *transport* — subprocess, HTTP, Docker, etc.
    """

    @abc.abstractmethod
    async def run(self, eval_input: EvalInput, metric_name: str) -> EvalResult:
        """Execute the evaluator and return its result."""


# ---------------------------------------------------------------------------
# Runtime — language-specific helpers for SubprocessBackend
# ---------------------------------------------------------------------------


class Runtime(abc.ABC):
    """Maps a file extension to the command needed to run it."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable runtime name (e.g. ``"Python"``)."""

    @property
    @abc.abstractmethod
    def extensions(self) -> tuple[str, ...]:
        """File extensions this runtime handles (e.g. ``(".py",)``)."""

    @abc.abstractmethod
    def build_command(self, path: Path) -> list[str]:
        """Return the argv list to execute *path*."""

    def is_available(self) -> bool:
        """Return True if the runtime's interpreter is found on the system."""
        try:
            self.build_command(Path("__probe__"))
            return True
        except RuntimeError:
            return False


class PythonRuntime(Runtime):
    def __init__(self, python_path: Path | None = None):
        self._exe = str(python_path) if python_path else sys.executable

    @property
    def name(self) -> str:
        return "Python"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".py",)

    def build_command(self, path: Path) -> list[str]:
        return [self._exe, str(path)]

    def is_available(self) -> bool:
        return True


class NodeRuntime(Runtime):
    def __init__(self) -> None:
        self._exe = shutil.which("node")

    @property
    def name(self) -> str:
        return "Node.js"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".js", ".ts")

    def build_command(self, path: Path) -> list[str]:
        if not self._exe:
            raise RuntimeError("Node.js not found on PATH (required for .js/.ts evaluators)")
        return [self._exe, str(path)]

    def is_available(self) -> bool:
        return self._exe is not None


_RUNTIMES: list[Runtime] = [
    PythonRuntime(),
    NodeRuntime(),
]


def get_runtimes() -> list[Runtime]:
    """Return all registered runtimes."""
    return list(_RUNTIMES)


def supported_extensions() -> set[str]:
    """All file extensions supported by registered runtimes."""
    exts: set[str] = set()
    for rt in _RUNTIMES:
        exts.update(rt.extensions)
    return exts


def _resolve_runtime(path: Path) -> Runtime:
    """Find the runtime that handles *path*'s extension."""
    suffix = path.suffix.lower()
    for rt in _RUNTIMES:
        if suffix in rt.extensions:
            return rt
    raise ValueError(f"No runtime registered for extension '{suffix}'. Supported: {sorted(supported_extensions())}")


# ---------------------------------------------------------------------------
# Subprocess runner (used by SubprocessBackend)
# ---------------------------------------------------------------------------


async def _run_subprocess(
    cmd: list[str],
    input_json: str,
    timeout: int,
    metric_name: str,
) -> EvalResult:
    """Run a subprocess, pipe JSON on stdin, read JSON from stdout."""
    logger.info("Running custom evaluator %r: %s", metric_name, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input_json.encode()),
            timeout=timeout,
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Custom evaluator '{metric_name}' timed out after {timeout}s") from exc

    stderr_text = stderr_bytes.decode(errors="replace").strip()
    if stderr_text:
        logger.debug("Custom evaluator %r stderr:\n%s", metric_name, stderr_text)

    if proc.returncode != 0:
        raise RuntimeError(
            f"Custom evaluator '{metric_name}' exited with code {proc.returncode}"
            + (f": {stderr_text}" if stderr_text else "")
        )

    stdout_text = stdout_bytes.decode().strip()
    if not stdout_text:
        hint = ""
        if stderr_text:
            hint = f"\nEvaluator stderr:\n{stderr_text}"
        raise RuntimeError(f"Custom evaluator '{metric_name}' produced no output on stdout" + hint)

    try:
        return EvalResult.model_validate_json(stdout_text)
    except Exception as exc:
        raise RuntimeError(f"Custom evaluator '{metric_name}' produced invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


class SubprocessBackend(EvaluatorBackend):
    """Runs a local code file (.py, .js, .ts, …) as a subprocess.

    The correct interpreter is resolved from the file extension via the
    :data:`_RUNTIMES` registry.  Pass a pre-configured *runtime* to override
    the default (e.g. a :class:`PythonRuntime` with a venv interpreter).
    """

    def __init__(self, path: Path, timeout: int = 30, runtime: Runtime | None = None):
        self._path = path.resolve()
        self._runtime = runtime or _resolve_runtime(self._path)
        self._timeout = timeout

        if not self._path.exists():
            raise FileNotFoundError(f"Evaluator file not found: {self._path}")

    async def run(self, eval_input: EvalInput, metric_name: str) -> EvalResult:
        cmd = self._runtime.build_command(self._path)
        return await _run_subprocess(cmd, eval_input.model_dump_json(), self._timeout, metric_name)


# ---------------------------------------------------------------------------
# Executor factory
# ---------------------------------------------------------------------------

_EXECUTOR_FACTORIES: dict[str, Callable[..., EvaluatorBackend]] = {
    "local": lambda path, timeout: SubprocessBackend(path, timeout),
}


def create_executor(executor_name: str, path: Path, timeout: int = 30) -> EvaluatorBackend:
    """Construct an EvaluatorBackend by executor name (e.g. 'local', 'docker')."""
    factory = _EXECUTOR_FACTORIES.get(executor_name)
    if factory is None:
        raise ValueError(f"Unknown executor '{executor_name}'. Available: {sorted(_EXECUTOR_FACTORIES.keys())}")
    return factory(path, timeout)


def register_executor(name: str, factory: Callable[..., EvaluatorBackend]) -> None:
    """Register a new executor factory (e.g. for Docker support)."""
    _EXECUTOR_FACTORIES[name] = factory


# ---------------------------------------------------------------------------
# Canonical turns -> protocol InvocationData
# ---------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(value)


def _args_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {} if value is None else {"value": value}


def turn_to_data(turn: Turn, performance_metrics: dict[str, Any] | None = None) -> InvocationData:
    calls = [ToolCallData(name=t.name, args=_args_dict(t.arguments), id=t.call_id) for t in turn.tool_calls]
    responses = [
        ToolResponseData(
            name=t.name,
            output=_json_text(t.result),
            status="error" if t.is_error else None,
            id=t.call_id,
            response=t.result,
        )
        for t in turn.tool_calls
        if t.result is not None or t.is_error
    ]
    return InvocationData(
        invocation_id=turn.ref.span_id,
        user_content=text_of(turn.user_input) or "",
        final_response=text_of(turn.final_output),
        intermediate_steps=IntermediateStepData(tool_calls=calls, tool_responses=responses),
        performance_metrics=performance_metrics,
        trace_id=turn.ref.trace_id,
        span_id=turn.ref.span_id,
    )


def expected_turn_to_data(turn: ExpectedTurn) -> InvocationData:
    calls = [
        ToolCallData(name=c.get("name") or "", args=_args_dict(c.get("arguments")), id=c.get("id"))
        for c in turn.tool_calls
    ]
    responses = [
        ToolResponseData(
            name=r.get("name") or "", output=_json_text(r.get("response")), id=r.get("id"), response=r.get("response")
        )
        for r in turn.tool_responses
    ]
    return InvocationData(
        invocation_id=turn.invocation_id or "",
        user_content=turn.user_text or "",
        final_response=turn.final_text,
        intermediate_steps=IntermediateStepData(tool_calls=calls, tool_responses=responses),
    )


def _status_of(result: EvalResult, threshold: float) -> str:
    if result.status is not None:
        return result.status.value
    return "PASSED" if result.score >= threshold else "FAILED"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def evaluate_custom_evaluator(
    evaluator_def,
    actual: Conversation,
    expected: ExpectedConversation | None,
    performance_metrics: dict[str, Any] | None = None,
    expected_reason: str | None = None,
):
    """Evaluate one evaluator over a conversation and return a ``MetricResult``.

    ``expected`` is the golden conversation selected for ``actual``, or ``None`` with
    ``expected_reason`` explaining why there is none.
    """
    from .config import BuiltinMetricDef, CodeEvaluatorDef, RemoteEvaluatorDef
    from .runner import MetricResult

    if isinstance(evaluator_def, BuiltinMetricDef):
        from .adk_bridge import expected_invocations, to_adk_invocations
        from .builtin_metrics import evaluate_builtin_metric

        actual_invocations = to_adk_invocations(actual.turns)
        golden = expected_invocations(expected)
        alignment: dict[str, int] = {}
        if golden is not None and len(golden) != len(actual_invocations):
            aligned = align(actual.turns, expected.turns)
            n = len(aligned.pairs)
            actual_invocations, golden = actual_invocations[:n], golden[:n]
            alignment = {"missing_turns": aligned.missing_turns, "unexpected_turns": aligned.unexpected_turns}
        result = await evaluate_builtin_metric(
            metric_name=evaluator_def.name,
            actual_invocations=actual_invocations,
            expected_invocations=golden,
            judge_model=evaluator_def.judge_model,
            threshold=evaluator_def.threshold,
            match_type=evaluator_def.trajectory_match_type,
            credential_ref=evaluator_def.credential_ref,
            judge_base_url=evaluator_def.judge_base_url,
            expected_reason=expected_reason,
        )
        if alignment:
            result.details = {**(result.details or {}), **alignment}
        return result

    is_remote = isinstance(evaluator_def, RemoteEvaluatorDef)
    if is_remote:
        from .evaluator.resolver import get_default_resolver

        evaluator_def = await get_default_resolver().resolve(evaluator_def)

    if not isinstance(evaluator_def, CodeEvaluatorDef):
        raise ValueError(f"Unsupported custom evaluator type: {type(evaluator_def).__name__}")

    evaluator_path = Path(evaluator_def.path)
    runtime: Runtime | None = None
    if evaluator_path.suffix == ".py":
        from .evaluator.venv import ensure_venv_async

        try:
            venv_python = await ensure_venv_async(evaluator_path, strict_requirements=is_remote)
        except Exception as exc:
            logger.error("Failed to set up venv for '%s': %s", evaluator_def.name, exc)
            return MetricResult(
                metric_name=evaluator_def.name,
                error=f"Dependency installation failed: {exc}",
                error_type=type(exc).__name__,
            )
        if venv_python:
            runtime = PythonRuntime(python_path=venv_python)

    if runtime is not None:
        backend: EvaluatorBackend = SubprocessBackend(evaluator_path, evaluator_def.timeout, runtime=runtime)
    else:
        backend = create_executor(evaluator_def.executor, evaluator_path, evaluator_def.timeout)

    eval_input = EvalInput(
        metric_name=evaluator_def.name,
        threshold=evaluator_def.threshold,
        config=evaluator_def.config,
        invocations=[turn_to_data(t, performance_metrics) for t in actual.turns],
        expected_invocations=[expected_turn_to_data(t) for t in expected.turns] if expected else None,
    )
    try:
        result = await backend.run(eval_input, evaluator_def.name)
    except Exception as exc:
        logger.exception("Failed to evaluate custom evaluator '%s'", evaluator_def.name)
        return MetricResult(metric_name=evaluator_def.name, error=str(exc), error_type=type(exc).__name__)

    threshold = evaluator_def.threshold
    return MetricResult(
        metric_name=evaluator_def.name,
        score=result.score,
        eval_status=_status_of(result, threshold),
        per_invocation_scores=list(result.per_invocation_scores),
        per_invocation_statuses=[
            "NOT_EVALUATED" if s is None else "PASSED" if s >= threshold else "FAILED"
            for s in result.per_invocation_scores
        ],
        details=result.details,
    )
