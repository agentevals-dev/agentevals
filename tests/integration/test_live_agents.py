"""E2E tests: run real example agents against live servers.

These tests launch agent subprocesses that emit real OTLP traces,
exercising the full pipeline including BatchSpanProcessor/BatchLogRecordProcessor
flush timing, session grouping, and invocation extraction.

Requires API keys (OPENAI_API_KEY for LangChain/Strands).
Skipped when keys are not available.

Tests are synchronous because:
- Agents run as subprocesses (blocking)
- Servers run in a background thread with their own event loop
- We poll session state with time.sleep() (sync)
"""

from __future__ import annotations

import os
import subprocess
import sys

import httpx
import pytest

from .conftest import wait_for_session_complete_sync

pytestmark = pytest.mark.e2e

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_skip_no_openai = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)


def _run_agent(
    script: str,
    otlp_port: int,
    session_name: str,
    eval_set_id: str = "e2e-test",
    extra_env: dict | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run an example agent script as a subprocess."""
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_port}",
        "OTEL_RESOURCE_ATTRIBUTES": (f"agentevals.eval_set_id={eval_set_id},agentevals.session_name={session_name}"),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
    )


@_skip_no_openai
class TestLangchainZeroCode:
    """Run the LangChain zero-code OTLP example and verify session grouping."""

    def test_session_created_with_spans_and_logs(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-langchain"

        result = _run_agent(
            "examples/zero-code-examples/langchain/run.py",
            otlp_port,
            session_name,
        )
        assert result.returncode == 0, f"Agent failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        wait_for_session_complete_sync(mgr, session_name, timeout=30)
        session = mgr.sessions[session_name]

        assert session.is_complete
        assert session.source == "otlp"
        assert len(session.spans) > 0, "Expected spans from LLM calls"
        assert len(session.logs) > 0, "LangChain uses logs for message content"

    def test_invocations_extracted_with_content(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-langchain-inv"

        result = _run_agent(
            "examples/zero-code-examples/langchain/run.py",
            otlp_port,
            session_name,
        )
        assert result.returncode == 0, f"Agent failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        wait_for_session_complete_sync(mgr, session_name, timeout=30)
        session = mgr.sessions[session_name]

        assert len(session.invocations) > 0, "Expected extracted invocations"
        for inv in session.invocations:
            has_content = inv.get("userText") or inv.get("agentResponse")
            assert has_content, f"Invocation {inv.get('invocationId', '?')} has no content"

    def test_session_visible_via_api(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-langchain-api"

        result = _run_agent(
            "examples/zero-code-examples/langchain/run.py",
            otlp_port,
            session_name,
        )
        assert result.returncode == 0

        wait_for_session_complete_sync(mgr, session_name, timeout=30)

        resp = httpx.get(f"http://127.0.0.1:{main_port}/api/streaming/sessions")
        assert resp.status_code == 200
        session_ids = [s["sessionId"] for s in resp.json()["data"]]
        assert session_name in session_ids


@_skip_no_openai
class TestStrandsZeroCode:
    """Run the Strands zero-code OTLP example and verify session grouping."""

    def test_session_created_spans_only(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-strands"

        result = _run_agent(
            "examples/zero-code-examples/strands/run.py",
            otlp_port,
            session_name,
            extra_env={
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            },
        )
        assert result.returncode == 0, f"Agent failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        wait_for_session_complete_sync(mgr, session_name, timeout=30)
        session = mgr.sessions[session_name]

        assert session.is_complete
        assert session.source == "otlp"
        assert len(session.spans) > 0, "Expected spans from LLM calls"

    def test_invocations_extracted(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-strands-inv"

        result = _run_agent(
            "examples/zero-code-examples/strands/run.py",
            otlp_port,
            session_name,
            extra_env={
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            },
        )
        assert result.returncode == 0, f"Agent failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        wait_for_session_complete_sync(mgr, session_name, timeout=30)
        session = mgr.sessions[session_name]

        assert len(session.invocations) > 0, "Expected extracted invocations"

    def test_session_visible_via_api(self, live_servers):
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-strands-api"

        result = _run_agent(
            "examples/zero-code-examples/strands/run.py",
            otlp_port,
            session_name,
            extra_env={
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            },
        )
        assert result.returncode == 0

        wait_for_session_complete_sync(mgr, session_name, timeout=30)

        resp = httpx.get(f"http://127.0.0.1:{main_port}/api/streaming/sessions")
        assert resp.status_code == 200
        session_ids = [s["sessionId"] for s in resp.json()["data"]]
        assert session_name in session_ids


@_skip_no_openai
class TestAgentRerun:
    """Verify that re-running an agent with the same session_name creates
    separate sessions, not merging them into one.

    Each subprocess is a new OS process with new trace_ids. The OTLP
    receiver must recognize these as distinct runs and assign unique
    session IDs (session_name, session_name-2, etc.)."""

    def test_strands_rerun_creates_separate_sessions(self, live_servers):
        """Run the Strands agent twice with the same session_name.
        Each run must produce its own session."""
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-strands-rerun"
        strands_env = {
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        }

        result1 = _run_agent(
            "examples/zero-code-examples/strands/run.py",
            otlp_port,
            session_name,
            extra_env=strands_env,
        )
        assert result1.returncode == 0, f"Run 1 failed:\n{result1.stderr}"
        wait_for_session_complete_sync(mgr, session_name, timeout=30)

        result2 = _run_agent(
            "examples/zero-code-examples/strands/run.py",
            otlp_port,
            session_name,
            extra_env=strands_env,
        )
        assert result2.returncode == 0, f"Run 2 failed:\n{result2.stderr}"
        wait_for_session_complete_sync(mgr, f"{session_name}-2", timeout=30)

        s1 = mgr.sessions[session_name]
        s2 = mgr.sessions[f"{session_name}-2"]

        assert s1.is_complete and s2.is_complete
        assert len(s1.spans) > 0
        assert len(s2.spans) > 0
        assert s1.trace_ids.isdisjoint(s2.trace_ids), f"Sessions share trace_ids: {s1.trace_ids & s2.trace_ids}"

    def test_langchain_rerun_creates_separate_sessions(self, live_servers):
        """Run the LangChain agent twice with the same session_name.
        Each run must produce its own session."""
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-langchain-rerun"

        result1 = _run_agent(
            "examples/zero-code-examples/langchain/run.py",
            otlp_port,
            session_name,
        )
        assert result1.returncode == 0, f"Run 1 failed:\n{result1.stderr}"
        wait_for_session_complete_sync(mgr, session_name, timeout=30)

        result2 = _run_agent(
            "examples/zero-code-examples/langchain/run.py",
            otlp_port,
            session_name,
        )
        assert result2.returncode == 0, f"Run 2 failed:\n{result2.stderr}"
        wait_for_session_complete_sync(mgr, f"{session_name}-2", timeout=30)

        s1 = mgr.sessions[session_name]
        s2 = mgr.sessions[f"{session_name}-2"]

        assert s1.is_complete and s2.is_complete
        assert len(s1.spans) > 0
        assert len(s2.spans) > 0
        assert s1.trace_ids.isdisjoint(s2.trace_ids), f"Sessions share trace_ids: {s1.trace_ids & s2.trace_ids}"

    def test_rerun_sessions_visible_via_api(self, live_servers):
        """Both rerun sessions are visible in the API response."""
        main_port, otlp_port, mgr = live_servers
        session_name = "e2e-strands-rerun-api"
        strands_env = {
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        }

        for run_idx in range(2):
            result = _run_agent(
                "examples/zero-code-examples/strands/run.py",
                otlp_port,
                session_name,
                extra_env=strands_env,
            )
            assert result.returncode == 0, f"Run {run_idx + 1} failed:\n{result.stderr}"
            expected_id = session_name if run_idx == 0 else f"{session_name}-2"
            wait_for_session_complete_sync(mgr, expected_id, timeout=30)

        resp = httpx.get(f"http://127.0.0.1:{main_port}/api/streaming/sessions")
        assert resp.status_code == 200
        session_ids = [s["sessionId"] for s in resp.json()["data"]]
        assert session_name in session_ids
        assert f"{session_name}-2" in session_ids
