"""Every fixture in tests/fixtures/otlp must extract to its truth file, from a file and live.

See tests/fixtures/otlp/MANIFEST.md for the corpus and the truth schema.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from agentevals.genai.extract import extract_conversation
from agentevals.genai.grouping import group_traces
from agentevals.genai.messages import text_of
from agentevals.genai.routing import GenAIRoutingPolicy
from agentevals.loader import load_telemetry
from agentevals.otel.decode import decode_json_document
from agentevals.otel.encode import encode_traces
from agentevals.otel.model import build_traces
from agentevals.otel.store import Limits, TelemetryStore

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "otlp"
TRUTHS = sorted(CORPUS.rglob("*.truth.json"))


def _fixture(truth_path: Path) -> Path:
    return truth_path.with_name(truth_path.name.replace(".truth.json", ".json"))


def _id(truth_path: Path) -> str:
    return str(truth_path.relative_to(CORPUS)).replace(".truth.json", "")


def _text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True)
    return value.strip() or None


def _json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
    return None if value in ({}, "", []) else value


def _turns(conversation):
    return [
        {
            "user": _text(text_of(t.user_input)),
            "final": _text(text_of(t.final_output)),
            "intermediate": [_text(x) for x in (text_of([m]) for m in t.intermediate_outputs) if x],
            "agents": sorted(t.agents),
            "tools": [(c.name, _json(c.arguments), _json(c.result), bool(c.is_error)) for c in t.tool_calls],
            "tokens": (t.usage.input_tokens, t.usage.output_tokens),
            "evaluations": len(t.external_evaluations),
        }
        for t in conversation.turns
    ]


def _expected(truth: dict) -> list[dict]:
    return [
        {
            "user": _text(t["user_text"]),
            "final": _text(t["final_text"]),
            "intermediate": [_text(x) for x in t["intermediate_texts"]],
            "agents": sorted(t["agents"]),
            "tools": [(c["name"], _json(c["args"]), _json(c["result"]), bool(c["is_error"])) for c in t["tool_calls"]],
            "tokens": (t["tokens"]["input"], t["tokens"]["output"]) if t.get("tokens") else None,
            "evaluations": len(t.get("external_evaluations", [])),
        }
        for t in truth["turns"]
    ]


@pytest.mark.parametrize("truth_path", TRUTHS, ids=_id)
def test_file_extraction_matches_truth(truth_path):
    truth = json.loads(truth_path.read_text())
    conversation = extract_conversation(load_telemetry(str(_fixture(truth_path))).traces)
    got, want = _turns(conversation), _expected(truth)
    if truth.get("extra_turns_allowed"):
        wanted = {t["user"] for t in want}
        got = [t for t in got if t["user"] in wanted]

    assert len(got) == len(want), [t["user"] for t in got]
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        if w["tokens"] is None:
            g = {**g, "tokens": None}
        assert g == w, f"turn {i + 1}"
    total = (
        sum(t.usage.input_tokens for t in conversation.turns),
        sum(t.usage.output_tokens for t in conversation.turns),
    )
    assert total == (truth["tokens"]["input"], truth["tokens"]["output"])


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _ingest_by_unit(items, apply):
    units: dict = {}
    for item in items:
        units.setdefault((id(item.resource), item.trace_id), []).append(item)
    for unit in units.values():
        apply(unit)


def _live_turns(document: dict, logs_first: bool) -> Counter:
    clock = _Clock()
    store = TelemetryStore(GenAIRoutingPolicy(), Limits(), clock=clock)
    decoded = decode_json_document(document, strict=True)
    assert decoded.rejected_spans == decoded.rejected_logs == 0
    steps = [(decoded.spans, store.ingest_spans), (decoded.logs, store.ingest_logs)]
    for items, apply in reversed(steps) if logs_first else steps:
        _ingest_by_unit(items, apply)
    clock.now += 3600
    store.tick()
    assert all(s.is_complete for s in store.sessions.values())

    turns = []
    for session_id in store.sessions:
        snapshots = store.snapshot(session_id)
        traces, _ = build_traces(
            [s for snap in snapshots for s in snap.spans], [r for snap in snapshots for r in snap.logs]
        )
        turns += _turns(extract_conversation(traces, session_id))
    return Counter(json.dumps(t, sort_keys=True, default=str) for t in turns)


OTLP_TRUTHS = [t for t in TRUTHS if '"resourceSpans"' in _fixture(t).read_text()[:4096]]


@pytest.mark.parametrize("logs_first", [False, True], ids=["spans_first", "logs_first"])
@pytest.mark.parametrize("truth_path", OTLP_TRUTHS, ids=_id)
def test_live_store_matches_file_extraction(truth_path, logs_first):
    traces = load_telemetry(str(_fixture(truth_path))).traces
    batch: Counter = Counter()
    for key, members in group_traces(traces, by_conversation=True):
        batch.update(json.dumps(t, sort_keys=True, default=str) for t in _turns(extract_conversation(members, key)))
    assert _live_turns(encode_traces(traces), logs_first) == batch
