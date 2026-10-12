"""Trace evaluation as a stream of events, shared by the REST evaluate routes and the gRPC API.

The evaluation runs in its own task with the request's credentials, so the resolved secrets
never leak into the caller's context and a stream that is closed early cancels the work. The
consumer always receives a terminal :class:`Done` or the evaluation's exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from google.adk.evaluation.eval_set import EvalSet

from ..config import EvalParams
from ..genai.extract import extract_conversation
from ..otel.model import Trace
from ..resolvers import reset_resolved_credentials, resolve_credential_refs, set_resolved_credentials
from ..runner import RunResult, TraceResult, evaluation_groups, run_evaluation_from_traces
from ..trace_metrics import extract_performance_metrics, extract_trace_metadata

if TYPE_CHECKING:
    from ..run.service import RunService

logger = logging.getLogger(__name__)


class CredentialResolutionError(ValueError):
    """A credential reference of the request could not be resolved."""


@dataclass
class Progress:
    message: str


@dataclass
class Performance:
    """One evaluation group's performance metrics, before it is evaluated."""

    trace_id: str
    performance_metrics: dict[str, Any]
    trace_metadata: dict[str, Any]


@dataclass
class GroupResult:
    """A group's results so far. A copy: the runner keeps appending to its own object."""

    result: TraceResult


@dataclass
class Done:
    result: RunResult


EvaluationEvent = Progress | Performance | GroupResult | Done


@dataclass
class Evaluation:
    traces: list[Trace]
    params: EvalParams
    eval_set: EvalSet | None = None
    credential_refs: dict[str, dict[str, Any]] | None = None
    load_errors: list[str] = field(default_factory=list)
    # Recorded with the run when storage is configured.
    eval_set_dict: dict[str, Any] | None = None
    trace_format: str | None = None
    upload_filenames: list[str] | None = None


def performance_events(traces: list[Trace], group_by: str, eval_set: EvalSet | None) -> list[Performance]:
    events = []
    for key, members in evaluation_groups(traces, group_by, eval_set):
        try:
            conversation = extract_conversation(members, key)
            events.append(
                Performance(
                    trace_id=members[0].trace_id,
                    performance_metrics=extract_performance_metrics(conversation, members),
                    trace_metadata=extract_trace_metadata(members, conversation),
                )
            )
        except Exception:
            logger.exception("Failed to extract early performance metrics for %s", key)
    return events


async def stream_evaluation(
    evaluation: Evaluation, *, run_service: RunService | None = None
) -> AsyncIterator[EvaluationEvent]:
    """Performance per group, then progress and group results, then :class:`Done`.

    Raises :class:`CredentialResolutionError` after the performance events when a credential
    reference cannot be resolved; nothing is evaluated then.
    """
    for event in performance_events(evaluation.traces, evaluation.params.group_by, evaluation.eval_set):
        yield event
    resolved = await _resolve(evaluation.credential_refs)

    queue: asyncio.Queue[EvaluationEvent | BaseException] = asyncio.Queue()

    async def on_progress(message: str) -> None:
        await queue.put(Progress(message))

    async def on_group(result: TraceResult) -> None:
        await queue.put(GroupResult(result.model_copy(deep=True)))

    async def run() -> None:
        token = set_resolved_credentials(resolved) if resolved else None
        try:
            if evaluation.traces:
                result = await run_evaluation_from_traces(
                    traces=evaluation.traces,
                    config=evaluation.params,
                    eval_set=evaluation.eval_set,
                    progress_callback=on_progress,
                    trace_progress_callback=on_group,
                )
            else:
                result = RunResult(errors=["No traces loaded."])
            if evaluation.load_errors:
                result.errors = evaluation.load_errors + result.errors
            await queue.put(Done(result))
        except Exception as exc:
            await queue.put(exc)
        finally:
            if token is not None:
                reset_resolved_credentials(token)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, Done):
                run_id = await persist_evaluation(run_service, evaluation, item.result)
                if run_id:
                    item.result.run_id = run_id
                yield item
                return
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _resolve(refs: dict[str, dict[str, Any]] | None) -> dict[str, str] | None:
    if not refs:
        return None
    try:
        return await resolve_credential_refs(refs)
    except ValueError as exc:
        raise CredentialResolutionError(str(exc)) from exc


async def evaluate(evaluation: Evaluation, *, run_service: RunService | None = None) -> RunResult:
    async for event in stream_evaluation(evaluation, run_service=run_service):
        if isinstance(event, Done):
            return event.result
    raise RuntimeError("evaluation ended without a result")


async def persist_evaluation(
    run_service: RunService | None,
    evaluation: Evaluation,
    result: RunResult,
) -> str | None:
    """Record a finished evaluation as a run when storage is configured. Best effort: the caller
    gets its result even when recording fails."""
    if run_service is None:
        return None
    try:
        run = await run_service.record_eval_run(
            params=evaluation.params,
            eval_set_dict=evaluation.eval_set_dict,
            trace_format=evaluation.trace_format,
            upload_filenames=evaluation.upload_filenames,
            run_result=result,
        )
        return str(run.run_id)
    except Exception:
        logger.exception("Failed to record the evaluation as a run; the result is still returned")
        return None
