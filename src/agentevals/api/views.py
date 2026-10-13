"""Views of sessions and conversations as ``agentevals.api.v1alpha1`` messages.

Built from the GenAI model, so every client renders the same turns without deriving them. The
builders are pure and need no server: callers pass copied facts and frozen spans, never live
store objects, so a builder can run in a worker thread while ingest goes on.

Every view encodes as protojson: texts and tool payloads are capped, non finite numbers become
null and bytes become base64 strings, each with a gap where something was changed.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google.protobuf import json_format, struct_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from ..genai.messages import text_of
from ..genai.model import Conversation, ExternalEvaluation, Gap, LlmCall, ToolCall, Turn, Usage
from ..otel.encode import attributes_to_json
from ..otel.model import Resource, Span, attr_str
from .v1alpha1 import views_pb2

MAX_TEXT_CHARS = 64 * 1024
MAX_VALUE_BYTES = 64 * 1024
MAX_VALUE_DEPTH = 32
# A JSON number is a double, exact for integers up to 2^53.
MAX_SAFE_INTEGER = 2**53
SHORT_ID_LENGTH = 6
# Generated ids (UUIDs, trace ids) are often time ordered, so only their tails tell them apart.
_GENERATED_ID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}"
)

GAP_TRUNCATED = "truncated"
GAP_NO_TURNS = "no_turns"

AGENT_NAME = "gen_ai.agent.name"
AGENT_VERSION = "gen_ai.agent.version"
SERVICE_NAME = "service.name"
SERVICE_VERSION = "service.version"

_STATUS = {
    "unset": trace_pb2.Status.STATUS_CODE_UNSET,
    "ok": trace_pb2.Status.STATUS_CODE_OK,
    "error": trace_pb2.Status.STATUS_CODE_ERROR,
}

SpanIndex = Mapping[tuple[str, str], Span]


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """What a session summary needs from the store, copied while no ingest can change it."""

    session_id: str
    received_at: datetime
    is_complete: bool
    trace_ids: tuple[str, ...]
    span_count: int
    eval_set_id: str | None = None


def session_view(
    facts: SessionFacts,
    conversation: Conversation | None = None,
    spans: SpanIndex | None = None,
    *,
    include_turns: bool = True,
) -> views_pb2.Session:
    """The session's summary and turns. Without a conversation, a summary from the facts alone."""
    spans = spans or {}
    turns = [turn_view(t, spans) for t in conversation.turns] if conversation else []
    session = views_pb2.Session(summary=_summary(facts, conversation, spans, turns))
    if include_turns:
        session.turns.extend(turns)
    return session


def conversation_view(conversation: Conversation, spans: SpanIndex) -> views_pb2.ConversationView:
    return views_pb2.ConversationView(
        key=conversation.key,
        conversation_id=conversation.conversation_id or "",
        trace_ids=conversation.trace_ids,
        turns=[turn_view(t, spans) for t in conversation.turns],
    )


def turn_view(turn: Turn, spans: SpanIndex) -> views_pb2.TurnView:
    gaps: list[Gap] = list(turn.warnings)
    view = views_pb2.TurnView(
        index=turn.index,
        trace_id=turn.ref.trace_id,
        span_id=turn.ref.span_id,
        status=_STATUS.get(turn.status, trace_pb2.Status.STATUS_CODE_UNSET),
        error_type=turn.error_type or "",
        agent_name=turn.agent_name or "",
        content_captured=turn.content_captured,
        tool_calls=[_tool_call(t, spans, gaps) for t in turn.tool_calls],
        model_calls=[_model_call(c) for c in turn.llm_calls],
        usage=_usage(turn.usage),
        external_evaluations=[_external_evaluation(e) for e in turn.external_evaluations],
    )
    _set_times(view, turn.start_ns, turn.end_ns)
    user_text = text_of(turn.user_input)
    if user_text is not None:
        view.user_text = _capped_text(user_text, "user text", gaps)
    answer_text = text_of(turn.final_output)
    if answer_text is not None:
        view.answer_text = _capped_text(answer_text, "answer text", gaps)
    view.gaps.extend(views_pb2.Gap(code=g.code, reason=g.reason) for g in dict.fromkeys(gaps))
    return view


def _summary(
    facts: SessionFacts,
    conversation: Conversation | None,
    spans: SpanIndex,
    turns: list[views_pb2.TurnView],
) -> views_pb2.SessionSummary:
    source = conversation.turns if conversation else []
    calls = [c for t in source for c in t.llm_calls]
    tools = [tc for t in source for tc in t.tool_calls]
    anchor = _anchor_span(source, spans)
    resource = anchor.resource if anchor is not None else None
    resource_attrs = resource.attributes if resource is not None else {}

    summary = views_pb2.SessionSummary(
        id=facts.session_id,
        short_id=_short_id(facts.session_id),
        agent=_str(resource_attrs.get(AGENT_NAME))
        or next((t.agent_name for t in source if t.agent_name), None)
        or _str(resource_attrs.get(SERVICE_NAME)),
        agent_version=_str(resource_attrs.get(AGENT_VERSION))
        or (attr_str(anchor.attributes, AGENT_VERSION) if anchor is not None else None)
        or _str(resource_attrs.get(SERVICE_VERSION)),
        state=views_pb2.SESSION_STATE_COMPLETE if facts.is_complete else views_pb2.SESSION_STATE_LIVE,
        content_capture=_content_capture(source),
        totals=views_pb2.SessionTotals(
            turns=len(source),
            failed_turns=sum(1 for t in source if t.status == "error"),
            model_calls=len(calls),
            tool_calls=len(tools),
            usage=_usage(_sum_usage(t.usage for t in source)),
            model_calls_with_usage=sum(1 for c in calls if c.usage is not None),
            tool_calls_with_arguments=sum(1 for tc in tools if tc.arguments is not None),
        ),
        models=list(dict.fromkeys(m for c in calls if (m := c.response_model or c.request_model))),
        conversation_id=(conversation.conversation_id if conversation else None) or "",
        trace_ids=list(conversation.trace_ids if conversation and conversation.trace_ids else facts.trace_ids),
        eval_set_id=facts.eval_set_id or "",
        span_count=facts.span_count,
    )
    if resource is not None:
        summary.resource.CopyFrom(_resource(resource))
    if source:
        _set_times(summary, min(t.start_ns for t in source), max(t.end_ns for t in source))
    else:
        summary.started_at.FromDatetime(facts.received_at)
    if turns and turns[0].HasField("user_text"):
        summary.first_user_text = turns[0].user_text
    if facts.is_complete and facts.span_count and not source:
        summary.gaps.append(views_pb2.Gap(code=GAP_NO_TURNS, reason="no span forms a GenAI turn"))
    return summary


def _short_id(session_id: str) -> str:
    return session_id[-SHORT_ID_LENGTH:] if _GENERATED_ID.fullmatch(session_id) else session_id


def _anchor_span(turns: list[Turn], spans: SpanIndex) -> Span | None:
    """The first turn's anchor span, else the earliest span: the service that produced the session."""
    for turn in turns:
        span = spans.get((turn.ref.trace_id, turn.ref.span_id))
        if span is not None:
            return span
    return min(spans.values(), key=lambda s: s.start_time_unix_nano, default=None)


def _content_capture(turns: list[Turn]) -> int:
    captured = {t.content_captured for t in turns}
    if not captured:
        return views_pb2.CONTENT_CAPTURE_UNSPECIFIED
    if captured == {True}:
        return views_pb2.CONTENT_CAPTURE_ON
    if captured == {False}:
        return views_pb2.CONTENT_CAPTURE_OFF
    return views_pb2.CONTENT_CAPTURE_PARTIAL


def _tool_call(tool: ToolCall, spans: SpanIndex, gaps: list[Gap]) -> views_pb2.ToolCallView:
    view = views_pb2.ToolCallView(
        span_id=tool.ref.span_id if tool.ref else "",
        name=tool.name,
        is_error=tool.is_error,
        error_type=tool.error_type or "",
    )
    if tool.call_id is not None:
        view.call_id = tool.call_id
    if tool.arguments is not None:
        view.arguments.CopyFrom(_value(tool.arguments, f"tool {tool.name} arguments", gaps))
    if tool.result is not None:
        view.result.CopyFrom(_value(tool.result, f"tool {tool.name} result", gaps))
    span = spans.get((tool.ref.trace_id, tool.ref.span_id)) if tool.ref else None
    if span is not None:
        _set_times(view, span.start_time_unix_nano, span.end_time_unix_nano)
    elif tool.start_ns:
        view.started_at.FromNanoseconds(tool.start_ns)
    return view


def _model_call(call: LlmCall) -> views_pb2.ModelCallView:
    view = views_pb2.ModelCallView(
        span_id=call.ref.span_id,
        operation=call.operation,
        provider=call.provider or "",
        request_model=call.request_model or "",
        response_model=call.response_model or "",
        response_id=call.response_id or "",
        usage=_usage(call.usage),
        finish_reasons=call.finish_reasons,
        status=_STATUS.get(call.status, trace_pb2.Status.STATUS_CODE_UNSET),
        error_type=call.error_type or "",
        agent_name=call.agent_name or "",
    )
    _set_times(view, call.start_ns, call.end_ns)
    return view


def _external_evaluation(evaluation: ExternalEvaluation) -> views_pb2.EvaluationResult:
    view = views_pb2.EvaluationResult(
        name=evaluation.name,
        trace_id=evaluation.ref.trace_id,
        span_id=evaluation.ref.span_id,
        source_service=evaluation.source_service or "",
    )
    if evaluation.score_value is not None and math.isfinite(evaluation.score_value):
        view.score_value = evaluation.score_value
    if evaluation.score_label is not None:
        view.score_label = evaluation.score_label
    if evaluation.explanation is not None:
        view.explanation = evaluation.explanation
    return view


def _usage(usage: Usage | None) -> views_pb2.Usage:
    view = views_pb2.Usage()
    if usage is not None:
        for name, value in usage.model_dump().items():
            if value is not None:
                setattr(view, name, value)
    return view


def _sum_usage(usages: Iterable[Usage]) -> Usage:
    total = Usage()
    for usage in usages:
        total = total + usage
    return total


def _resource(resource: Resource) -> resource_pb2.Resource:
    """The resource through the OTLP/JSON encoder, so both encodings agree on every value."""
    out = resource_pb2.Resource(dropped_attributes_count=resource.dropped_attributes_count)
    json_format.ParseDict({"attributes": attributes_to_json(resource.attributes)}, out)
    return out


def _set_times(view: Any, start_ns: int | None, end_ns: int | None) -> None:
    if not start_ns:
        return
    view.started_at.FromNanoseconds(start_ns)
    if end_ns and end_ns >= start_ns:
        if "ended_at" in view.DESCRIPTOR.fields_by_name:
            view.ended_at.FromNanoseconds(end_ns)
        view.duration.FromNanoseconds(end_ns - start_ns)


def _capped_text(text: str, what: str, gaps: list[Gap]) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    gaps.append(Gap(code=GAP_TRUNCATED, reason=f"{what} cut to {MAX_TEXT_CHARS} characters"))
    return text[:MAX_TEXT_CHARS]


def _value(obj: Any, what: str, gaps: list[Gap]) -> struct_pb2.Value:
    changed: list[str] = []
    safe = _json_safe(obj, 0, changed)
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_VALUE_BYTES:
        safe = encoded.encode()[:MAX_VALUE_BYTES].decode(errors="ignore")
        changed.append(f"cut to {MAX_VALUE_BYTES} bytes of JSON text")
    if changed:
        gaps.append(Gap(code=GAP_TRUNCATED, reason=f"{what}: {', '.join(dict.fromkeys(changed))}"))
    value = struct_pb2.Value()
    json_format.ParseDict(safe, value)
    return value


def json_safe(obj: Any) -> Any:
    """``obj`` with JSON values only, as a ``google.protobuf.Struct`` or ``Value`` holds them."""
    return _json_safe(obj, 0, [])


def _json_safe(obj: Any, depth: int, changed: list[str]) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        if abs(obj) <= MAX_SAFE_INTEGER:
            return obj
        changed.append("integers beyond 2^53 as text")
        return str(obj)
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        changed.append("non finite numbers set to null")
        return None
    if isinstance(obj, (bytes, bytearray)):
        changed.append("bytes as base64")
        return base64.b64encode(bytes(obj)).decode("ascii")
    if depth >= MAX_VALUE_DEPTH:
        changed.append(f"nesting cut at depth {MAX_VALUE_DEPTH}")
        return None
    if isinstance(obj, Mapping):
        return {str(k): _json_safe(v, depth + 1, changed) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, depth + 1, changed) for v in obj]
    changed.append("values that are not JSON as text")
    return str(obj)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
