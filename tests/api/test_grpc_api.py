"""The v1alpha1 gRPC API, served in process on an ephemeral port and called through real stubs."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import grpc
import pytest
from fastapi.testclient import TestClient
from google.protobuf import json_format, struct_pb2
from google.rpc import error_details_pb2, status_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
from opentelemetry.proto.trace.v1 import trace_pb2

from agentevals import __version__
from agentevals.api.app import create_app
from agentevals.api.rpc import convert, services
from agentevals.api.rpc.server import start_api_server, stop_api_server
from agentevals.api.rpc.services import ApiContext, Ports, SystemServicer
from agentevals.api.v1alpha1 import (
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
    views_pb2,
)
from agentevals.builtin_metrics import metric_catalog
from agentevals.otel.decode import decode_json_document
from agentevals.run.service import RunService
from agentevals.storage.models import Result, ResultStatus, Run, RunSpec, RunStatus, TraceTarget
from agentevals.storage.repos.memory import MemoryRepos
from agentevals.streaming import manager as manager_module
from agentevals.streaming.manager import LiveManager

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "otlp" / "hand" / "adk_go_main.json"
DOCUMENT = json.loads(FIXTURE.read_text())
TRACE_ID = "1dbd779ff94bf24c7b84678a5dc0346c"
PORTS = Ports(http=8001, otlp_http=4318, otlp_grpc=4317, api_grpc=8002)
# Not a hex string, so it cannot turn up inside a generated id by chance.
MARK = "qxmark"
SERVICES = [
    "agentevals.api.v1alpha1.SessionService",
    "agentevals.api.v1alpha1.EvaluationService",
    "agentevals.api.v1alpha1.RunService",
    "agentevals.api.v1alpha1.ConversionService",
    "agentevals.api.v1alpha1.SystemService",
]


@pytest.fixture
def repos():
    return MemoryRepos.create()


@pytest.fixture
def context(repos, monkeypatch):
    for name in ("AGENTEVALS_API_TRACES", "AGENTEVALS_GRPC_REFLECTION"):
        monkeypatch.delenv(name, raising=False)
    return ApiContext(manager=LiveManager(), run_service=RunService(repos.runs, repos.results), ports=PORTS)


@pytest.fixture
async def channel(context):
    api = await start_api_server(context, "127.0.0.1", 0)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{api.port}") as channel:
            yield channel
    finally:
        await stop_api_server(api)


def _document(content: bytes | None = None, name: str = "adk_go_main.json") -> common_pb2.TraceDocument:
    return common_pb2.TraceDocument(
        content=FIXTURE.read_bytes() if content is None else content,
        format=common_pb2.TRACE_FORMAT_OTLP_JSON,
        name=name,
    )


def _traces_data(document: dict) -> trace_pb2.TracesData:
    """The OTLP/JSON document as protobuf. OTLP/JSON writes ids as hex, the protobuf JSON mapping as base64."""
    data = {"resourceSpans": copy.deepcopy(document["resourceSpans"])}
    for resource_spans in data["resourceSpans"]:
        for scope_spans in resource_spans["scopeSpans"]:
            for span in scope_spans["spans"]:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if span.get(key):
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    return json_format.ParseDict(data, trace_pb2.TracesData())


def _error_info(error: grpc.aio.AioRpcError) -> error_details_pb2.ErrorInfo | None:
    for key, value in error.trailing_metadata() or ():
        if key == "grpc-status-details-bin":
            for detail in status_pb2.Status.FromString(value).details:
                info = error_details_pb2.ErrorInfo()
                if detail.Unpack(info):
                    return info
    return None


def _trailers(error: grpc.aio.AioRpcError) -> bytes:
    return b"".join(v if isinstance(v, bytes) else v.encode() for _, v in error.trailing_metadata() or ())


async def _load(manager: LiveManager, session_id: str, document: dict = DOCUMENT) -> None:
    decoded = decode_json_document(document, strict=True)
    manager.load_session(session_id, decoded.spans, decoded.logs)
    for _ in range(100):
        manager.tick()
        if not manager._running and not manager._pending:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("session recompute did not finish")


async def _read_until_synced(call) -> list[sessions_pb2.WatchSessionsResponse]:
    messages = []
    while True:
        message = await asyncio.wait_for(call.read(), 5)
        messages.append(message)
        if message.HasField("synced"):
            return messages


async def _stream(call) -> list:
    return [message async for message in call]


async def _read_to_end(call) -> list:
    messages = []
    while (message := await call.read()) is not grpc.aio.EOF:
        messages.append(message)
    return messages


async def _golden(channel) -> struct_pb2.Struct:
    response = await conversion_pb2_grpc.ConversionServiceStub(channel).ConvertTraces(
        conversion_pb2.ConvertTracesRequest(traces=common_pb2.TraceInput(documents=[_document()]))
    )
    return response.conversations[0].golden_adk_eval_set


def _trajectory_params() -> evaluation_pb2.EvalParams:
    return evaluation_pb2.EvalParams(
        evaluators=[
            evaluation_pb2.Evaluator(builtin=evaluation_pb2.BuiltinEvaluator(name="tool_trajectory_avg_score"))
        ],
        max_concurrent_evals=1,
    )


def _run_spec(**fields) -> runs_pb2.RunSpec:
    return runs_pb2.RunSpec(
        target=runs_pb2.TraceTarget(inline=_document()),
        eval_config=_trajectory_params(),
        **fields,
    )


class TestHealthAndReflection:
    async def test_every_service_reports_serving(self, channel):
        stub = health_pb2_grpc.HealthStub(channel)
        for name in ["", *SERVICES]:
            response = await stub.Check(health_pb2.HealthCheckRequest(service=name))
            assert response.status == health_pb2.HealthCheckResponse.SERVING, name

    async def test_reflection_is_off_by_default(self, channel):
        call = reflection_pb2_grpc.ServerReflectionStub(channel).ServerReflectionInfo(
            iter([reflection_pb2.ServerReflectionRequest(list_services="")])
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _stream(call)
        assert error.value.code() == grpc.StatusCode.UNIMPLEMENTED

    async def test_reflection_lists_the_services_when_enabled(self, context, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_GRPC_REFLECTION", "true")
        api = await start_api_server(context, "127.0.0.1", 0)
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{api.port}") as channel:
                call = reflection_pb2_grpc.ServerReflectionStub(channel).ServerReflectionInfo(
                    iter([reflection_pb2.ServerReflectionRequest(list_services="")])
                )
                [response] = await _stream(call)
        finally:
            await stop_api_server(api)
        names = {s.name for s in response.list_services_response.service}
        assert set(SERVICES) <= names


class TestPolicies:
    async def test_a_method_without_a_policy_refuses_startup(self, context, monkeypatch):
        class Unchecked(SystemServicer):
            async def GetVersion(self, request, context):  # noqa: N802
                return system_pb2.GetVersionResponse()

        real = services.servicers

        def with_unchecked(ctx):
            return [
                (Unchecked(ctx), register, service)
                if isinstance(servicer, SystemServicer)
                else (servicer, register, service)
                for servicer, register, service in real(ctx)
            ]

        monkeypatch.setattr("agentevals.api.rpc.server.servicers", with_unchecked)
        with pytest.raises(RuntimeError, match="SystemService methods without an rpc policy: GetVersion"):
            await start_api_server(context, "127.0.0.1", 0)

    async def test_unexpected_errors_are_masked(self, channel, caplog):
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]), params=_trajectory_params()
        )
        failing = AsyncMock(side_effect=RuntimeError(f"connection to {MARK}.internal refused"))
        with patch("agentevals.api.evaluation.run_evaluation_from_traces", failing):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))
        assert error.value.code() == grpc.StatusCode.INTERNAL
        assert error.value.details() == "internal server error"
        assert _error_info(error.value) is None
        assert MARK.encode() not in _trailers(error.value)
        assert MARK in caplog.text

    async def test_invalid_parameters_name_the_field_but_not_the_value(self, channel):
        params = evaluation_pb2.EvalParams(
            evaluators=[evaluation_pb2.Evaluator(code=evaluation_pb2.CodeEvaluator(name="c", path=f"/srv/{MARK}.exe"))]
        )
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]), params=params
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "evaluators.0" in error.value.details()
        assert MARK not in error.value.details()
        assert MARK.encode() not in _trailers(error.value)
        assert _error_info(error.value).reason == "INVALID_REQUEST"


class TestSessions:
    async def test_list_pages_newest_first(self, channel, context):
        for name in ("first", "second", "third"):
            await _load(context.manager, name)
        stub = sessions_pb2_grpc.SessionServiceStub(channel)
        page = await stub.ListSessions(sessions_pb2.ListSessionsRequest(page=common_pb2.PageRequest(limit=2)))
        assert [s.id for s in page.sessions] == ["third", "second"]
        rest = await stub.ListSessions(
            sessions_pb2.ListSessionsRequest(page=common_pb2.PageRequest(limit=2, page_token=page.page.next_page_token))
        )
        assert [s.id for s in rest.sessions] == ["first"]
        assert rest.page.next_page_token == ""

    @pytest.mark.parametrize("page", [common_pb2.PageRequest(limit=101), common_pb2.PageRequest(page_token="%%")])
    async def test_bad_paging_is_invalid_argument(self, channel, page):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await sessions_pb2_grpc.SessionServiceStub(channel).ListSessions(
                sessions_pb2.ListSessionsRequest(page=page)
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert _error_info(error.value).domain == "agentevals.api.v1alpha1"

    async def test_get_session_returns_the_turns(self, channel, context):
        await _load(context.manager, "weather")
        response = await sessions_pb2_grpc.SessionServiceStub(channel).GetSession(
            sessions_pb2.GetSessionRequest(id="weather")
        )
        summary, [turn] = response.session.summary, response.session.turns
        assert summary.state == views_pb2.SESSION_STATE_COMPLETE
        assert (summary.totals.usage.input_tokens, summary.totals.usage.output_tokens) == (90, 18)
        assert not summary.totals.usage.HasField("cache_read_input_tokens")
        assert turn.user_text == "What is the weather in Paris?"
        assert [t.name for t in turn.tool_calls] == ["get_weather"]
        assert json_format.MessageToDict(turn.tool_calls[0].arguments) == {"city": "Paris"}

    async def test_get_unknown_session_is_not_found(self, channel):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await sessions_pb2_grpc.SessionServiceStub(channel).GetSession(sessions_pb2.GetSessionRequest(id="nope"))
        assert error.value.code() == grpc.StatusCode.NOT_FOUND

    async def test_watch_sends_the_snapshot_then_synced_then_changes(self, channel, context):
        await _load(context.manager, "before")
        call = sessions_pb2_grpc.SessionServiceStub(channel).WatchSessions(
            sessions_pb2.WatchSessionsRequest(include_turns=True)
        )
        snapshot = await _read_until_synced(call)
        assert [m.upsert.summary.id for m in snapshot[:-1]] == ["before"]
        assert snapshot[0].upsert.turns[0].user_text == "What is the weather in Paris?"
        await _load(context.manager, "after")
        change = await asyncio.wait_for(call.read(), 5)
        assert change.upsert.summary.id == "after"
        call.cancel()

    async def test_watch_without_turns_sends_summaries(self, channel, context):
        await _load(context.manager, "weather")
        call = sessions_pb2_grpc.SessionServiceStub(channel).WatchSessions(sessions_pb2.WatchSessionsRequest())
        upsert = (await _read_until_synced(call))[0].upsert
        call.cancel()
        assert (len(upsert.turns), upsert.summary.totals.turns) == (0, 1)

    async def test_ui_stream_frames_equal_the_watch_messages(self, channel, context, monkeypatch):
        await _load(context.manager, "weather")
        call = sessions_pb2_grpc.SessionServiceStub(channel).WatchSessions(
            sessions_pb2.WatchSessionsRequest(include_turns=True)
        )
        watched = await _read_until_synced(call)
        call.cancel()

        mgr = context.manager
        subscribe = mgr.subscribe

        def subscribe_closed(**kwargs):
            # A closed watcher still hands out what it holds, so the stream ends after the snapshot.
            watcher = subscribe(**kwargs)
            watcher.close()
            return watcher

        monkeypatch.setattr(mgr, "subscribe", subscribe_closed)
        body = TestClient(create_app(enable_streaming=True, trace_manager=mgr)).get("/stream/ui-updates").text
        frames = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
        assert [json_format.Parse(f, sessions_pb2.WatchSessionsResponse()) for f in frames] == watched

    async def test_watchers_are_capped_and_released(self, channel, context, monkeypatch):
        monkeypatch.setattr(manager_module, "MAX_WATCHERS", 1)
        stub = sessions_pb2_grpc.SessionServiceStub(channel)
        call = stub.WatchSessions(sessions_pb2.WatchSessionsRequest())
        await _read_until_synced(call)

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _read_until_synced(stub.WatchSessions(sessions_pb2.WatchSessionsRequest()))
        assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert (
            TestClient(create_app(enable_streaming=True, trace_manager=context.manager))
            .get("/stream/ui-updates")
            .status_code
            == 503
        )

        call.cancel()
        for _ in range(100):
            if not context.manager.watchers:
                break
            await asyncio.sleep(0.01)
        assert context.manager.watchers == []


class TestEvaluation:
    async def test_list_metrics_is_the_catalog(self, channel):
        response = await evaluation_pb2_grpc.EvaluationServiceStub(channel).ListMetrics(
            evaluation_pb2.ListMetricsRequest()
        )
        assert [m.name for m in response.metrics] == [m.name for m in metric_catalog()]

    async def test_evaluate_traces_streams_until_done(self, channel):
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]),
            adk_eval_set=await _golden(channel),
            params=_trajectory_params(),
        )
        events = await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))
        kinds = [e.WhichOneof("event") for e in events]
        assert kinds[0] == "performance" and kinds[-1] == "done"
        assert kinds.index("trace_result") < kinds.index("done")
        [metric] = events[-1].done.trace_results[0].metric_results
        assert (metric.metric_name, metric.score, metric.eval_status) == (
            "tool_trajectory_avg_score",
            1.0,
            evaluation_pb2.EVAL_STATUS_PASSED,
        )
        assert events[0].performance.trace_id == TRACE_ID

    async def test_events_match_the_rest_stream(self, channel):
        golden = await _golden(channel)
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]), adk_eval_set=golden, params=_trajectory_params()
        )
        events = await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))

        body = (
            TestClient(create_app())
            .post(
                "/api/evaluate/json/stream",
                json={
                    "traces": DOCUMENT,
                    "evalSet": convert.from_struct(golden),
                    "config": {
                        "evaluators": [{"name": "tool_trajectory_avg_score", "type": "builtin"}],
                        "maxConcurrentEvals": 1,
                    },
                },
            )
            .text
        )
        frames = []
        for block in body.strip().split("\n\n"):
            lines = block.splitlines()
            data = json.loads(lines[-1].removeprefix("data: "))
            if lines[0] == "event: performance_metrics":
                frames.append(("performance", data))
            elif "message" in data:
                frames.append(("progress", data))
            elif "traceProgress" in data:
                frames.append(("trace_result", data))
            else:
                frames.append(("done", data))

        assert [e.WhichOneof("event") for e in events] == [kind for kind, _ in frames]
        assert [e.progress.message for e in events if e.HasField("progress")] == [
            data["message"] for kind, data in frames if kind == "progress"
        ]
        rest_metric = frames[-1][1]["result"]["traceResults"][0]["metricResults"][0]
        grpc_metric = events[-1].done.trace_results[0].metric_results[0]
        assert (rest_metric["metricName"], rest_metric["score"]) == (grpc_metric.metric_name, grpc_metric.score)

    async def test_unresolvable_credential_is_failed_precondition(self, channel):
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]),
            params=_trajectory_params(),
            credential_refs={"judge": convert.to_struct({"kind": f"{MARK}-resolver", "name": MARK})},
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert MARK not in error.value.details()

    async def test_evaluator_without_a_kind_is_invalid_argument(self, channel):
        request = evaluation_pb2.EvaluateTracesRequest(
            traces=common_pb2.TraceInput(documents=[_document()]),
            params=evaluation_pb2.EvalParams(evaluators=[evaluation_pb2.Evaluator()]),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _stream(evaluation_pb2_grpc.EvaluationServiceStub(channel).EvaluateTraces(request))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


class TestRuns:
    @pytest.mark.parametrize(
        ("method", "request_message"),
        [
            ("SubmitRun", runs_pb2.SubmitRunRequest()),
            ("GetRun", runs_pb2.GetRunRequest(run_id=str(uuid4()))),
            ("ListRuns", runs_pb2.ListRunsRequest()),
            ("CancelRun", runs_pb2.CancelRunRequest(run_id=str(uuid4()))),
            ("ListRunResults", runs_pb2.ListRunResultsRequest(run_id=str(uuid4()))),
            ("WatchRun", runs_pb2.WatchRunRequest(run_id=str(uuid4()))),
        ],
    )
    async def test_runs_need_storage(self, channel, context, method, request_message):
        context.run_service = None
        call = getattr(runs_pb2_grpc.RunServiceStub(channel), method)(request_message)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await (_stream(call) if method == "WatchRun" else call)
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        info = _error_info(error.value)
        assert (info.reason, info.domain) == ("STORAGE_NOT_CONFIGURED", "agentevals.api.v1alpha1")

    async def test_submit_then_get(self, channel):
        stub = runs_pb2_grpc.RunServiceStub(channel)
        submitted = (await stub.SubmitRun(runs_pb2.SubmitRunRequest(spec=_run_spec()))).run
        assert submitted.status == runs_pb2.RUN_STATUS_QUEUED
        fetched = (await stub.GetRun(runs_pb2.GetRunRequest(run_id=submitted.run_id))).run
        assert fetched == submitted
        assert json.loads(fetched.spec.target.inline.content) == DOCUMENT
        assert fetched.spec.eval_config.evaluators[0].builtin.name == "tool_trajectory_avg_score"

    async def test_an_otlp_target_is_stored_as_otlp_json(self, channel, repos):
        spec = runs_pb2.RunSpec(
            target=runs_pb2.TraceTarget(otlp=common_pb2.OtlpTelemetry(traces=_traces_data(DOCUMENT))),
            eval_config=_trajectory_params(),
        )
        run = (await runs_pb2_grpc.RunServiceStub(channel).SubmitRun(runs_pb2.SubmitRunRequest(spec=spec))).run
        stored = await repos.runs.get(UUID(run.run_id))
        assert stored.spec.target.trace_format == "otlp-json"
        spans = [s for rs in stored.spec.target.inline["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
        assert {s["traceId"] for s in spans} == {TRACE_ID}
        assert len(spans) == sum(len(ss["spans"]) for rs in DOCUMENT["resourceSpans"] for ss in rs["scopeSpans"])

    async def test_reads_never_return_secrets(self, channel, repos):
        spec = runs_pb2.RunSpec(
            target=runs_pb2.TraceTarget(
                http=runs_pb2.HttpTraceTarget(
                    base_url=f"https://reader:{MARK}@traces.example.com/api?key={MARK}", trace_id=TRACE_ID
                )
            ),
            eval_config=_trajectory_params(),
            context=runs_pb2.RunContext(headers={"Authorization": f"Bearer {MARK}"}),
            sinks=[
                convert.to_struct(
                    {
                        "kind": "http_webhook",
                        "url": f"https://hooks.example.com/services/{MARK}",
                        "headers": {"X-Api-Key": MARK, "X-Signature": {"from_env": "HOOK_SIGNATURE"}},
                    }
                )
            ],
        )
        stub = runs_pb2_grpc.RunServiceStub(channel)
        submitted = await stub.SubmitRun(runs_pb2.SubmitRunRequest(spec=spec))
        run_id = submitted.run.run_id
        responses = [
            submitted,
            await stub.GetRun(runs_pb2.GetRunRequest(run_id=run_id)),
            await stub.ListRuns(runs_pb2.ListRunsRequest()),
            await stub.CancelRun(runs_pb2.CancelRunRequest(run_id=run_id)),
        ]
        for response in responses:
            assert MARK.encode() not in response.SerializeToString(), type(response).__name__

        run = responses[1].run
        assert run.spec.target.http.base_url == "https://traces.example.com/api"
        assert run.spec.context.headers["Authorization"] == "REDACTED"
        sink = convert.from_struct(run.spec.sinks[0])
        assert sink["url"] == "https://hooks.example.com/REDACTED"
        assert sink["headers"] == {"X-Api-Key": "REDACTED", "X-Signature": {"from_env": "HOOK_SIGNATURE"}}

        stored = await repos.runs.get(UUID(run_id))
        assert stored.spec.context["headers"]["Authorization"] == f"Bearer {MARK}"

    async def test_conflict_names_the_run_without_its_spec(self, channel):
        stub = runs_pb2_grpc.RunServiceStub(channel)
        run_id = str(uuid4())
        first = _run_spec(context=runs_pb2.RunContext(headers={"Authorization": f"Bearer {MARK}"}))
        await stub.SubmitRun(runs_pb2.SubmitRunRequest(run_id=run_id, spec=first))
        assert (await stub.SubmitRun(runs_pb2.SubmitRunRequest(run_id=run_id, spec=first))).run.run_id == run_id

        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.SubmitRun(runs_pb2.SubmitRunRequest(run_id=run_id, spec=_run_spec()))
        assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
        info = _error_info(error.value)
        assert (info.reason, dict(info.metadata)) == ("RUN_SPEC_CONFLICT", {"run_id": run_id})
        assert MARK.encode() not in _trailers(error.value)

    @pytest.mark.parametrize("trace_id", ["../../admin", "abc", TRACE_ID + "0", f"{TRACE_ID[:16]}/x"])
    async def test_http_target_needs_a_hex_trace_id(self, channel, trace_id):
        spec = runs_pb2.RunSpec(
            target=runs_pb2.TraceTarget(
                http=runs_pb2.HttpTraceTarget(base_url="https://traces.example.com/api", trace_id=trace_id)
            ),
            eval_config=_trajectory_params(),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await runs_pb2_grpc.RunServiceStub(channel).SubmitRun(runs_pb2.SubmitRunRequest(spec=spec))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "trace_id" in error.value.details()

    async def test_uploaded_target_is_output_only(self, channel):
        spec = runs_pb2.RunSpec(target=runs_pb2.TraceTarget(uploaded=runs_pb2.UploadedTraces(trace_count=1)))
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await runs_pb2_grpc.RunServiceStub(channel).SubmitRun(runs_pb2.SubmitRunRequest(spec=spec))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    @pytest.mark.parametrize("run_id", ["", "not-a-uuid"])
    async def test_get_run_needs_a_uuid(self, channel, run_id):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await runs_pb2_grpc.RunServiceStub(channel).GetRun(runs_pb2.GetRunRequest(run_id=run_id))
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_get_unknown_run_is_not_found(self, channel):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await runs_pb2_grpc.RunServiceStub(channel).GetRun(runs_pb2.GetRunRequest(run_id=str(uuid4())))
        assert error.value.code() == grpc.StatusCode.NOT_FOUND

    async def test_list_runs_pages_newest_first_and_filters_by_status(self, channel, repos):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        ids = []
        for n, status in enumerate([RunStatus.QUEUED, RunStatus.SUCCEEDED, RunStatus.QUEUED]):
            run = Run(
                run_id=uuid4(),
                status=status,
                spec=RunSpec(target=TraceTarget(kind="inline", inline={"n": n})),
                created_at=start + timedelta(minutes=n),
            )
            await repos.runs.create(run)
            ids.append(str(run.run_id))
        stub = runs_pb2_grpc.RunServiceStub(channel)

        page = await stub.ListRuns(runs_pb2.ListRunsRequest(page=common_pb2.PageRequest(limit=2)))
        assert [r.run_id for r in page.runs] == [ids[2], ids[1]]
        rest = await stub.ListRuns(
            runs_pb2.ListRunsRequest(page=common_pb2.PageRequest(limit=2, page_token=page.page.next_page_token))
        )
        assert ([r.run_id for r in rest.runs], rest.page.next_page_token) == ([ids[0]], "")

        done = await stub.ListRuns(runs_pb2.ListRunsRequest(statuses=[runs_pb2.RUN_STATUS_SUCCEEDED]))
        assert [r.run_id for r in done.runs] == [ids[1]]

    async def test_cancel_a_queued_run(self, channel):
        stub = runs_pb2_grpc.RunServiceStub(channel)
        run = (await stub.SubmitRun(runs_pb2.SubmitRunRequest(spec=_run_spec()))).run
        cancelled = (await stub.CancelRun(runs_pb2.CancelRunRequest(run_id=run.run_id))).run
        assert (cancelled.status, cancelled.cancel_requested) == (runs_pb2.RUN_STATUS_CANCELLED, True)
        assert cancelled.HasField("finished_at")

    async def test_results_keep_unscored_invocations_in_place(self, channel, repos):
        stub = runs_pb2_grpc.RunServiceStub(channel)
        run_id = UUID((await stub.SubmitRun(runs_pb2.SubmitRunRequest(spec=_run_spec()))).run.run_id)
        await repos.results.upsert_many(run_id, [_result(run_id, n, [0.5, None, 1.0]) for n in range(3)])

        page = await stub.ListRunResults(
            runs_pb2.ListRunResultsRequest(run_id=str(run_id), page=common_pb2.PageRequest(limit=2))
        )
        assert [r.result_id for r in page.results] == ["r0", "r1"]
        assert [(i.HasField("score"), i.score) for i in page.results[0].per_invocation] == [
            (True, 0.5),
            (False, 0.0),
            (True, 1.0),
        ]
        rest = await stub.ListRunResults(
            runs_pb2.ListRunResultsRequest(
                run_id=str(run_id), page=common_pb2.PageRequest(limit=2, page_token=page.page.next_page_token)
            )
        )
        assert ([r.result_id for r in rest.results], rest.page.next_page_token) == (["r2"], "")

    async def test_watch_run_streams_each_change_until_the_run_ends(self, channel, repos, monkeypatch):
        monkeypatch.setattr(services, "WATCH_RUN_POLL_SECONDS", 0.01)
        stub = runs_pb2_grpc.RunServiceStub(channel)
        run_id = UUID((await stub.SubmitRun(runs_pb2.SubmitRunRequest(spec=_run_spec()))).run.run_id)
        call = stub.WatchRun(runs_pb2.WatchRunRequest(run_id=str(run_id)))

        first = await asyncio.wait_for(call.read(), 5)
        assert (first.run.status, len(first.results)) == (runs_pb2.RUN_STATUS_QUEUED, 0)
        await repos.runs.claim_next(worker_id="w", lease=timedelta(seconds=30), max_attempts=3)
        await repos.results.upsert_many(run_id, [_result(run_id, 0, [1.0])])
        await repos.runs.update_status(run_id, RunStatus.SUCCEEDED, summary={"passed": 1})

        updates = [first, *await asyncio.wait_for(_read_to_end(call), 5)]
        statuses = [u.run.status for u in updates]
        assert statuses[-1] == runs_pb2.RUN_STATUS_SUCCEEDED
        assert statuses == sorted(statuses)
        assert [r.result_id for u in updates for r in u.results] == ["r0"]
        assert convert.from_struct(updates[-1].run.summary) == {"passed": 1}

    async def test_watch_unknown_run_is_not_found(self, channel):
        call = runs_pb2_grpc.RunServiceStub(channel).WatchRun(runs_pb2.WatchRunRequest(run_id=str(uuid4())))
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await _stream(call)
        assert error.value.code() == grpc.StatusCode.NOT_FOUND


def _result(run_id: UUID, n: int, scores: list[float | None]) -> Result:
    return Result(
        result_id=f"r{n}",
        run_id=run_id,
        eval_set_item_id=f"item-{n}",
        eval_set_item_name=f"trace-{n}",
        evaluator_name="tool_trajectory_avg_score",
        evaluator_type="builtin",
        status=ResultStatus.PASSED,
        score=1.0,
        per_invocation_scores=scores,
    )


class TestConversion:
    async def test_convert_returns_turns_and_a_golden_eval_set(self, channel):
        response = await conversion_pb2_grpc.ConversionServiceStub(channel).ConvertTraces(
            conversion_pb2.ConvertTracesRequest(traces=common_pb2.TraceInput(documents=[_document()]))
        )
        [converted] = response.conversations
        assert [t.user_text for t in converted.conversation.turns] == ["What is the weather in Paris?"]
        golden = convert.from_struct(converted.golden_adk_eval_set)
        response_data = golden["eval_cases"][0]["conversation"][0]["intermediate_data"]["tool_responses"][0]
        assert response_data["response"]["temp_c"] == 21
        assert isinstance(response_data["response"]["temp_c"], int)
        assert list(response.warnings) == []

    async def test_otlp_protobuf_input_equals_the_json_document(self, channel):
        stub = conversion_pb2_grpc.ConversionServiceStub(channel)
        from_json = await stub.ConvertTraces(
            conversion_pb2.ConvertTracesRequest(traces=common_pb2.TraceInput(documents=[_document()]))
        )
        from_proto = await stub.ConvertTraces(
            conversion_pb2.ConvertTracesRequest(
                traces=common_pb2.TraceInput(otlp=common_pb2.OtlpTelemetry(traces=_traces_data(DOCUMENT)))
            )
        )
        assert from_proto.conversations == from_json.conversations
        assert len(from_json.conversations[0].conversation.turns) == 1

    async def test_an_unreadable_document_is_a_warning(self, channel):
        response = await conversion_pb2_grpc.ConversionServiceStub(channel).ConvertTraces(
            conversion_pb2.ConvertTracesRequest(
                traces=common_pb2.TraceInput(documents=[_document(b"\xff not json", name="broken.json"), _document()])
            )
        )
        assert len(response.conversations) == 1
        [warning] = response.warnings
        assert warning.startswith("Failed to load broken.json")

    async def test_an_oversized_document_is_invalid_argument(self, channel):
        big = _document(b" " * (convert.MAX_DOCUMENT_BYTES + 1), name="big.json")
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await conversion_pb2_grpc.ConversionServiceStub(channel).ConvertTraces(
                conversion_pb2.ConvertTracesRequest(traces=common_pb2.TraceInput(documents=[big]))
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "big.json" in error.value.details()


class TestSystem:
    async def test_get_version(self, channel):
        response = await system_pb2_grpc.SystemServiceStub(channel).GetVersion(system_pb2.GetVersionRequest())
        assert response.version == __version__

    async def test_get_config(self, channel, context, monkeypatch):
        for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", MARK)
        response = await system_pb2_grpc.SystemServiceStub(channel).GetConfig(system_pb2.GetConfigRequest())
        assert (response.ports.http, response.ports.otlp_http, response.ports.otlp_grpc, response.ports.api_grpc) == (
            8001,
            4318,
            4317,
            8002,
        )
        limits = context.manager.store.limits
        assert response.live_limits.max_sessions == limits.max_sessions
        assert response.live_limits.idle_timeout.ToTimedelta() == timedelta(seconds=limits.idle_timeout_seconds)
        assert response.runs_enabled is True
        assert (response.judge_keys.openai, response.judge_keys.google, response.judge_keys.anthropic) == (
            True,
            False,
            False,
        )
        assert MARK.encode() not in response.SerializeToString()
