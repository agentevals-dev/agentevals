"""Timing stress tests for OTLP session grouping.

Simulates real-world batch flush patterns: concurrent requests,
interleaved span/log batches, and race conditions.
Uses ASGI transport with fast timers.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    make_genai_log,
    make_genai_span,
    make_log_request,
    make_trace_request,
    send_logs,
    send_traces,
    wait_for_session_complete,
)

pytestmark = pytest.mark.integration


class TestInterleavedBatches:
    async def test_interleaved_span_log_batches(self, trace_manager, otlp_client):
        """Simulate BatchSpanProcessor and BatchLogRecordProcessor flushing
        independently with delays between batches."""
        session_name = "interleaved"

        # Batch 1: spans from first LLM call
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="t1",
                session_name=session_name,
                spans=[make_genai_span(trace_id="t1", span_id="s1")],
            ),
        )
        await asyncio.sleep(0.02)

        # Batch 2: logs from first LLM call (arrive after spans)
        await send_logs(
            otlp_client,
            make_log_request(
                trace_id="t1",
                session_name=session_name,
                log_records=[make_genai_log("gen_ai.user.message", "Hello", trace_id="t1")],
            ),
        )
        await asyncio.sleep(0.02)

        # Batch 3: spans from second LLM call (different trace_id, same session)
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="t2",
                session_name=session_name,
                spans=[make_genai_span(trace_id="t2", span_id="s2")],
            ),
        )
        await asyncio.sleep(0.02)

        # Batch 4: logs from second LLM call
        await send_logs(
            otlp_client,
            make_log_request(
                trace_id="t2",
                session_name=session_name,
                log_records=[make_genai_log("gen_ai.user.message", "World", trace_id="t2")],
            ),
        )

        # Complete with root span
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="t3",
                session_name=session_name,
                spans=[make_genai_span(trace_id="t3", parent_span_id=None)],
            ),
        )

        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        assert len(session.spans) == 3
        assert len(session.logs) == 2
        assert session.trace_ids == {"t1", "t2", "t3"}


class TestConcurrentRequests:
    async def test_concurrent_traces_same_session(self, trace_manager, otlp_client):
        """Multiple trace requests arriving simultaneously for the same session."""
        session_name = "concurrent"

        async def send_trace(idx: int):
            await send_traces(
                otlp_client,
                make_trace_request(
                    trace_id=f"ct-{idx}",
                    session_name=session_name,
                    spans=[make_genai_span(trace_id=f"ct-{idx}")],
                ),
            )

        await asyncio.gather(*[send_trace(i) for i in range(5)])

        # Complete
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="ct-root",
                session_name=session_name,
                spans=[make_genai_span(trace_id="ct-root", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        assert len(session.spans) == 6
        assert len(session.trace_ids) == 6

    async def test_mixed_session_names_concurrent(self, trace_manager, otlp_client):
        """Interleaved spans from session A and B in concurrent requests."""

        async def send_for_session(name: str, idx: int):
            await send_traces(
                otlp_client,
                make_trace_request(
                    trace_id=f"{name}-{idx}",
                    session_name=name,
                    spans=[make_genai_span(trace_id=f"{name}-{idx}")],
                ),
            )

        tasks = []
        for i in range(3):
            tasks.append(send_for_session("sess-a", i))
            tasks.append(send_for_session("sess-b", i))
        await asyncio.gather(*tasks)

        # Complete both
        for name in ["sess-a", "sess-b"]:
            await send_traces(
                otlp_client,
                make_trace_request(
                    trace_id=f"{name}-root",
                    session_name=name,
                    spans=[make_genai_span(trace_id=f"{name}-root", parent_span_id=None)],
                ),
            )

        await wait_for_session_complete(trace_manager, "sess-a")
        await wait_for_session_complete(trace_manager, "sess-b")

        a = trace_manager.sessions["sess-a"]
        b = trace_manager.sessions["sess-b"]
        assert len(a.spans) == 4
        assert len(b.spans) == 4
        assert a.trace_ids.isdisjoint(b.trace_ids)


class TestGracePeriodInteractions:
    async def test_logs_arrive_during_grace_period(self, trace_manager, otlp_client):
        """Send root span then immediately send logs during grace period."""
        session_name = "grace-logs"

        # Send spans including root
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="gl-t1",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="gl-t1", span_id="child", parent_span_id="root"),
                    make_genai_span(trace_id="gl-t1", span_id="root", parent_span_id=None),
                ],
            ),
        )

        # Immediately send logs (before grace period expires)
        session = trace_manager.sessions[session_name]
        assert not session.is_complete

        await send_logs(
            otlp_client,
            make_log_request(
                trace_id="gl-t1",
                session_name=session_name,
                log_records=[
                    make_genai_log("gen_ai.user.message", "Grace test", trace_id="gl-t1"),
                ],
            ),
        )

        await wait_for_session_complete(trace_manager, session_name)

        assert len(session.logs) == 1
        assert len(session.spans) == 2


class TestRapidSequentialSessions:
    async def test_rapid_new_trace_creates_new_session(self, trace_manager, otlp_client):
        """Complete session A, immediately start session B with same name
        but new trace_id. Since trace_id is unknown, a new session is created."""
        session_name = "rapid"

        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="rapid-a",
                session_name=session_name,
                spans=[make_genai_span(trace_id="rapid-a", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="rapid-b",
                session_name=session_name,
                spans=[make_genai_span(trace_id="rapid-b", parent_span_id=None)],
            ),
        )
        await wait_for_session_complete(trace_manager, f"{session_name}-2")

        a = trace_manager.sessions[session_name]
        b = trace_manager.sessions[f"{session_name}-2"]
        assert a.trace_ids == {"rapid-a"}
        assert b.trace_ids == {"rapid-b"}
        assert len(a.spans) == 1
        assert len(b.spans) == 1

    async def test_split_batch_reopens_despite_rapid_timing(self, trace_manager, otlp_client):
        """Even with rapid timing, a known trace_id reopens the session."""
        session_name = "rapid-split"

        # Batch 1: root span + child from next trace
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="rs-t1",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="rs-t1", parent_span_id=None),
                    make_genai_span(trace_id="rs-t2", span_id="t2-child", parent_span_id="t2-root"),
                ],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        # Batch 2: rest of the split trace
        await send_traces(
            otlp_client,
            make_trace_request(
                trace_id="rs-t2",
                session_name=session_name,
                spans=[
                    make_genai_span(trace_id="rs-t2", span_id="t2-root", parent_span_id=None),
                ],
            ),
        )
        await wait_for_session_complete(trace_manager, session_name)

        assert len(trace_manager.sessions) == 1
        session = trace_manager.sessions[session_name]
        assert session.trace_ids == {"rs-t1", "rs-t2"}
        assert len(session.spans) == 3


class TestLargeBatches:
    async def test_large_batch_with_multiple_traces(self, trace_manager, otlp_client):
        """Single OTLP request with spans from 5 different trace_ids,
        all with the same session_name."""
        session_name = "large-batch"
        spans = []
        for i in range(5):
            spans.append(make_genai_span(trace_id=f"lb-{i}", span_id=f"s-{i}"))
        spans.append(make_genai_span(trace_id="lb-root", parent_span_id=None))

        body = make_trace_request(
            trace_id="lb-0",
            session_name=session_name,
            spans=spans,
        )
        await send_traces(otlp_client, body)
        await wait_for_session_complete(trace_manager, session_name)

        session = trace_manager.sessions[session_name]
        assert len(session.spans) == 6
        assert len(session.trace_ids) == 6

    async def test_log_batch_spans_multiple_sessions(self, trace_manager, otlp_client):
        """Logs with different trace_ids that map to different sessions."""
        # Create two sessions
        for name in ["log-batch-a", "log-batch-b"]:
            await send_traces(
                otlp_client,
                make_trace_request(
                    trace_id=f"t-{name}",
                    session_name=name,
                    spans=[make_genai_span(trace_id=f"t-{name}")],
                ),
            )

        # Single log batch with entries for both sessions
        log_body = make_log_request(
            trace_id="t-log-batch-a",
            session_name="log-batch-a",
            log_records=[
                make_genai_log("gen_ai.user.message", "For A", trace_id="t-log-batch-a"),
            ],
        )
        await send_logs(otlp_client, log_body)

        log_body_b = make_log_request(
            trace_id="t-log-batch-b",
            session_name="log-batch-b",
            log_records=[
                make_genai_log("gen_ai.user.message", "For B", trace_id="t-log-batch-b"),
            ],
        )
        await send_logs(otlp_client, log_body_b)

        # Complete both with root spans
        for name in ["log-batch-a", "log-batch-b"]:
            await send_traces(
                otlp_client,
                make_trace_request(
                    trace_id=f"t-{name}-root",
                    session_name=name,
                    spans=[make_genai_span(trace_id=f"t-{name}-root", parent_span_id=None)],
                ),
            )

        await wait_for_session_complete(trace_manager, "log-batch-a")
        await wait_for_session_complete(trace_manager, "log-batch-b")

        a = trace_manager.sessions["log-batch-a"]
        b = trace_manager.sessions["log-batch-b"]
        assert len(a.logs) == 1
        assert len(b.logs) == 1
