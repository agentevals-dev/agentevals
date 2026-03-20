"""Session grouping edge case tests.

Deterministic tests using crafted OTLP payloads via ASGI transport.
Each test gets a fresh StreamingTraceManager with fast timers (0.1s/0.5s).
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    get_sessions,
    make_genai_log,
    make_genai_span,
    make_log_request,
    make_trace_request,
    send_logs,
    send_traces,
    wait_for_session_complete,
)

pytestmark = pytest.mark.integration


class TestBasicSessionCreation:
    async def test_single_trace_creates_session(self, trace_manager, otlp_client):
        body = make_trace_request(
            trace_id="t1",
            session_name="basic",
            spans=[make_genai_span(trace_id="t1", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "basic")

        session = trace_manager.sessions["basic"]
        assert session.is_complete
        assert session.source == "otlp"
        assert len(session.spans) == 1
        assert "t1" in session.trace_ids

    async def test_session_name_from_resource_attrs(self, trace_manager, otlp_client):
        body = make_trace_request(
            trace_id="t1",
            session_name="custom-name",
            spans=[make_genai_span(trace_id="t1", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "custom-name")

        assert "custom-name" in trace_manager.sessions

    async def test_fallback_session_name(self, trace_manager, otlp_client):
        """Without agentevals.session_name, falls back to otlp-{trace_id[:12]}."""
        body = make_trace_request(
            trace_id="abcdef123456789",
            session_name=None,
            spans=[make_genai_span(trace_id="abcdef123456789", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "otlp-abcdef123456")

        assert "otlp-abcdef123456" in trace_manager.sessions

    async def test_eval_set_id_propagated(self, trace_manager, otlp_client):
        body = make_trace_request(
            trace_id="t1",
            session_name="eval-test",
            eval_set_id="my-eval-set",
            spans=[make_genai_span(trace_id="t1", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "eval-test")

        assert trace_manager.sessions["eval-test"].eval_set_id == "my-eval-set"


class TestMultiTraceGrouping:
    async def test_multi_trace_same_session(self, trace_manager, otlp_client):
        """Multiple traces with same session_name → one session."""
        for i in range(3):
            body = make_trace_request(
                trace_id=f"trace-{i}",
                session_name="multi",
                spans=[make_genai_span(trace_id=f"trace-{i}")],
            )
            await send_traces(otlp_client, body)

        # Send a root span to trigger completion
        body = make_trace_request(
            trace_id="trace-root",
            session_name="multi",
            spans=[make_genai_span(trace_id="trace-root", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "multi")

        session = trace_manager.sessions["multi"]
        assert len(session.spans) == 4
        assert session.trace_ids == {"trace-0", "trace-1", "trace-2", "trace-root"}

    async def test_different_names_different_sessions(self, trace_manager, otlp_client):
        for name in ["session-a", "session-b"]:
            body = make_trace_request(
                trace_id=f"trace-{name}",
                session_name=name,
                spans=[make_genai_span(trace_id=f"trace-{name}", parent_span_id=None)],
            )
            await send_traces(otlp_client, body)

        await wait_for_session_complete(trace_manager, "session-a")
        await wait_for_session_complete(trace_manager, "session-b")

        assert "session-a" in trace_manager.sessions
        assert "session-b" in trace_manager.sessions
        assert trace_manager.sessions["session-a"].trace_ids != trace_manager.sessions["session-b"].trace_ids


class TestSessionCompletion:
    async def test_root_span_triggers_completion(self, trace_manager, otlp_client):
        body = make_trace_request(
            trace_id="t1",
            session_name="root-test",
            spans=[
                make_genai_span(trace_id="t1", span_id="child1", parent_span_id="root1"),
                make_genai_span(trace_id="t1", span_id="root1", parent_span_id=None),
            ],
        )
        await send_traces(otlp_client, body)

        # Should complete within grace period (0.1s) + tolerance
        await wait_for_session_complete(trace_manager, "root-test", timeout=1.0)

        session = trace_manager.sessions["root-test"]
        assert session.is_complete
        assert session.has_root_span
        assert len(session.spans) == 2

    async def test_no_root_span_idle_timeout(self, trace_manager, otlp_client):
        """Without root span, session completes via idle timeout (0.5s in tests)."""
        body = make_trace_request(
            trace_id="t1",
            session_name="idle-test",
            spans=[make_genai_span(trace_id="t1", parent_span_id="some-parent")],
        )
        await send_traces(otlp_client, body)

        session = trace_manager.sessions["idle-test"]
        assert not session.is_complete
        assert not session.has_root_span

        await wait_for_session_complete(trace_manager, "idle-test", timeout=2.0)
        assert session.is_complete


class TestSessionNameCollisions:
    async def test_sequential_runs_unique_ids(self, trace_manager, otlp_client):
        """Repeated runs with same name after completion → name-2, name-3."""
        # First run
        body = make_trace_request(
            trace_id="run1",
            session_name="repeated",
            spans=[make_genai_span(trace_id="run1", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "repeated")

        # Second run — same name but new trace_id → new session
        body = make_trace_request(
            trace_id="run2",
            session_name="repeated",
            spans=[make_genai_span(trace_id="run2", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "repeated-2")

        # Third run
        body = make_trace_request(
            trace_id="run3",
            session_name="repeated",
            spans=[make_genai_span(trace_id="run3", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "repeated-3")

        assert "repeated" in trace_manager.sessions
        assert "repeated-2" in trace_manager.sessions
        assert "repeated-3" in trace_manager.sessions
        assert trace_manager.sessions["repeated"].trace_ids == {"run1"}
        assert trace_manager.sessions["repeated-2"].trace_ids == {"run2"}
        assert trace_manager.sessions["repeated-3"].trace_ids == {"run3"}


class TestOrphanLogs:
    async def test_orphan_logs_before_spans(self, trace_manager, otlp_client):
        """Logs arriving before spans are buffered and replayed into session."""
        log_body = make_log_request(
            trace_id="orphan-trace",
            session_name="orphan-test",
            log_records=[
                make_genai_log("gen_ai.user.message", "Hello", trace_id="orphan-trace"),
            ],
        )
        await send_logs(otlp_client, log_body)

        assert len(trace_manager._orphan_logs) == 1

        trace_body = make_trace_request(
            trace_id="orphan-trace",
            session_name="orphan-test",
            spans=[make_genai_span(trace_id="orphan-trace", parent_span_id=None)],
        )
        await send_traces(otlp_client, trace_body)
        await wait_for_session_complete(trace_manager, "orphan-test")

        session = trace_manager.sessions["orphan-test"]
        assert len(session.logs) >= 1
        assert len(trace_manager._orphan_logs) == 0

    async def test_orphan_logs_matched_by_session_name(self, trace_manager, otlp_client):
        """Orphan log with matching session_name but different trace_id → replayed."""
        log_body = make_log_request(
            trace_id="log-trace",
            session_name="name-match",
            log_records=[
                make_genai_log("gen_ai.user.message", "Hi", trace_id="log-trace"),
            ],
        )
        await send_logs(otlp_client, log_body)

        # Spans arrive with a different trace_id but same session_name
        trace_body = make_trace_request(
            trace_id="span-trace",
            session_name="name-match",
            spans=[make_genai_span(trace_id="span-trace", parent_span_id=None)],
        )
        await send_traces(otlp_client, trace_body)
        await wait_for_session_complete(trace_manager, "name-match")

        session = trace_manager.sessions["name-match"]
        assert "log-trace" in session.trace_ids
        assert "span-trace" in session.trace_ids
        assert len(session.logs) >= 1


class TestLateLogs:
    async def test_late_logs_after_completion(self, trace_manager, otlp_client):
        """Logs arriving after session completion trigger re-extraction."""
        trace_body = make_trace_request(
            trace_id="late-trace",
            session_name="late-test",
            spans=[make_genai_span(trace_id="late-trace", parent_span_id=None)],
        )
        await send_traces(otlp_client, trace_body)
        await wait_for_session_complete(trace_manager, "late-test")

        session = trace_manager.sessions["late-test"]
        assert session.is_complete
        assert len(session.logs) == 0

        log_body = make_log_request(
            trace_id="late-trace",
            session_name="late-test",
            log_records=[
                make_genai_log("gen_ai.user.message", "Late hello", trace_id="late-trace"),
            ],
        )
        await send_logs(otlp_client, log_body)

        # Wait for re-extraction debounce (0.1s in tests)
        await asyncio.sleep(0.3)

        assert len(session.logs) == 1


class TestAPIVisibility:
    async def test_sessions_visible_via_api(self, trace_manager, otlp_client, api_client):
        body = make_trace_request(
            trace_id="vis-trace",
            session_name="visible",
            spans=[make_genai_span(trace_id="vis-trace", parent_span_id=None)],
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, "visible")

        sessions = await get_sessions(api_client)
        session_ids = [s["sessionId"] for s in sessions]
        assert "visible" in session_ids


class TestSpanLimits:
    async def test_span_limit_enforcement(self, trace_manager, otlp_client):
        """Session rejects spans beyond MAX_SPANS_PER_SESSION."""
        from agentevals.streaming.session import MAX_SPANS_PER_SESSION

        # Pre-fill the session with spans up to the limit
        body = make_trace_request(
            trace_id="limit-trace",
            session_name="limit-test",
            spans=[make_genai_span(trace_id="limit-trace")],
        )
        await send_traces(otlp_client, body)

        session = trace_manager.sessions["limit-test"]
        session.spans.extend([{}] * (MAX_SPANS_PER_SESSION - 1))
        assert not session.can_accept_span()

        # This span should be rejected
        body2 = make_trace_request(
            trace_id="limit-trace",
            session_name="limit-test",
            spans=[make_genai_span(trace_id="limit-trace")],
        )
        await send_traces(otlp_client, body2)

        assert len(session.spans) == MAX_SPANS_PER_SESSION


class TestSplitBatchReopen:
    """Regression tests for the split-batch scenario where the OTLP
    BatchSpanProcessor flushes one trace's spans across the session
    completion boundary. Some child spans arrive before the grace period
    fires, the root span arrives after. Because the trace_id was already
    registered in the session, the session is reopened.

    Bug report: strands-zero-code vs strands-zero-code-2 — a 3-turn
    conversation was broken into 2 sessions because turn 3's spans
    were split across the completion boundary."""

    async def test_split_trace_reopens_completed_session(self, trace_manager, otlp_client):
        """When child spans arrive before completion and the root span
        arrives after, the session reopens because the trace_id already
        exists in the session."""
        session_name = "split-reopen"

        # Batch 1: turn 1 root + turn 2 child spans in same flush
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="sr-t1",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="sr-t1", span_id="t1-root", parent_span_id=None),
                    make_genai_span(trace_id="sr-t2", span_id="t2-child", parent_span_id="t2-root"),
                ],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        assert session.is_complete
        assert "sr-t2" in session.trace_ids

        # Batch 2: turn 2 root span arrives after completion
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="sr-t2",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="sr-t2", span_id="t2-root", parent_span_id=None),
                ],
            ),
        )

        assert not session.is_complete
        assert len(trace_manager.sessions) == 1
        assert session.trace_ids == {"sr-t1", "sr-t2"}

        await wait_for_session_complete(trace_manager, session_name)
        assert session.is_complete
        assert len(session.spans) == 3

    async def test_strands_three_turn_bug_repro(self, trace_manager, otlp_client):
        """Reproduces the exact bug from the Strands SDK report: the
        BatchSpanProcessor flushes turns 1-2 and partial turn 3 in one
        batch, then the rest of turn 3 in a second batch after the
        session completes. All spans must end up in a single session."""
        session_name = "strands-repro"

        # Batch 1: turns 1 & 2 fully, plus turn 3 child spans
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="t1",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="t1", span_id="t1-llm", parent_span_id="t1-root"),
                    make_genai_span(trace_id="t1", span_id="t1-root", parent_span_id=None, name="invoke_agent"),
                    make_genai_span(trace_id="t2", span_id="t2-llm", parent_span_id="t2-root"),
                    make_genai_span(
                        trace_id="t2", span_id="t2-tool", parent_span_id="t2-root", name="execute_tool roll_die"
                    ),
                    make_genai_span(trace_id="t2", span_id="t2-root", parent_span_id=None, name="invoke_agent"),
                    # Turn 3 child spans — flushed in same batch
                    make_genai_span(trace_id="t3", span_id="t3-llm", parent_span_id="t3-root"),
                    make_genai_span(
                        trace_id="t3", span_id="t3-tool", parent_span_id="t3-root", name="execute_tool check_prime"
                    ),
                ],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        assert session.is_complete
        assert "t3" in session.trace_ids

        # Batch 2: turn 3 root span + remaining spans (after completion)
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="t3",
                session_name=session_name,
                spans=[
                    make_genai_span(
                        trace_id="t3", span_id="t3-loop", parent_span_id="t3-root", name="execute_event_loop_cycle"
                    ),
                    make_genai_span(trace_id="t3", span_id="t3-root", parent_span_id=None, name="invoke_agent"),
                ],
            ),
        )

        assert not session.is_complete
        await wait_for_session_complete(trace_manager, session_name)

        assert len(trace_manager.sessions) == 1
        assert session.trace_ids == {"t1", "t2", "t3"}
        assert len(session.spans) == 9

    async def test_new_trace_after_completion_creates_new_session(self, trace_manager, otlp_client):
        """A completely new trace_id after session completion creates a
        new session (not a reopen). This is the re-run case."""
        session_name = "no-reopen"

        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="run-1",
                session_name=session_name,
                spans=[make_genai_span(trace_id="run-1", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        # New trace_id (not seen before) → new session
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="run-2",
                session_name=session_name,
                spans=[make_genai_span(trace_id="run-2", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, f"{session_name}-2")

        assert len(trace_manager.sessions) == 2
        assert trace_manager.sessions[session_name].trace_ids == {"run-1"}
        assert trace_manager.sessions[f"{session_name}-2"].trace_ids == {"run-2"}

    async def test_reopen_preserves_existing_spans_and_logs(self, trace_manager, otlp_client):
        """Reopening a session preserves all previously collected spans and logs."""
        session_name = "preserve"

        # Send spans and logs with trace_id "pres-t1", PLUS a child span
        # from "pres-t2" so its trace_id is registered for reopen
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="pres-t1",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="pres-t1", parent_span_id=None),
                    make_genai_span(trace_id="pres-t2", span_id="t2-child", parent_span_id="t2-root"),
                ],
            ),
        )
        await send_logs(
            otlp_client,
            make_log_request(
                trace_id="pres-t1",
                session_name=session_name,
                log_records=[
                    make_genai_log("gen_ai.user.message", "Turn 1", trace_id="pres-t1"),
                ],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        spans_before = len(session.spans)
        logs_before = len(session.logs)
        assert spans_before >= 2
        assert logs_before >= 1

        # Reopen via split-batch trace_id match
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="pres-t2",
                session_name=session_name,
                spans=[make_genai_span(trace_id="pres-t2", span_id="t2-root", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        assert len(session.spans) == spans_before + 1
        assert len(session.logs) == logs_before
