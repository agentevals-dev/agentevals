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
            "llm_calls": len(t.llm_calls),
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
            "llm_calls": t.get("llm_calls"),
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
        for optional in ("tokens", "llm_calls"):
            if w[optional] is None:
                g = {**g, optional: None}
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


# Ordered replay: deliver a fixture the way exporters would, tick the store's clock
# between deliveries, and check that sessions are keyed and completed correctly.
# per_span sends one item at a time in end time order. bsp_5s models the SDK batch
# processors of each process (resource): spans every OTEL_BSP_SCHEDULE_DELAY (5 s) or
# as soon as OTEL_BSP_MAX_EXPORT_BATCH_SIZE (512) are waiting, logs every
# OTEL_BLRP_SCHEDULE_DELAY (1 s). Each process gets a fixed phase so runs are
# deterministic. The two schedules expose different ordering bugs.

SPAN_DELAY_NS = 5_000_000_000
LOG_DELAY_NS = 1_000_000_000
MAX_EXPORT_BATCH = 512
PHASE_STEP_NS = 1_700_000_000


def _log_time(record) -> int:
    return record.time_unix_nano or record.observed_time_unix_nano or 0


def _per_span(spans, logs, t0):
    items = [(s.end_time_unix_nano, 0, s) for s in spans] + [(_log_time(r), 1, r) for r in logs]
    return [(when, [item]) for when, _, item in sorted(items, key=lambda i: i[:2])]


def _bsp_5s(spans, logs, t0):
    processes: dict[str, int] = {}
    for item in [*spans, *logs]:
        processes.setdefault(json.dumps(item.resource.attributes, sort_keys=True, default=str), len(processes))
    out = []
    for signal, items, when, delay in (
        (0, spans, lambda s: s.end_time_unix_nano, SPAN_DELAY_NS),
        (1, logs, _log_time, LOG_DELAY_NS),
    ):
        by_process: dict[int, list] = {}
        for item in sorted(items, key=when):
            key = json.dumps(item.resource.attributes, sort_keys=True, default=str)
            by_process.setdefault(processes[key], []).append(item)
        for index, members in by_process.items():
            phase = (index * PHASE_STEP_NS) % delay
            batch, due = [], None
            for item in members:
                flush = t0 + phase - ((t0 + phase - when(item)) // delay) * delay
                if batch and flush != due:
                    out.append((due, index, signal, batch))
                    batch = []
                batch.append(item)
                due = flush
                if len(batch) == MAX_EXPORT_BATCH:
                    out.append((when(item), index, signal, batch))
                    batch = []
            if batch:
                out.append((due, index, signal, batch))
    return [(when, batch) for when, _, _, batch in sorted(out, key=lambda d: d[:3])]


SCHEDULES = {"per_span": _per_span, "bsp_5s": _bsp_5s}


def _top_span_delivery(deliveries, spans) -> dict[str, int]:
    """Delivery index of each trace's top span: its root, else its outermost span with a parent elsewhere."""
    present = {(s.trace_id, s.span_id) for s in spans}
    delivered = {id(item): i for i, (_, batch) in enumerate(deliveries) for item in batch}
    tops: dict[str, tuple] = {}
    for span in spans:
        if span.parent_span_id and (span.trace_id, span.parent_span_id) in present:
            continue
        rank = (bool(span.parent_span_id), span.start_time_unix_nano)
        if span.trace_id not in tops or rank < tops[span.trace_id][0]:
            tops[span.trace_id] = (rank, delivered[id(span)])
    return {trace_id: index for trace_id, (_, index) in tops.items()}


def _replay_document(truth_path: Path) -> dict:
    fixture = _fixture(truth_path)
    if '"resourceSpans"' in fixture.read_text()[:4096]:
        return json.loads(fixture.read_text())
    return encode_traces(load_telemetry(str(fixture)).traces)


@pytest.mark.parametrize("schedule", SCHEDULES)
@pytest.mark.parametrize("truth_path", TRUTHS, ids=_id)
def test_ordered_replay(truth_path, schedule):
    truth = json.loads(truth_path.read_text())
    decoded = decode_json_document(_replay_document(truth_path), strict=True)
    end_times = {s.end_time_unix_nano for s in decoded.spans} - {0}
    if len(end_times) < 2:
        pytest.skip(f"{len(end_times)} distinct span end times, so the order carries no information")

    t0 = min(end_times)
    deliveries = SCHEDULES[schedule](decoded.spans, decoded.logs, t0)
    tops = _top_span_delivery(deliveries, decoded.spans)
    clock = _Clock()
    store = TelemetryStore(GenAIRoutingPolicy(), Limits(), clock=clock)
    completions, reopens = [], []

    for i, (when, batch) in enumerate(deliveries):
        clock.now = (when - t0) / 1e9 + 1.0
        for session_id in store.tick():
            held = set(store.sessions[session_id].trace_ids)
            completions.append((i, session_id, held))
            early = sorted(t for t in held if tops.get(t, -1) >= i)
            assert not early, f"{session_id} completed at {clock.now:.1f}s before the top span of {early} arrived"
        seen = len(store.events)
        spans = [item for item in batch if hasattr(item, "end_time_unix_nano")]
        logs = [item for item in batch if not hasattr(item, "end_time_unix_nano")]
        if spans:
            store.ingest_spans(spans)
        if logs:
            store.ingest_logs(logs)
        reopens += [(i, event[1]) for event in store.events[seen:] if event[0] == "reopened"]

    clock.now += 3600
    end = len(deliveries)
    completions += [(end, sid, set(store.sessions[sid].trace_ids)) for sid in store.tick()]
    assert all(s.is_complete for s in store.sessions.values())

    for session_id, session in store.sessions.items():
        final = set(session.trace_ids)
        settled = min(
            i
            for i, sid, held in completions
            if sid == session_id and held >= final and all(tops.get(t, -1) < i for t in final)
        )
        late = [i for i, sid in reopens if sid == session_id and i >= settled]
        assert not late, f"{session_id} reopened after its final completion"

    if key := truth.get("session_key"):
        assert [s.key for s in store.sessions.values()] == [(key["kind"], key["value"])]

    live: Counter = Counter()
    for session_id in store.sessions:
        snapshots = store.snapshot(session_id)
        traces, _ = build_traces(
            [s for snap in snapshots for s in snap.spans], [r for snap in snapshots for r in snap.logs]
        )
        live.update(
            json.dumps(t, sort_keys=True, default=str) for t in _turns(extract_conversation(traces, session_id))
        )
    batch: Counter = Counter()
    for key, members in group_traces(load_telemetry(str(_fixture(truth_path))).traces, by_conversation=True):
        batch.update(json.dumps(t, sort_keys=True, default=str) for t in _turns(extract_conversation(members, key)))
    assert live == batch
