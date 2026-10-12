"""Session watchers: the latest pending event per session, a snapshot on subscribe, and a cap."""

from __future__ import annotations

import asyncio

import pytest

from agentevals.api.v1alpha1 import sessions_pb2
from agentevals.otel.decode import decode_json_document
from agentevals.otel.store import Limits
from agentevals.streaming import manager as manager_module
from agentevals.streaming.manager import LiveManager, TooManyWatchers, Watcher, WatchEvent
from otel.builders import chat, named, request


def _event(session_id: str, span_count: int = 0) -> WatchEvent:
    session = sessions_pb2.WatchSessionsResponse().upsert
    session.summary.id = session_id
    session.summary.span_count = span_count
    return WatchEvent.upsert(session)


async def _drain(watcher: Watcher) -> list[sessions_pb2.WatchSessionsResponse]:
    """Everything the watcher holds, in order. Closes it first, so this never waits."""
    watcher.close()
    out = []
    while (event := await watcher.next()) is not None:
        out.append(event.with_turns)
    return out


def _decoded(name: str):
    return decode_json_document(request([chat(trace=name)], named(name)), strict=True)


class TestWatcher:
    async def test_a_newer_event_replaces_the_pending_one_and_moves_last(self):
        watcher = Watcher(include_turns=True)
        watcher.put("a", _event("a", 1))
        watcher.put("b", _event("b", 1))
        watcher.put("a", _event("a", 2))
        events = await _drain(watcher)
        assert [(e.upsert.summary.id, e.upsert.summary.span_count) for e in events] == [("b", 1), ("a", 2)]

    async def test_next_waits_for_an_event(self):
        watcher = Watcher(include_turns=True)
        waiting = asyncio.create_task(watcher.next())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        watcher.put("a", _event("a"))
        assert (await asyncio.wait_for(waiting, 1)).with_turns.upsert.summary.id == "a"

    async def test_a_closed_watcher_hands_out_what_it_holds_then_ends(self):
        watcher = Watcher(include_turns=True)
        watcher.put("a", _event("a"))
        watcher.close()
        assert (await watcher.next()).with_turns.upsert.summary.id == "a"
        assert await watcher.next() is None

    async def test_close_wakes_a_waiting_reader(self):
        watcher = Watcher(include_turns=True)
        waiting = asyncio.create_task(watcher.next())
        await asyncio.sleep(0.01)
        watcher.close()
        assert await asyncio.wait_for(waiting, 1) is None

    def test_summary_watchers_get_the_summary_form(self):
        session = sessions_pb2.WatchSessionsResponse().upsert
        session.summary.id = "a"
        session.turns.add(index=0)
        event = WatchEvent.upsert(session)
        assert len(Watcher(include_turns=True).response(event).upsert.turns) == 1
        summary_only = Watcher(include_turns=False).response(event)
        assert (summary_only.upsert.summary.id, len(summary_only.upsert.turns)) == ("a", 0)


class TestSubscribe:
    async def test_snapshot_is_newest_first_then_synced(self):
        mgr = LiveManager()
        for name in ("first", "second"):
            await mgr.ingest_spans(_decoded(name))
        events = await _drain(mgr.subscribe(include_turns=False))
        kinds = [(e.WhichOneof("event"), e.upsert.summary.id) for e in events]
        assert kinds == [("upsert", "second"), ("upsert", "first"), ("synced", "")]

    def test_watchers_are_capped(self, monkeypatch):
        monkeypatch.setattr(manager_module, "MAX_WATCHERS", 2)
        mgr = LiveManager()
        first = mgr.subscribe(include_turns=False)
        mgr.subscribe(include_turns=False)
        with pytest.raises(TooManyWatchers):
            mgr.subscribe(include_turns=False)
        mgr.unsubscribe(first)
        assert first.closed
        mgr.subscribe(include_turns=False)

    def test_unsubscribe_twice_is_harmless(self):
        mgr = LiveManager()
        watcher = mgr.subscribe(include_turns=False)
        mgr.unsubscribe(watcher)
        mgr.unsubscribe(watcher)
        assert mgr.watchers == []

    async def test_a_removed_session_replaces_its_pending_upsert(self):
        mgr = LiveManager(Limits(max_sessions=1))
        watcher = mgr.subscribe(include_turns=False)
        decoded = _decoded("old")
        mgr.load_session("old", decoded.spans, decoded.logs)
        await mgr.ingest_spans(_decoded("new"))
        assert list(mgr.sessions) == ["new"]
        events = await _drain(watcher)
        assert [e.WhichOneof("event") for e in events if e.upsert.summary.id == "old" or e.removed.id == "old"] == [
            "removed"
        ]
        assert "new" in [e.upsert.summary.id for e in events]

    async def test_shutdown_closes_every_watcher(self):
        mgr = LiveManager()
        watchers = [mgr.subscribe(include_turns=False) for _ in range(3)]
        await mgr.shutdown()
        assert mgr.watchers == [] and all(w.closed for w in watchers)
