"""Live manager: session views of running sessions and keeping the event loop free."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agentevals.api.v1alpha1 import views_pb2
from agentevals.api.views import SessionFacts, session_view
from agentevals.genai.extract import extract_conversation
from agentevals.genai.model import Conversation
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from agentevals.streaming import manager as manager_module
from agentevals.streaming.manager import LiveManager
from otel.builders import agent, assistant, chat, named, request, span, user


def _conversation(*spans: dict) -> tuple[Conversation, dict]:
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    spans_by_id = {(s.trace_id, s.span_id): s for t in traces for s in t.spans.values()}
    return extract_conversation(traces, "s"), spans_by_id


def _tool_layer(span_id: str, parent: str, start: int, **attrs) -> dict:
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search", **attrs}
    return span(span_id=span_id, parent=parent, attrs=attrs, start=start, end=start + 500)


FRAMEWORK = _tool_layer("fw", "a1", 1_000, **{"gen_ai.tool.call.id": "x1"})
CLIENT = _tool_layer("client", "fw", 1_010, **{"gen_ai.tool.call.result": '{"hits": 3}'})
SERVER = _tool_layer("server", "client", 1_020, **{"gen_ai.tool.call.result": '{"hits": 3}'})


class TestToolLayers:
    @pytest.mark.parametrize(
        ("early", "late"),
        [
            pytest.param([FRAMEWORK, CLIENT], [SERVER], id="server-last"),
            pytest.param([CLIENT, SERVER], [FRAMEWORK], id="framework-last"),
        ],
    )
    def test_layers_of_one_call_arriving_apart_are_one_call(self, early, late):
        root = agent(span_id="a1", start=0, end=10_000)
        facts = SessionFacts(
            session_id="s", received_at=datetime.now(UTC), is_complete=False, trace_ids=(), span_count=0
        )
        partial = session_view(facts, *_conversation(root, *early))
        assert sum(len(t.tool_calls) for t in partial.turns) <= 1
        turn = session_view(facts, *_conversation(root, *early, *late)).turns[0]
        assert [t.name for t in turn.tool_calls] == ["search"]
        assert turn.tool_calls[0].HasField("result")


class TestSessionList:
    async def test_a_running_session_lists_its_turns(self):
        mgr = LiveManager()
        mgr.start()
        waiting = chat(
            span_id="c1", parent="not-yet-exported", inputs=[user("still there?")], outputs=[assistant("yes")]
        )
        await mgr.ingest_spans(decode_json_document(request([waiting], named("open")), strict=True))
        for _ in range(100):
            session = mgr.view(mgr.sessions["open"]).with_turns.upsert
            if session.turns:
                break
            await asyncio.sleep(0.02)
        await mgr.shutdown()
        assert session.summary.state == views_pb2.SESSION_STATE_LIVE
        assert [t.user_text for t in session.turns] == ["still there?"]


class TestEventLoop:
    async def test_ingest_hands_the_loop_back_during_a_large_export(self, monkeypatch):
        monkeypatch.setattr(manager_module, "YIELD_INTERVAL_SECONDS", 0.0)
        spans = [chat(trace=f"t{n}", span_id=f"c{n}") for n in range(2_000)]
        decoded = decode_json_document(request(spans), strict=True)
        mgr = LiveManager()
        ticks = 0
        stop = asyncio.Event()

        async def other_work():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0)

        worker = asyncio.create_task(other_work())
        await asyncio.sleep(0)
        before = ticks
        await mgr.ingest_spans(decoded)
        stop.set()
        await worker
        assert ticks - before > 100

    @pytest.mark.parametrize("units", [1, 50])
    async def test_each_routing_unit_applies_whole(self, units):
        mgr = LiveManager()
        spans = [chat(trace=f"t{n % units}", span_id=f"c{n}") for n in range(200)]
        result = await mgr.ingest_spans(decode_json_document(request(spans, named("all")), strict=True))
        assert result.accepted == 200
        assert mgr.sessions["all"].span_count == 200
