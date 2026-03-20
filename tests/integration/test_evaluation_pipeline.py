"""Evaluation pipeline integration tests.

Tests the full pipeline: traces → session → eval set creation → evaluation.
Eval set creation is deterministic (no API keys).
Full evaluation requires a Google API key for the judge model.
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


def _make_realistic_session_traces(
    session_name: str,
    queries: list[str],
) -> tuple[list[dict], list[dict]]:
    """Build OTLP trace + log request bodies that produce extractable invocations.

    Creates one LLM span + user/assistant log pair per query, simulating
    a GenAI semconv agent with logs-based content delivery.
    """
    trace_requests = []
    log_requests = []

    for i, query in enumerate(queries):
        trace_id = f"{session_name}-t{i}"
        span_id = f"span-{i}"
        is_last = i == len(queries) - 1

        trace_requests.append(
            make_trace_request(
                trace_id=trace_id,
                session_name=session_name,
                eval_set_id=f"{session_name}-eval",
                spans=[
                    make_genai_span(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=None if is_last else "parent",
                        name="chat gpt-4o-mini",
                    )
                ],
            )
        )

        log_requests.append(
            make_log_request(
                trace_id=trace_id,
                session_name=session_name,
                log_records=[
                    make_genai_log(
                        "gen_ai.user.message",
                        query,
                        trace_id=trace_id,
                        span_id=span_id,
                        role="user",
                    ),
                    make_genai_log(
                        "gen_ai.assistant.message",
                        f"Response to: {query}",
                        trace_id=trace_id,
                        span_id=span_id,
                        role="assistant",
                    ),
                ],
            )
        )

    return trace_requests, log_requests


async def _create_session(otlp_client, trace_manager, session_name, queries):
    """Send realistic traces and wait for session to complete."""
    trace_reqs, log_reqs = _make_realistic_session_traces(session_name, queries)

    for req in trace_reqs:
        await send_traces(otlp_client, req)
    for req in log_reqs:
        await send_logs(otlp_client, req)

    await wait_for_session_complete(trace_manager, session_name, timeout=3.0)

    # Wait for late-log re-extraction
    await asyncio.sleep(0.3)


class TestEvalSetCreation:
    """Verify eval set creation from completed sessions (no API key needed)."""

    async def test_create_eval_set_from_session(self, trace_manager, otlp_client, api_client):
        await _create_session(
            otlp_client,
            trace_manager,
            "eval-golden",
            ["What is 2+2?", "Is that prime?", "Tell me a joke"],
        )

        resp = await api_client.post(
            "/api/streaming/create-eval-set",
            json={"session_id": "eval-golden", "eval_set_id": "test-eval"},
        )
        assert resp.status_code == 200

        data = resp.json()["data"]
        assert data["numInvocations"] > 0
        eval_set = data["evalSet"]
        assert "eval_cases" in eval_set
        assert len(eval_set["eval_cases"]) > 0
        conversation = eval_set["eval_cases"][0]["conversation"]
        assert len(conversation) == 3

    async def test_create_eval_set_nonexistent_session(self, trace_manager, otlp_client, api_client):
        resp = await api_client.post(
            "/api/streaming/create-eval-set",
            json={"session_id": "does-not-exist", "eval_set_id": "test"},
        )
        assert resp.status_code == 404
