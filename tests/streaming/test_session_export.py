"""A live session exported as OTLP loads back as the same conversation, grouped as one."""

from __future__ import annotations

import json

from agentevals.genai.extract import extract_conversation
from agentevals.genai.messages import text_of
from agentevals.loader import load_traces
from agentevals.otel.decode import decode_json_document
from agentevals.runner import evaluation_groups
from agentevals.streaming.manager import LiveManager
from genai.test_continuations import _tool_round
from otel.builders import chat, log_record, log_request, named, request


def _turns(conversation):
    return [
        (text_of(t.user_input), text_of(t.final_output), [c.name for c in t.tool_calls]) for t in conversation.turns
    ]


async def _session(mgr: LiveManager, resource: dict) -> None:
    await mgr.ingest_spans(decode_json_document(request(list(_tool_round()), resource), strict=True))
    await mgr.ingest_logs(
        decode_json_document(log_request([log_record(trace="a", span_id="c1")], resource), strict=True)
    )


async def test_export_round_trips_as_one_conversation(tmp_path):
    mgr = LiveManager()
    await _session(mgr, {"gen_ai.conversation.id": "conv-7"})
    session_id = next(iter(mgr.sessions))
    live = extract_conversation(mgr.session_traces(session_id), session_id)

    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(mgr.session_document(session_id)) + "\n")
    traces = load_traces(str(path))
    groups = evaluation_groups(traces, "auto")

    assert len(groups) == 1
    assert _turns(extract_conversation(groups[0][1], groups[0][0])) == _turns(live)
    assert len(live.turns) == 1


async def test_export_keeps_logs_joined_to_their_spans():
    mgr = LiveManager()
    await _session(mgr, named("s"))
    document = mgr.session_document("s")
    records = [r for rl in document["resourceLogs"] for sl in rl["scopeLogs"] for r in sl["logRecords"]]
    assert len(records) == 1
    assert records[0]["spanId"]
    spans = [s for rs in document["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
    assert all("gen_ai.user.message" not in json.dumps(s) for s in spans)


async def test_every_resource_carries_the_session_name():
    mgr = LiveManager()
    await mgr.ingest_spans(decode_json_document(request([chat(trace="x")], {"session.id": "sid-1"}), strict=True))
    await mgr.ingest_spans(decode_json_document(request([chat(trace="y")], named("given")), strict=True))
    for session_id in mgr.sessions:
        document = mgr.session_document(session_id)
        names = {
            a["value"]["stringValue"]
            for rs in document["resourceSpans"]
            for a in rs["resource"]["attributes"]
            if a["key"] == "agentevals.session_name"
        }
        assert names == {session_id}
