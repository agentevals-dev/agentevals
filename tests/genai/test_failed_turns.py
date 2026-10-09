"""An agent span that failed before any model call is still a turn, so failures count."""

from __future__ import annotations

from agentevals.genai import semconv as sc
from agentevals.genai.extract import analyze, extract_conversation
from agentevals.otel.decode import decode_json_document
from agentevals.otel.model import build_traces
from agentevals.streaming.manager import invocation_dict
from otel.builders import agent, chat, request, sid, span

STATUS_ERROR = 2


def _traces(*spans: dict):
    traces, _ = build_traces(decode_json_document(request(list(spans)), strict=True).spans)
    return traces


def _rpc_child(span_id: str = "rpc") -> dict:
    return span(span_id=span_id, parent="a1", name="TaskStoreService/UpdateTask", attrs={"rpc.method": "x/UpdateTask"})


def _failed_agent(*, status: bool = True, error_type: str | None = None) -> dict:
    data = agent()
    if status:
        data["status"] = {"code": STATUS_ERROR}
    if error_type:
        data["attributes"].append({"key": "error.type", "value": {"stringValue": error_type}})
    return data


def test_agent_span_with_error_status_and_no_work_is_a_failed_turn():
    conversation = extract_conversation(_traces(_failed_agent(error_type="runtime_error"), _rpc_child()))

    [turn] = conversation.turns
    assert turn.ref.span_id == sid("a1")
    assert (turn.status, turn.error_type) == ("error", "runtime_error")
    assert turn.llm_calls == [] and turn.tool_calls == []
    assert turn.content_captured is False


def test_error_status_without_error_type_gets_the_fallback_type():
    [turn] = extract_conversation(_traces(_failed_agent())).turns
    assert (turn.status, turn.error_type) == ("error", sc.ERROR_TYPE_OTHER)


def test_error_type_alone_marks_the_anchor_as_failed():
    [turn] = extract_conversation(_traces(_failed_agent(status=False, error_type="timeout"))).turns
    assert turn.error_type == "timeout"


def test_empty_agent_span_without_an_error_is_still_not_a_turn():
    [trace] = _traces(agent(), _rpc_child())
    analysis = analyze(trace)

    assert analysis.anchors == []
    assert any("not a turn" in w for w in analysis.warnings)


def test_failed_agent_with_a_model_call_keeps_the_call():
    [turn] = extract_conversation(_traces(_failed_agent(error_type="runtime_error"), chat(parent="a1"))).turns
    assert turn.status == "error" and len(turn.llm_calls) == 1


def test_session_payload_reports_status_and_content_capture():
    [turn] = extract_conversation(_traces(_failed_agent(error_type="runtime_error"))).turns
    payload = invocation_dict(turn, {})

    assert payload["status"] == "error"
    assert payload["errorType"] == "runtime_error"
    assert payload["contentCaptured"] is False
    assert payload["warnings"] == []
