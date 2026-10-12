"""The five v1alpha1 services. Each method only translates: the work happens in the same code the
REST API and the CLI use."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import grpc

from ... import __version__
from ...adk_bridge import eval_set_from_turns, load_eval_set_from_dict
from ...builtin_metrics import metric_catalog
from ...genai.extract import extract_conversation
from ...run.service import RunService, RunSubmitConflict
from ...runner import evaluation_groups
from ...storage.models import RunStatus
from ...streaming.manager import LiveManager, TooManyWatchers
from ..evaluation import (
    CredentialResolutionError,
    Done,
    Evaluation,
    EvaluationEvent,
    GroupResult,
    Performance,
    Progress,
    stream_evaluation,
)
from ..v1alpha1 import (
    common_pb2,
    conversion_pb2,
    conversion_pb2_grpc,
    evaluation_pb2,
    evaluation_pb2_grpc,
    runs_pb2,
    runs_pb2_grpc,
    sessions_pb2,
    sessions_pb2_grpc,
    system_pb2,
    system_pb2_grpc,
)
from ..views import conversation_view
from . import convert
from .policy import REASON_RUN_SPEC_CONFLICT, ApiError, rpc, validation_message

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
WATCH_RUN_POLL_SECONDS = 1.0
# A watch ends after this long even when the run has not finished; the caller watches again.
WATCH_RUN_MAX_SECONDS = 3600.0
TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class Ports:
    http: int = 0
    otlp_http: int = 0
    otlp_grpc: int = 0
    api_grpc: int = 0


@dataclass(slots=True)
class ApiContext:
    manager: LiveManager
    run_service: RunService | None = None
    ports: Ports = Ports()


# ---------------------------------------------------------------- paging


def _page_size(page: common_pb2.PageRequest) -> int:
    if page.limit < 0 or page.limit > MAX_PAGE_SIZE:
        raise ApiError.invalid(f"page.limit must be between 0 and {MAX_PAGE_SIZE}")
    return page.limit or DEFAULT_PAGE_SIZE


def _token(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def _from_token(token: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode()))
    except ValueError as exc:
        raise ApiError.invalid("page.page_token is not valid") from exc
    if not isinstance(data, dict):
        raise ApiError.invalid("page.page_token is not valid")
    return data


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ApiError.invalid(f"{field} must be a UUID") from exc


# ---------------------------------------------------------------- sessions


class SessionServicer(sessions_pb2_grpc.SessionServiceServicer):
    def __init__(self, context: ApiContext) -> None:
        self._manager = context.manager

    @rpc()
    async def ListSessions(self, request, context):  # noqa: N802
        size = _page_size(request.page)
        ordered = sorted(self._manager.sessions.values(), key=lambda s: (s.started_at, s.session_id), reverse=True)
        if request.page.page_token:
            after = _from_token(request.page.page_token)
            last = (str(after.get("t", "")), str(after.get("id", "")))
            ordered = [s for s in ordered if (s.started_at.isoformat(), s.session_id) < last]
        page = ordered[:size]
        response = sessions_pb2.ListSessionsResponse(
            sessions=[self._manager.view(s).summary_only.upsert.summary for s in page]
        )
        if len(ordered) > size:
            response.page.next_page_token = _token({"t": page[-1].started_at.isoformat(), "id": page[-1].session_id})
        return response

    @rpc()
    async def GetSession(self, request, context):  # noqa: N802
        session = self._manager.sessions.get(request.id)
        if session is None:
            raise ApiError.not_found(f"session {request.id} not found")
        return sessions_pb2.GetSessionResponse(session=self._manager.view(session).with_turns.upsert)

    @rpc()
    async def WatchSessions(self, request, context):  # noqa: N802
        try:
            watcher = self._manager.subscribe(include_turns=request.include_turns)
        except TooManyWatchers as exc:
            raise ApiError(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc)) from exc
        try:
            while (event := await watcher.next()) is not None:
                yield watcher.response(event)
        finally:
            self._manager.unsubscribe(watcher)


# ---------------------------------------------------------------- evaluation


def _evaluation_response(event: EvaluationEvent) -> evaluation_pb2.EvaluateTracesResponse:
    if isinstance(event, Progress):
        return evaluation_pb2.EvaluateTracesResponse(progress=evaluation_pb2.EvaluationProgress(message=event.message))
    if isinstance(event, Performance):
        return evaluation_pb2.EvaluateTracesResponse(
            performance=evaluation_pb2.GroupPerformance(
                trace_id=event.trace_id,
                performance_metrics=convert.to_struct(event.performance_metrics),
                trace_metadata=convert.to_struct(event.trace_metadata),
            )
        )
    if isinstance(event, GroupResult):
        return evaluation_pb2.EvaluateTracesResponse(trace_result=convert.trace_result_to_pb(event.result))
    assert isinstance(event, Done)
    return evaluation_pb2.EvaluateTracesResponse(done=convert.run_result_to_pb(event.result))


class EvaluationServicer(evaluation_pb2_grpc.EvaluationServiceServicer):
    def __init__(self, context: ApiContext) -> None:
        self._context = context

    @rpc()
    async def ListMetrics(self, request, context):  # noqa: N802
        return evaluation_pb2.ListMetricsResponse(
            metrics=[evaluation_pb2.Metric(**dataclasses.asdict(m)) for m in metric_catalog()]
        )

    @rpc()
    async def EvaluateTraces(self, request, context):  # noqa: N802
        traces, load_errors = await asyncio.to_thread(convert.load_trace_input, request.traces)
        eval_set_dict = convert.from_struct(request.adk_eval_set) if request.HasField("adk_eval_set") else None
        evaluation = Evaluation(
            traces=traces,
            params=convert.eval_params_from_pb(request.params),
            eval_set=load_eval_set_from_dict(eval_set_dict) if eval_set_dict is not None else None,
            credential_refs={name: convert.from_struct(ref) for name, ref in request.credential_refs.items()} or None,
            load_errors=load_errors,
            eval_set_dict=eval_set_dict,
        )
        try:
            async for event in stream_evaluation(evaluation, run_service=self._context.run_service):
                yield _evaluation_response(event)
        except CredentialResolutionError as exc:
            logger.info("EvaluateTraces: %s", exc)
            raise ApiError(grpc.StatusCode.FAILED_PRECONDITION, "a credential reference could not be resolved") from exc


# ---------------------------------------------------------------- runs


class RunServicer(runs_pb2_grpc.RunServiceServicer):
    def __init__(self, context: ApiContext) -> None:
        self._context = context

    @property
    def run_service(self) -> RunService | None:
        return self._context.run_service

    async def _run(self, run_id: str):
        run = await self.run_service.get(_uuid(run_id, "run_id"))
        if run is None:
            raise ApiError.not_found(f"run {run_id} not found")
        return run

    @rpc(storage=True)
    async def SubmitRun(self, request, context):  # noqa: N802
        run_id = _uuid(request.run_id, "run_id") if request.run_id else None
        spec = convert.run_spec_from_pb(request.spec)
        try:
            run = await self.run_service.submit(run_id=run_id, spec=spec)
        except RunSubmitConflict as exc:
            raise ApiError(
                grpc.StatusCode.ALREADY_EXISTS,
                "run_id already exists with a different spec",
                REASON_RUN_SPEC_CONFLICT,
                {"run_id": str(exc.persisted.run_id)},
            ) from exc
        except ValueError as exc:
            cause = exc.__cause__
            message = validation_message(cause) if hasattr(cause, "errors") else str(exc)
            raise ApiError.invalid(message) from exc
        return runs_pb2.SubmitRunResponse(run=convert.run_to_pb(run))

    @rpc(storage=True)
    async def GetRun(self, request, context):  # noqa: N802
        return runs_pb2.GetRunResponse(run=convert.run_to_pb(await self._run(request.run_id)))

    @rpc(storage=True)
    async def ListRuns(self, request, context):  # noqa: N802
        size = _page_size(request.page)
        before = None
        if request.page.page_token:
            try:
                before = datetime.fromisoformat(str(_from_token(request.page.page_token).get("before", "")))
            except ValueError as exc:
                raise ApiError.invalid("page.page_token is not valid") from exc
        statuses = [convert.RUN_STATUS_FROM_PB[s] for s in request.statuses if s in convert.RUN_STATUS_FROM_PB]
        runs = await self.run_service.list(status=statuses or None, limit=size + 1, before=before)
        response = runs_pb2.ListRunsResponse(runs=[convert.run_to_pb(r) for r in runs[:size]])
        if len(runs) > size:
            response.page.next_page_token = _token({"before": runs[size - 1].created_at.isoformat()})
        return response

    @rpc(storage=True)
    async def CancelRun(self, request, context):  # noqa: N802
        run_id = _uuid(request.run_id, "run_id")
        await self.run_service.cancel(run_id)
        return runs_pb2.CancelRunResponse(run=convert.run_to_pb(await self._run(request.run_id)))

    @rpc(storage=True)
    async def ListRunResults(self, request, context):  # noqa: N802
        size = _page_size(request.page)
        run = await self._run(request.run_id)
        offset = int(_from_token(request.page.page_token).get("offset", 0)) if request.page.page_token else 0
        results = await self.run_service.list_results(run.run_id)
        page = results[offset : offset + size]
        response = runs_pb2.ListRunResultsResponse(results=[convert.result_to_pb(r) for r in page])
        if offset + size < len(results):
            response.page.next_page_token = _token({"offset": offset + size})
        return response

    @rpc(storage=True)
    async def WatchRun(self, request, context):  # noqa: N802
        run = await self._run(request.run_id)
        seen: dict[str, dict[str, Any]] = {}
        last_state = None
        deadline = time.monotonic() + WATCH_RUN_MAX_SECONDS
        while True:
            results = await self.run_service.list_results(run.run_id)
            changed = []
            for result in results:
                content = result.model_dump(mode="json")
                if seen.get(result.result_id) != content:
                    seen[result.result_id] = content
                    changed.append(result)
            state = (run.status, run.attempt, run.error, run.finished_at, run.cancel_requested)
            if state != last_state or changed:
                last_state = state
                yield runs_pb2.WatchRunResponse(
                    run=convert.run_to_pb(run), results=[convert.result_to_pb(r) for r in changed]
                )
            if run.status in TERMINAL_RUN_STATUSES or time.monotonic() > deadline:
                return
            await asyncio.sleep(WATCH_RUN_POLL_SECONDS)
            run = await self._run(request.run_id)


# ---------------------------------------------------------------- conversion


def _convert(traces, group_by: str) -> list[conversion_pb2.ConvertedConversation]:
    out = []
    for key, members in evaluation_groups(traces, group_by):
        conversation = extract_conversation(members, key)
        spans = {(s.trace_id, s.span_id): s for t in members for s in t.spans.values()}
        out.append(
            conversion_pb2.ConvertedConversation(
                conversation=conversation_view(conversation, spans),
                golden_adk_eval_set=convert.to_struct(eval_set_from_turns(key, conversation.turns)),
            )
        )
    return out


class ConversionServicer(conversion_pb2_grpc.ConversionServiceServicer):
    def __init__(self, context: ApiContext) -> None:
        pass

    @rpc()
    async def ConvertTraces(self, request, context):  # noqa: N802
        traces, warnings = await asyncio.to_thread(convert.load_trace_input, request.traces)
        group_by = convert.GROUP_BY_FROM_PB.get(request.group_by, "trace")
        conversations = await asyncio.to_thread(_convert, traces, group_by)
        return conversion_pb2.ConvertTracesResponse(conversations=conversations, warnings=warnings)


# ---------------------------------------------------------------- system


class SystemServicer(system_pb2_grpc.SystemServiceServicer):
    def __init__(self, context: ApiContext) -> None:
        self._context = context

    @rpc()
    async def GetVersion(self, request, context):  # noqa: N802
        return system_pb2.GetVersionResponse(version=__version__)

    @rpc()
    async def GetConfig(self, request, context):  # noqa: N802
        from ... import evaluation_events

        ports, limits = self._context.ports, self._context.manager.store.limits
        response = system_pb2.GetConfigResponse(
            ports=system_pb2.Ports(
                http=ports.http, otlp_http=ports.otlp_http, otlp_grpc=ports.otlp_grpc, api_grpc=ports.api_grpc
            ),
            live_limits=system_pb2.LiveLimits(
                max_sessions=limits.max_sessions,
                spans_per_trace=limits.spans_per_trace,
                spans_per_session=limits.spans_per_session,
                traces_per_session=limits.traces_per_session,
                max_bytes=limits.max_bytes,
            ),
            runs_enabled=self._context.run_service is not None,
            evaluation_events_enabled=evaluation_events.enabled(),
            judge_keys=system_pb2.JudgeKeys(
                google=bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
                openai=bool(os.environ.get("OPENAI_API_KEY")),
            ),
        )
        response.live_limits.completion_grace.FromNanoseconds(int(limits.completion_grace_seconds * 1e9))
        response.live_limits.idle_timeout.FromNanoseconds(int(limits.idle_timeout_seconds * 1e9))
        response.live_limits.session_ttl.FromNanoseconds(int(limits.session_ttl_seconds * 1e9))
        return response


def servicers(context: ApiContext) -> list[tuple[Any, Any, Any]]:
    """Every service as (servicer, registration function, service descriptor)."""
    return [
        (
            SessionServicer(context),
            sessions_pb2_grpc.add_SessionServiceServicer_to_server,
            sessions_pb2.DESCRIPTOR.services_by_name["SessionService"],
        ),
        (
            EvaluationServicer(context),
            evaluation_pb2_grpc.add_EvaluationServiceServicer_to_server,
            evaluation_pb2.DESCRIPTOR.services_by_name["EvaluationService"],
        ),
        (
            RunServicer(context),
            runs_pb2_grpc.add_RunServiceServicer_to_server,
            runs_pb2.DESCRIPTOR.services_by_name["RunService"],
        ),
        (
            ConversionServicer(context),
            conversion_pb2_grpc.add_ConversionServiceServicer_to_server,
            conversion_pb2.DESCRIPTOR.services_by_name["ConversionService"],
        ),
        (
            SystemServicer(context),
            system_pb2_grpc.add_SystemServiceServicer_to_server,
            system_pb2.DESCRIPTOR.services_by_name["SystemService"],
        ),
    ]
