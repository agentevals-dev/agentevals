"""Live manager: the append only UI diff, broadcast back pressure and keeping the event loop free."""

from __future__ import annotations

import asyncio

import pytest

from agentevals.genai.extract import extract_conversation
from agentevals.genai.model import Conversation, LlmCall, Turn, Usage
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import SpanRef, build_traces
from agentevals.streaming import manager as manager_module
from agentevals.streaming.manager import LiveManager, LiveState, live_events
from otel.builders import agent, assistant, chat, named, request, sid, tid, tool_result, user


def _conversation(*spans: dict) -> Conversation:
    decoded = decode_json_document(request(list(spans)), strict=True)
    traces, _ = build_traces(decoded.spans, decoded.logs)
    return extract_conversation(traces, "s")


def _kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


class TestLiveEvents:
    def test_a_late_agent_span_does_not_repeat_elements(self):
        state = LiveState()
        call = chat(span_id="c1", parent="a1", inputs=[user("Roll a die")], outputs=[assistant("4")])
        first = live_events("s", _conversation(call), state, incomplete={tid("t1")})
        assert _kinds(first) == ["user_input", "agent_response", "token_update"]

        second = live_events("s", _conversation(agent(span_id="a1"), call), state, incomplete={tid("t1")})
        assert second == []

    def test_a_late_wrapper_span_does_not_repeat_the_response(self):
        state = LiveState()
        leaf = chat(span_id="p1", parent="w1", outputs=[assistant("4")])
        live_events("s", _conversation(leaf), state, incomplete=set())
        wrapper = chat(span_id="w1", outputs=[assistant("4")])
        assert "agent_response" not in _kinds(live_events("s", _conversation(wrapper, leaf), state, incomplete=set()))

    def test_tool_call_and_result_are_sent_once(self):
        state = LiveState()
        spans = [
            chat(
                span_id="c1",
                inputs=[user("roll")],
                outputs=[assistant(tool_calls=[("call_1", "roll_die", {"sides": 6})])],
            ),
        ]
        assert _kinds(live_events("s", _conversation(*spans), state, set())).count("tool_call") == 1
        spans.append(
            chat(
                span_id="c2",
                start=3_000,
                end=4_000,
                inputs=[
                    user("roll"),
                    assistant(tool_calls=[("call_1", "roll_die", {"sides": 6})]),
                    tool_result("call_1", "roll_die", 5),
                ],
                outputs=[assistant("You rolled 5")],
            )
        )
        kinds = _kinds(live_events("s", _conversation(*spans), state, set()))
        assert kinds.count("tool_call") == 0
        assert kinds.count("tool_result") == 1

    def test_invocation_id_is_the_trace_until_the_trace_completes(self):
        conversation = _conversation(agent(span_id="a1"), chat(span_id="c1", parent="a1"))
        open_ = live_events("s", conversation, LiveState(), incomplete={tid("t1")})
        done = live_events("s", conversation, LiveState(), incomplete=set())
        assert {e["invocationId"] for e in open_} == {tid("t1")}
        assert {e["invocationId"] for e in done} == {sid("a1")}

    def test_token_updates_are_signed_deltas(self):
        ref = SpanRef(tid("t1"), sid("c1"))
        call = LlmCall(ref=ref, operation="chat", usage=Usage(input_tokens=8, output_tokens=2))
        turn = Turn(ref=ref, llm_calls=[call], usage=Usage(input_tokens=8, output_tokens=2))
        state = LiveState(input_tokens=30, output_tokens=2)
        events = live_events("s", Conversation(key="s", turns=[turn]), state, set())
        update = next(e for e in events if e["type"] == "token_update")
        assert (update["inputTokens"], update["outputTokens"]) == (-22, 0)
        assert (state.input_tokens, state.output_tokens) == (8, 2)

    def test_elements_beyond_the_identity_cap_are_left_to_session_complete(self, monkeypatch):
        monkeypatch.setattr(manager_module, "MAX_EMITTED_KEYS", 2)
        spans = [
            chat(trace=f"t{n}", span_id=f"c{n}", outputs=[assistant(f"answer {n}")], start=n * 10, end=n * 10 + 5)
            for n in range(5)
        ]
        events = live_events("s", _conversation(*spans), LiveState(), set())
        assert _kinds(events).count("agent_response") + _kinds(events).count("user_input") <= 2


async def _drain_until(client, kind: str, timeout: float = 10.0) -> list[dict]:
    events: list[dict] = []

    async def _read():
        while True:
            event = await client.queue.get()
            if event is None:
                return
            events.append(event)
            if event.get("type") == kind:
                return

    await asyncio.wait_for(_read(), timeout)
    return events


def _many_calls(n: int) -> dict:
    spans = [agent(span_id="a1", start=1, end=10**9)]
    spans += [
        chat(span_id=f"c{i}", parent="a1", outputs=[assistant(f"step {i}")], start=1_000 + i, end=1_500 + i)
        for i in range(n)
    ]
    return request(spans, named("burst"))


class TestBroadcast:
    async def test_a_large_recompute_reaches_a_reading_client(self):
        mgr = LiveManager(completion_grace_seconds=0.0)
        mgr.start()
        client = mgr.register_sse_client()
        reader = asyncio.create_task(_drain_until(client, "session_complete"))
        await mgr.ingest_spans(decode_json_document(_many_calls(1_500), strict=True))
        events = await reader
        await mgr.shutdown()
        assert client.dropped is False
        assert _kinds(events).count("agent_response") == 1_500

    async def test_a_client_that_does_not_read_is_dropped(self):
        mgr = LiveManager(completion_grace_seconds=0.0)
        mgr.start()
        client = mgr.register_sse_client()
        await mgr.ingest_spans(decode_json_document(_many_calls(1_500), strict=True))
        for _ in range(200):
            if client.dropped:
                break
            await asyncio.sleep(0.02)
        await mgr.shutdown()
        assert client.dropped is True
        assert client not in mgr.clients


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
