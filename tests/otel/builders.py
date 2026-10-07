"""OTLP/JSON builders and store drivers shared by the store, live and extraction tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agentevals.genai.routing import GenAIRoutingPolicy
from agentevals.otel.decode import DecodeResult, decode_json_document
from agentevals.otel.store import IngestResult, Limits, TelemetryStore


def tid(label: str) -> str:
    return hashlib.sha256(f"trace:{label}".encode()).hexdigest()[:32]


def sid(label: str) -> str:
    return hashlib.sha256(f"span:{label}".encode()).hexdigest()[:16]


def attr(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    return {"key": key, "value": {"stringValue": value}}


def span(
    trace: str = "t1",
    span_id: str = "s1",
    parent: str | None = None,
    name: str = "span",
    attrs: dict | None = None,
    start: int = 1_000,
    end: int = 2_000,
    flags: int | None = None,
) -> dict:
    data = {
        "traceId": tid(trace),
        "spanId": sid(span_id),
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [attr(k, v) for k, v in (attrs or {}).items()],
    }
    if parent:
        data["parentSpanId"] = sid(parent)
    if flags is not None:
        data["flags"] = flags
    return data


def user(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "content": text}]}


def assistant(text: str | None = None, tool_calls: list[tuple[str, str, dict]] = ()) -> dict:
    parts = [{"type": "text", "content": text}] if text else []
    parts += [{"type": "tool_call", "id": cid, "name": name, "arguments": args} for cid, name, args in tool_calls]
    return {"role": "assistant", "parts": parts}


def tool_result(call_id: str, name: str, response: Any) -> dict:
    return {
        "role": "tool",
        "parts": [{"type": "tool_call_response", "id": call_id, "name": name, "response": response}],
    }


def chat(
    trace: str = "t1",
    span_id: str = "c1",
    parent: str | None = None,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    tokens: tuple[int, int] = (10, 3),
    start: int = 1_000,
    end: int = 2_000,
    extra: dict | None = None,
    content: bool = True,
    finish: str | None = None,
) -> dict:
    attrs: dict[str, Any] = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-test"}
    if content:
        attrs["gen_ai.input.messages"] = inputs if inputs is not None else [user("hello")]
        attrs["gen_ai.output.messages"] = outputs if outputs is not None else [assistant("hi")]
    attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"] = tokens
    attrs.update(extra or {})
    data = span(trace, span_id, parent, "chat gpt-test", attrs, start, end)
    if finish:
        data["attributes"].append(
            {"key": "gen_ai.response.finish_reasons", "value": {"arrayValue": {"values": [{"stringValue": finish}]}}}
        )
    return data


def agent(trace: str = "t1", span_id: str = "a1", parent: str | None = None, name: str = "assistant", **kw) -> dict:
    return span(
        trace,
        span_id,
        parent,
        f"invoke_agent {name}",
        {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name},
        **kw,
    )


def request(spans: list[dict], resource: dict | None = None, scope: str = "test") -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [attr(k, v) for k, v in (resource or {}).items()]},
                "scopeSpans": [{"scope": {"name": scope}, "spans": spans}],
            }
        ]
    }


def log_record(
    trace: str = "t1", span_id: str | None = None, event: str = "gen_ai.user.message", body: Any = None, **extra
) -> dict:
    record = {
        "eventName": event,
        "observedTimeUnixNano": "1500",
        "traceId": tid(trace),
        "body": {"stringValue": json.dumps(body if body is not None else {"content": "hello"})},
        "attributes": [],
        **extra,
    }
    if span_id:
        record["spanId"] = sid(span_id)
    return record


def log_request(records: list[dict], resource: dict | None = None, scope: str = "test") -> dict:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [attr(k, v) for k, v in (resource or {}).items()]},
                "scopeLogs": [{"scope": {"name": scope}, "logRecords": records}],
            }
        ]
    }


def named(name: str, **more: Any) -> dict:
    return {"agentevals.session_name": name, **more}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(**limits: Any) -> tuple[TelemetryStore, FakeClock]:
    clock = FakeClock()
    return TelemetryStore(GenAIRoutingPolicy(), Limits(**limits), clock=clock), clock


def _units(items) -> list[list]:
    units: dict = {}
    for item in items:
        units.setdefault((id(item.resource), item.trace_id), []).append(item)
    return list(units.values())


def ingest(store: TelemetryStore, document: dict) -> IngestResult:
    decoded: DecodeResult = decode_json_document(document, strict=True)
    total = IngestResult()
    for unit in _units(decoded.spans):
        total.extend(store.ingest_spans(unit))
    for unit in _units(decoded.logs):
        total.extend(store.ingest_logs(unit))
    return total
