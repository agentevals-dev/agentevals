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
import logging
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.adk.evaluation.eval_case import Invocation, get_all_tool_calls
from google.adk.evaluation.evaluator import EvalStatus, EvaluationResult, Evaluator, PerInvocationResult

from agentevals._protocol import (
    EvalInput,
    EvalResult,
    InvocationData,
    ToolCallData,
    ToolResponseData,
)

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
# ADK Invocation ↔ InvocationData conversion
# ---------------------------------------------------------------------------


def _content_to_text(content) -> str:
    """Extract plain text from an ADK Content object."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if hasattr(content, "parts") and content.parts:
        texts = []
        for part in content.parts:
            if hasattr(part, "text") and part.text:
                texts.append(part.text)
        return " ".join(texts)
    return ""


def _extract_tool_calls_from_invocation(inv: Invocation) -> list[ToolCallData]:
    """Extract tool calls from an Invocation's intermediate_data."""
    calls: list[ToolCallData] = []
    if not inv.intermediate_data:
        return calls

    try:
        tool_uses = get_all_tool_calls(inv.intermediate_data)
        for tc in tool_uses:
            calls.append(ToolCallData(name=tc.name or "", args=tc.args or {}))
    except Exception:
        pass

    return calls


def _extract_tool_responses_from_invocation(inv: Invocation) -> list[ToolResponseData]:
    """Extract tool responses from intermediate_data."""
    responses: list[ToolResponseData] = []
    if not inv.intermediate_data:
        return responses

    if hasattr(inv.intermediate_data, "tool_responses"):
        for tr in inv.intermediate_data.tool_responses or []:
            name = ""
            output = ""
            if hasattr(tr, "name"):
                name = tr.name or ""
            if hasattr(tr, "response"):
                output = str(tr.response) if tr.response else ""
            elif hasattr(tr, "output"):
                output = str(tr.output) if tr.output else ""
            responses.append(ToolResponseData(name=name, output=output))

    return responses


def invocation_to_data(inv: Invocation) -> InvocationData:
    """Convert an ADK Invocation to a simplified InvocationData for the protocol."""
    return InvocationData(
        invocation_id=inv.invocation_id or "",
        user_content=_content_to_text(inv.user_content),
        final_response=_content_to_text(inv.final_response) or None,
        tool_calls=_extract_tool_calls_from_invocation(inv),
        tool_responses=_extract_tool_responses_from_invocation(inv),
    )


def invocations_to_data(invocations: list[Invocation] | None) -> list[InvocationData] | None:
    """Convert a list of ADK Invocations, or return None."""
    if invocations is None:
        return None
    return [invocation_to_data(inv) for inv in invocations]


# ---------------------------------------------------------------------------
# EvalResult → EvaluationResult conversion
# ---------------------------------------------------------------------------


def _eval_result_to_evaluation_result(
    result: EvalResult,
    threshold: float,
    actual_invocations: list[Invocation],
) -> EvaluationResult:
    """Convert our protocol EvalResult into an ADK EvaluationResult."""
    if result.status:
        status_map = {
            "PASSED": EvalStatus.PASSED,
            "FAILED": EvalStatus.FAILED,
            "NOT_EVALUATED": EvalStatus.NOT_EVALUATED,
        }
        overall_status = status_map.get(result.status.upper(), EvalStatus.NOT_EVALUATED)
    else:
        overall_status = EvalStatus.PASSED if result.score >= threshold else EvalStatus.FAILED

    per_inv_results: list[PerInvocationResult] = []
    for i, inv in enumerate(actual_invocations):
        score = result.per_invocation_scores[i] if i < len(result.per_invocation_scores) else None
        per_inv_results.append(
            PerInvocationResult(
                actual_invocation=inv,
                score=score,
                eval_status=overall_status,
            )
        )

    return EvaluationResult(
        overall_score=result.score,
        overall_eval_status=overall_status,
        per_invocation_results=per_inv_results,
    )


# ---------------------------------------------------------------------------
# CustomEvaluatorRunner — ADK Evaluator adapter (backend-agnostic)
# ---------------------------------------------------------------------------


class CustomEvaluatorRunner(Evaluator):
    """Wraps any :class:`EvaluatorBackend` as an ADK :class:`Evaluator`.

    Handles the conversion between ADK ``Invocation`` objects and the
    language-agnostic ``EvalInput``/``EvalResult`` protocol.
    """

    def __init__(
        self,
        backend: EvaluatorBackend,
        metric_name: str,
        threshold: float = 0.5,
        config: dict[str, Any] | None = None,
    ):
        self._backend = backend
        self._metric_name = metric_name
        self._threshold = threshold
        self._config = config or {}

    async def evaluate_invocations(
        self,
        actual_invocations: list[Invocation],
        expected_invocations: list[Invocation] | None = None,
        conversation_scenario=None,
    ) -> EvaluationResult:

        eval_input = EvalInput(
            metric_name=self._metric_name,
            threshold=self._threshold,
            config=self._config,
            invocations=invocations_to_data(actual_invocations) or [],
            expected_invocations=invocations_to_data(expected_invocations),
        )

        result = await self._backend.run(eval_input, self._metric_name)
        return _eval_result_to_evaluation_result(result, self._threshold, actual_invocations)


# ---------------------------------------------------------------------------
# Public helper — build and run a custom evaluator from a config definition
# ---------------------------------------------------------------------------


async def evaluate_custom_evaluator(
    evaluator_def,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
):
    """Evaluate a single custom evaluator and return a ``MetricResult``.

    This is the entry point called by the runner.  It constructs the
    appropriate backend from the config definition, wraps it in a
    ``CustomEvaluatorRunner``, and runs the evaluation.
    """
    import inspect as _inspect

    from .config import CodeEvaluatorDef, OpenAIEvalDef, RemoteEvaluatorDef
    from .runner import MetricResult

    if isinstance(evaluator_def, OpenAIEvalDef):
        from .openai_eval_backend import evaluate_openai_eval

        return await evaluate_openai_eval(evaluator_def, actual_invocations, expected_invocations)

    if isinstance(evaluator_def, RemoteEvaluatorDef):
        from .evaluator.resolver import get_default_resolver

        evaluator_def = await get_default_resolver().resolve(evaluator_def)

    if isinstance(evaluator_def, CodeEvaluatorDef):
        evaluator_path = Path(evaluator_def.path)

        runtime: Runtime | None = None
        if evaluator_path.suffix == ".py":
            from .evaluator.venv import ensure_venv_async

            try:
                venv_python = await ensure_venv_async(evaluator_path)
            except Exception as exc:
                logger.error("Failed to set up venv for '%s': %s", evaluator_def.name, exc)
                return MetricResult(
                    metric_name=evaluator_def.name,
                    error=f"Dependency installation failed: {exc}",
                )
            if venv_python:
                runtime = PythonRuntime(python_path=venv_python)

        if runtime is not None:
            backend = SubprocessBackend(evaluator_path, evaluator_def.timeout, runtime=runtime)
        else:
            backend = create_executor(evaluator_def.executor, evaluator_path, evaluator_def.timeout)
    else:
        raise ValueError(f"Unsupported custom evaluator type: {type(evaluator_def).__name__}")

    evaluator_instance = CustomEvaluatorRunner(
        backend=backend,
        metric_name=evaluator_def.name,
        threshold=evaluator_def.threshold,
        config=evaluator_def.config,
    )

    try:
        if _inspect.iscoroutinefunction(evaluator_instance.evaluate_invocations):
            eval_result: EvaluationResult = await evaluator_instance.evaluate_invocations(
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )
        else:
            import asyncio

            eval_result: EvaluationResult = await asyncio.to_thread(
                evaluator_instance.evaluate_invocations,
                actual_invocations=actual_invocations,
                expected_invocations=expected_invocations,
            )

        per_inv_scores = [r.score for r in eval_result.per_invocation_results]

        return MetricResult(
            metric_name=evaluator_def.name,
            score=eval_result.overall_score,
            eval_status=eval_result.overall_eval_status.name,
            per_invocation_scores=per_inv_scores,
        )

    except Exception as exc:
        logger.exception("Failed to evaluate custom evaluator '%s'", evaluator_def.name)
        return MetricResult(
            metric_name=evaluator_def.name,
            error=str(exc),
        )
