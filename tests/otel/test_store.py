"""Live store rules: staging, merges, reruns, retry dedup, eviction and bounds."""

from __future__ import annotations

from agentevals.otel.decode import decode_json_document
from agentevals.otel.store import (
    REASON_LOG_MEMORY,
    REASON_MERGE_REFUSED,
    REASON_PENDING_FULL,
    REASON_SPAN_MEMORY,
    REASON_SPAN_SESSIONS,
)

from .builders import agent, chat, ingest, log_record, log_request, make_store, named, request, span, tid


def _complete(store, clock, seconds: float = 60.0) -> list[str]:
    clock.advance(seconds)
    return store.tick()


class TestStaging:
    def test_trace_without_genai_spans_is_not_listed(self):
        store, _ = make_store()
        ingest(store, request([span(name="GET /health")]))
        assert store.sessions == {}
        assert store.events == []

    def test_staged_trace_joins_once_a_key_arrives(self):
        store, _ = make_store()
        ingest(store, request([span(span_id="http", name="POST /run")]))
        ingest(store, request([span(span_id="db", parent="http", name="SELECT")], named("my-run")))
        session = store.sessions["my-run"]
        assert session.span_count == 2
        assert tid("t1") not in store.staged

    def test_staged_traces_expire(self):
        store, clock = make_store(staged_ttl_seconds=120.0)
        ingest(store, request([span(name="GET /")]))
        clock.advance(121)
        store.expire()
        assert store.staged == {}
        assert store.nbytes == 0

    def test_staging_is_bounded(self):
        store, _ = make_store(staged_traces=2)
        for n in range(3):
            ingest(store, request([span(trace=f"t{n}", name="GET /")]))
        assert list(store.staged) == [tid("t1"), tid("t2")]


class TestMerge:
    def test_provisional_session_merge_is_refused_when_the_target_is_full(self):
        store, _ = make_store(traces_per_session=1)
        ingest(store, request([chat(trace="other")], named("run")))
        ingest(store, request([chat(trace="t1")]))
        result = ingest(store, request([agent(trace="t1")], named("run")))
        assert result.reasons[REASON_MERGE_REFUSED] == 1
        assert sorted(store.sessions) == ["otlp-" + tid("t1")[:12], "run"]

    def test_merged_session_keeps_the_traces_spans_and_logs(self):
        store, _ = make_store()
        ingest(store, request([chat(trace="t1", span_id="c1")]))
        ingest(store, log_request([log_record(trace="t1", span_id="c1")]))
        ingest(store, request([agent(trace="t1")], named("run")))
        session = store.sessions["run"]
        assert (session.span_count, session.log_count) == (2, 1)


class TestReruns:
    def _finish(self, store, clock, trace: str, resource: dict) -> None:
        ingest(store, request([agent(trace=trace)], resource))
        _complete(store, clock)

    def test_same_instance_rejoins_however_late(self):
        store, clock = make_store(rerun_window_seconds=30.0)
        self._finish(store, clock, "turn1", named("app", **{"service.instance.id": "p1"}))
        clock.advance(600)
        ingest(store, request([agent(trace="turn2")], named("app", **{"service.instance.id": "p1"})))
        assert list(store.sessions) == ["app"]
        assert store.sessions["app"].trace_ids == [tid("turn1"), tid("turn2")]

    def test_different_instance_starts_a_new_generation_at_once(self):
        store, clock = make_store(rerun_window_seconds=30.0)
        self._finish(store, clock, "run1", named("app", **{"service.instance.id": "p1"}))
        ingest(store, request([agent(trace="run2")], named("app", **{"service.instance.id": "p2"})))
        assert list(store.sessions) == ["app", "app-2"]

    def test_without_instance_ids_the_window_decides(self):
        store, clock = make_store(rerun_window_seconds=30.0)
        self._finish(store, clock, "turn1", named("app"))
        clock.advance(20)
        ingest(store, request([agent(trace="turn2")], named("app")))
        assert list(store.sessions) == ["app"]
        _complete(store, clock)
        clock.advance(31)
        ingest(store, request([agent(trace="run2")], named("app")))
        assert list(store.sessions) == ["app", "app-2"]

    def test_conversation_ids_never_split(self):
        store, clock = make_store(rerun_window_seconds=30.0)
        conv = {"gen_ai.conversation.id": "conv-1"}
        ingest(store, request([agent(trace="a")], {**conv, "service.instance.id": "p1"}))
        _complete(store, clock, 600)
        ingest(store, request([agent(trace="b")], {**conv, "service.instance.id": "p2"}))
        assert list(store.sessions) == ["conv-1"]

    def test_active_named_session_is_joined_by_any_instance(self):
        store, _ = make_store()
        ingest(store, request([agent(trace="a")], named("app", **{"service.instance.id": "frontend"})))
        ingest(store, request([agent(trace="b")], named("app", **{"service.instance.id": "backend"})))
        assert list(store.sessions) == ["app"]


class TestRetries:
    def test_a_retried_log_batch_is_stored_once(self):
        store, _ = make_store()
        ingest(store, request([chat(span_id="c1")], named("s")))
        body = log_request([log_record(span_id="c1")])
        first, retry = ingest(store, body), ingest(store, body)
        assert (first.accepted, retry.accepted, retry.rejected) == (1, 1, 0)
        assert store.sessions["s"].log_count == 1

    def test_distinct_logs_with_equal_content_are_kept(self):
        store, _ = make_store()
        ingest(store, request([chat(span_id="c1")], named("s")))
        ingest(store, log_request([log_record(span_id="c1"), log_record(span_id="c1", timeUnixNano="7")]))
        assert store.sessions["s"].log_count == 2

    def test_a_retried_span_batch_changes_nothing(self):
        store, _ = make_store()
        body = request([chat(span_id="c1")], named("s"))
        ingest(store, body)
        version = store.traces[tid("t1")].version
        assert ingest(store, body).accepted == 1
        assert store.traces[tid("t1")].version == version


class TestEviction:
    def test_session_limit_evicts_the_oldest_finished_session(self):
        store, clock = make_store(max_sessions=2)
        for name in ("a", "b"):
            ingest(store, request([agent(trace=name)], named(name)))
        _complete(store, clock)
        store.events.clear()
        ingest(store, request([agent(trace="c")], named("c")))
        assert sorted(store.sessions) == ["b", "c"]
        assert ("removed", "a", None) in store.events

    def test_session_limit_refuses_new_sessions_while_all_are_active(self):
        store, _ = make_store(max_sessions=1)
        ingest(store, request([agent(trace="a")], named("a")))
        result = ingest(store, request([agent(trace="b")], named("b")))
        assert result.reasons[REASON_SPAN_SESSIONS] == 1
        assert result.overloaded

    def test_memory_budget_evicts_finished_sessions_first(self):
        store, clock = make_store(max_bytes=20_000)
        ingest(
            store,
            request(
                [
                    chat(
                        trace="old",
                        outputs=[{"role": "assistant", "parts": [{"type": "text", "content": "x" * 8_000}]}],
                    )
                ],
                named("old"),
            ),
        )
        _complete(store, clock)
        ingest(
            store,
            request(
                [
                    chat(
                        trace="new",
                        outputs=[{"role": "assistant", "parts": [{"type": "text", "content": "y" * 15_000}]}],
                    )
                ],
                named("new"),
            ),
        )
        assert list(store.sessions) == ["new"]
        assert store.nbytes <= 20_000

    def test_memory_budget_refuses_when_nothing_can_be_evicted(self):
        store, _ = make_store(max_bytes=5_000)
        ingest(store, request([chat(trace="a")], named("a")))
        big = [{"role": "assistant", "parts": [{"type": "text", "content": "z" * 10_000}]}]
        result = ingest(store, request([chat(trace="a", span_id="c2", outputs=big)], named("a")))
        assert result.reasons[REASON_SPAN_MEMORY] == 1
        big_log = log_record(trace="a", span_id="c1", body={"content": "z" * 10_000})
        assert ingest(store, log_request([big_log])).reasons[REASON_LOG_MEMORY] == 1

    def test_removed_sessions_release_their_bytes(self):
        store, clock = make_store(session_ttl_seconds=10.0)
        ingest(store, request([chat()], named("s")))
        _complete(store, clock)
        clock.advance(11)
        store.expire()
        assert (store.sessions, store.traces, store.nbytes) == ({}, {}, 0)


class TestPendingLogs:
    def test_per_trace_cap(self):
        store, _ = make_store(pending_logs_per_trace=1)
        result = ingest(store, log_request([log_record(), log_record(timeUnixNano="9")]))
        assert (result.accepted, result.reasons[REASON_PENDING_FULL]) == (1, 1)

    def test_byte_cap(self):
        store, _ = make_store(pending_log_bytes=1_000)
        result = ingest(store, log_request([log_record(body={"content": "x" * 2_000})]))
        assert result.reasons[REASON_PENDING_FULL] == 1
        assert store.pending.count == 0

    def test_replayed_logs_count_toward_the_session(self):
        store, _ = make_store()
        ingest(store, log_request([log_record(span_id="c1")]))
        ingest(store, request([chat(span_id="c1")], named("s")))
        assert store.sessions["s"].log_count == 1
        assert store.pending.count == 0


class TestLoadSession:
    def test_bypasses_routing_and_keeps_names_unique(self):
        store, _ = make_store()
        decoded = decode_json_document(request([chat()], named("ignored")), strict=False)
        first = store.load_session("bundle", decoded.spans, decoded.logs)
        second = store.load_session("bundle", decoded.spans, decoded.logs)
        assert (first.session_id, second.session_id) == ("bundle", "bundle-2")
        assert first.is_complete and first.completed_once
        assert "ignored" not in store.sessions


def _owned_bytes(store) -> int:
    return sum(t.nbytes for t in store.traces.values()) + sum(t.nbytes for t in store.staged.values())


def _big_reply(size: int) -> list[dict]:
    return [{"role": "assistant", "parts": [{"type": "text", "content": "z" * size}]}]


def _agent_with_conversation(
    trace: str, span_id: str, conversation: str, start: int, parent: str | None = None
) -> dict:
    attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conversation}
    return span(trace=trace, span_id=span_id, parent=parent, attrs=attrs, start=start, end=9_000)


class TestRollback:
    def test_a_session_whose_spans_are_all_refused_leaves_nothing_behind(self):
        store, _ = make_store(max_bytes=2_000)
        result = ingest(store, request([chat(outputs=_big_reply(5_000))], named("a")))
        assert result.reasons[REASON_SPAN_MEMORY] == 1
        assert (store.sessions, store.traces, store.nbytes) == ({}, {}, 0)
        assert not [e for e in store.events if e[0] == "started"]

    def test_memory_refusals_do_not_use_up_session_slots(self):
        store, _ = make_store(max_bytes=2_000, max_sessions=2)
        for n in range(3):
            ingest(store, request([chat(trace=f"big{n}", outputs=_big_reply(5_000))], named(f"big{n}")))
        assert ingest(store, request([chat(trace="small")], named("small"))).accepted == 1
        assert list(store.sessions) == ["small"]

    def test_a_retry_that_promotes_a_finished_session_completes_again(self):
        store, clock = make_store()
        ingest(store, request([chat(trace="t1", span_id="c1")]))
        _complete(store, clock)
        ingest(store, request([chat(trace="t1", span_id="c1")], named("run")))
        assert list(store.sessions) == ["run"]
        assert _complete(store, clock) == ["run"]


class TestPromotion:
    def test_promotion_never_evicts_the_session_being_promoted(self):
        store, clock = make_store(max_sessions=1)
        ingest(store, request([chat(trace="t1", span_id="c1")]))
        _complete(store, clock)
        keyed = span(trace="t1", span_id="x", parent="c1", attrs={"gen_ai.conversation.id": "conv-1"})
        result = ingest(store, request([keyed]))
        assert result.reasons[REASON_SPAN_SESSIONS] == 1
        assert result.overloaded
        assert [s.span_count for s in store.sessions.values()] == [1]

    def test_the_earliest_span_owns_the_trace_key_across_exports(self):
        store, _ = make_store()
        ingest(store, request([_agent_with_conversation("t1", "d", "conv-delegate", 2_000, parent="r")]))
        ingest(store, request([_agent_with_conversation("t1", "r", "conv-parent", 1_000)]))
        assert list(store.sessions) == ["conv-parent"]
        assert store.sessions["conv-parent"].span_count == 2

    def test_only_the_trace_whose_earliest_span_arrives_moves(self):
        store, _ = make_store()
        for trace in ("t1", "t2"):
            ingest(store, request([_agent_with_conversation(trace, f"d-{trace}", "conv-delegate", 2_000)]))
        ingest(store, request([_agent_with_conversation("t1", "r", "conv-parent", 1_000)]))
        assert store.session_of_trace(tid("t1")).session_id == "conv-parent"
        assert store.session_of_trace(tid("t2")).session_id == "conv-delegate"

    def test_a_later_enclosed_span_does_not_move_the_trace(self):
        store, _ = make_store()
        ingest(store, request([_agent_with_conversation("t1", "r", "conv-parent", 1_000)]))
        ingest(store, request([_agent_with_conversation("t1", "d", "conv-delegate", 2_000, parent="r")]))
        assert list(store.sessions) == ["conv-parent"]
        assert store.counters["session key conflicts"] == 1

    def test_a_session_id_session_moves_to_the_conversation_learned_later(self):
        store, _ = make_store()
        ingest(store, request([_subprocess_span("t1", "h1", "sess-1", 2_000)]))
        assert list(store.sessions) == ["sess-1"]
        ingest(store, request([_agent_with_conversation("t1", "a1", "conv-1", 1_000)]))
        assert list(store.sessions) == ["conv-1"]
        assert ("removed", "sess-1", "conv-1") in store.events
        assert store.sessions["conv-1"].span_count == 2

    def test_the_next_turn_of_the_subprocess_joins_the_same_conversation(self):
        store, _ = make_store()
        for trace in ("t1", "t2"):
            ingest(store, request([_subprocess_span(trace, f"h-{trace}", "sess-1", 2_000)]))
            ingest(store, request([_agent_with_conversation(trace, f"a-{trace}", "conv-1", 1_000)]))
        assert list(store.sessions) == ["conv-1"]
        assert len(store.sessions["conv-1"].trace_ids) == 2

    def test_a_weaker_key_arriving_later_is_not_a_conflict(self):
        store, _ = make_store()
        ingest(store, request([_agent_with_conversation("t1", "a1", "conv-1", 1_000)]))
        ingest(store, request([_subprocess_span("t1", "h1", "sess-1", 2_000, parent="a1")]))
        assert list(store.sessions) == ["conv-1"]
        assert store.counters["session key conflicts"] == 0


def _subprocess_span(
    trace: str, span_id: str, session_id: str, start: int, parent: str | None = "elsewhere", flags: int | None = None
) -> dict:
    """A harness subprocess span: keyed only by ``session.id``, continuing a trace from its parent process."""
    return span(
        trace=trace,
        span_id=span_id,
        parent=parent,
        attrs={"session.id": session_id},
        start=start,
        end=3_000,
        flags=flags,
    )


class TestCompletionRoots:
    def test_a_remote_parent_span_inside_a_longer_trace_does_not_complete_it(self):
        store, clock = make_store(completion_grace_seconds=3.0, idle_timeout_seconds=30.0)
        ingest(store, request([span(span_id="rpc", parent="root", start=1_000, end=9_000)], named("s")))
        ingest(store, request([_subprocess_span("t1", "h1", "sess-1", 2_000, flags=0x300)], named("s")))
        assert _complete(store, clock, 4) == []
        assert _complete(store, clock, 27) == ["s"]

    def test_a_remote_parent_span_enclosing_the_trace_completes_it_after_the_grace(self):
        store, clock = make_store(completion_grace_seconds=3.0, idle_timeout_seconds=30.0)
        entry = span(span_id="entry", parent="caller", start=1_000, end=9_000, flags=0x300)
        ingest(store, request([span(span_id="work", parent="entry", start=2_000, end=3_000)], named("s")))
        ingest(store, request([entry], named("s")))
        assert _complete(store, clock, 4) == ["s"]


class TestAccounting:
    def test_a_log_never_evicts_the_staged_trace_it_is_written_to(self):
        store, _ = make_store(max_bytes=2_000)
        ingest(store, request([span(trace="st", name="GET /")]))
        ingest(store, log_request([log_record(trace="st", body={"content": "y" * 1_600})]))
        assert tid("st") in store.staged
        assert store.nbytes == _owned_bytes(store)

    def test_resource_attributes_count_against_the_budget(self):
        store, _ = make_store()
        ingest(store, request([chat(content=False)], {**named("r"), "k8s.pod.annotations": "r" * 10_000}))
        assert store.nbytes > 10_000
        assert store.nbytes == _owned_bytes(store)

    def test_a_transient_refusal_makes_the_whole_export_retryable(self):
        store, _ = make_store(max_bytes=2_000)
        document = request([chat(trace="a")], named("a"))
        document["resourceSpans"] += request([chat(trace="b", outputs=_big_reply(5_000))], named("b"))["resourceSpans"]
        result = ingest(store, document)
        assert result.accepted == 1
        assert result.reasons[REASON_SPAN_MEMORY] == 1
        assert result.overloaded
