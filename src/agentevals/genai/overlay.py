"""Read only normalization overlay: one canonical view per span.

The overlay never mutates telemetry. It starts from the span's own attributes and fills
canonical keys that are missing, from these sources in order (first hit wins, and a message
list is always taken whole from one source):

1. the current semantic convention key on the span
2. a deprecated alias on the span (:data:`semconv.DEPRECATED_ALIASES`)
3. the ``gen_ai.client.inference.operation.details`` event: span event first, then a log
   record on the same span
4. deprecated per message events (``gen_ai.user.message``, ``gen_ai.choice``, ...): span
   events first, then log records on the same span
5. Google ADK legacy attributes (``gcp.vertex.agent.*``), only on ADK spans
6. legacy indexed attributes (``gen_ai.prompt.N.*`` / ``gen_ai.completion.N.*``), see
   :func:`_legacy_indexed`

The long tail of other dialects is normalized in the Collector (``gen_ai_normalizer``,
``transform``), not here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..otel.model import LogRecord, Span, SpanEvent, attr_str, parse_json_value
from . import semconv as sc
from .messages import (
    Message,
    content_to_parts,
    google_content_to_message,
    google_parts,
    openai_tool_calls_to_parts,
    parse_messages,
    parse_parts,
    text_part,
)

ADK_SCOPE = "gcp.vertex.agent"
ADK_PREFIX = "gcp.vertex.agent."
ADK_LLM_REQUEST = "gcp.vertex.agent.llm_request"
ADK_LLM_RESPONSE = "gcp.vertex.agent.llm_response"
ADK_TOOL_ARGS = "gcp.vertex.agent.tool_call_args"
ADK_TOOL_RESPONSE = "gcp.vertex.agent.tool_response"
ADK_INVOCATION_ID = "gcp.vertex.agent.invocation_id"
ADK_SESSION_ID = "gcp.vertex.agent.session_id"
ADK_MERGED_TOOL_NAME = "(merged tools)"
ADK_MERGED_SPAN_NAME = "execute_tool (merged)"

MESSAGE_KEYS = (sc.INPUT_MESSAGES, sc.OUTPUT_MESSAGES, sc.SYSTEM_INSTRUCTIONS, sc.TOOL_DEFINITIONS)
DETAILS_SCALAR_KEYS = (
    sc.REQUEST_MODEL,
    sc.RESPONSE_MODEL,
    sc.RESPONSE_ID,
    sc.RESPONSE_FINISH_REASONS,
    sc.PROVIDER_NAME,
    sc.USAGE_INPUT_TOKENS,
    sc.USAGE_OUTPUT_TOKENS,
    sc.USAGE_CACHE_READ_INPUT_TOKENS,
    sc.USAGE_CACHE_WRITE_INPUT_TOKENS,
    sc.USAGE_REASONING_OUTPUT_TOKENS,
)


@dataclass(frozen=True, slots=True)
class SpanView:
    """Canonical attributes of one span. Message keys hold parsed lists (or ``None``)."""

    span: Span
    attrs: Mapping[str, Any]
    operation: str | None
    ignored: bool
    is_adk: bool
    sources: Mapping[str, str] = field(default_factory=dict)
    framework_ids: Mapping[str, str] = field(default_factory=dict)
    exception_event: bool = False

    def messages(self, key: str) -> list[Message] | None:
        value = self.attrs.get(key)
        return value if isinstance(value, list) and value else None


def _is_adk(span: Span) -> bool:
    return span.scope.name == ADK_SCOPE or any(k.startswith(ADK_PREFIX) for k in span.attributes)


def _parse_message_key(key: str, value: Any) -> list | None:
    if value is None:
        return None
    if key == sc.SYSTEM_INSTRUCTIONS:
        return parse_parts(value)
    if key == sc.TOOL_DEFINITIONS:
        data = parse_json_value(value)
        data = [data] if isinstance(data, Mapping) else data
        defs = [dict(d) for d in data if isinstance(d, Mapping)] if isinstance(data, list) else []
        return defs or None
    return parse_messages(value)


class _Filler:
    def __init__(self, raw: Mapping[str, Any]):
        self.attrs: dict[str, Any] = dict(raw)
        self.sources: dict[str, str] = {}
        for key in MESSAGE_KEYS:
            parsed = _parse_message_key(key, raw.get(key))
            if parsed is None:
                self.attrs.pop(key, None)
            else:
                self.attrs[key] = parsed
                self.sources[key] = "span"

    def missing(self, key: str) -> bool:
        return self.attrs.get(key) in (None, [], "")

    def fill(self, key: str, value: Any, source: str) -> None:
        if value is None or value == [] or not self.missing(key):
            return
        self.attrs[key] = value
        self.sources[key] = source


def _event_attrs(span: Span, logs: Sequence[LogRecord], name: str) -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = [("span_event", e.attributes) for e in span.events if e.name == name]
    found += [("log", r.attributes) for r in logs if r.event_name == name]
    return found


def _apply_details(f: _Filler, span: Span, logs: Sequence[LogRecord]) -> None:
    for where, attrs in _event_attrs(span, logs, sc.EVENT_INFERENCE_DETAILS):
        for key in MESSAGE_KEYS:
            f.fill(key, _parse_message_key(key, attrs.get(key)), f"details_{where}")
        for key in DETAILS_SCALAR_KEYS:
            f.fill(key, attrs.get(key), f"details_{where}")


def _body(value: Any) -> Any:
    if isinstance(value, str):
        parsed = parse_json_value(value)
        return parsed if isinstance(parsed, Mapping) else {"content": value}
    return value if isinstance(value, Mapping) else {}


def _legacy_event_messages(span: Span, logs: Sequence[LogRecord]) -> tuple[list, list, list]:
    """Deprecated message events: span events first, then logs on this span, each in time order."""
    events: list[tuple[str, Any]] = []
    for ev in span.events:
        if ev.name in sc.LEGACY_MESSAGE_EVENTS:
            events.append((ev.name, dict(ev.attributes)))
    if not events:
        ordered = sorted(
            (r for r in logs if r.event_name in sc.LEGACY_MESSAGE_EVENTS),
            key=lambda r: r.time_unix_nano or r.observed_time_unix_nano or 0,
        )
        events = [(r.event_name, r.body) for r in ordered]

    system: list = []
    inputs: list[Message] = []
    outputs: list[Message] = []
    for name, raw in events:
        body = _body(raw)
        content = body.get("content")
        if name == sc.EVENT_SYSTEM_MESSAGE:
            system.extend(content_to_parts(content))
        elif name == sc.EVENT_USER_MESSAGE:
            message = (
                google_content_to_message(content) if isinstance(content, Mapping) and "parts" in content else None
            )
            inputs.append(message or {"role": "user", "parts": content_to_parts(content)})
        elif name == sc.EVENT_ASSISTANT_MESSAGE:
            parts = content_to_parts(content) + openai_tool_calls_to_parts(body.get("tool_calls"))
            inputs.append({"role": "assistant", "parts": parts})
        elif name == sc.EVENT_TOOL_MESSAGE:
            parsed = parse_json_value(content) if isinstance(content, str) else content
            response = content if parsed is None else parsed
            inputs.append(
                {"role": "tool", "parts": [{"type": "tool_call_response", "id": body.get("id"), "response": response}]}
            )
        elif name == sc.EVENT_CHOICE:
            message = body.get("message") if isinstance(body.get("message"), Mapping) else None
            if message is not None:
                parts = content_to_parts(message.get("content")) + openai_tool_calls_to_parts(message.get("tool_calls"))
            elif isinstance(content, Mapping) and "parts" in content:
                parts = google_parts(content["parts"])
            else:
                parts = content_to_parts(content)
            out: Message = {"role": "assistant", "parts": parts}
            if body.get("finish_reason") is not None:
                out["finish_reason"] = body["finish_reason"]
            outputs.append(out)
    return [p for p in system if p], [m for m in inputs if m["parts"]], [m for m in outputs if m["parts"]]


def _apply_legacy_events(f: _Filler, span: Span, logs: Sequence[LogRecord]) -> None:
    system, inputs, outputs = _legacy_event_messages(span, logs)
    f.fill(sc.SYSTEM_INSTRUCTIONS, parse_parts(system) if system else None, "legacy_events")
    f.fill(sc.INPUT_MESSAGES, parse_messages(inputs) if inputs else None, "legacy_events")
    f.fill(sc.OUTPUT_MESSAGES, parse_messages(outputs) if outputs else None, "legacy_events")
    reasons = [m["finish_reason"] for m in outputs if isinstance(m.get("finish_reason"), str)]
    f.fill(sc.RESPONSE_FINISH_REASONS, [r.lower() for r in reasons] or None, "legacy_events")


def _apply_adk(f: _Filler, raw: Mapping[str, Any]) -> None:
    request = parse_json_value(raw.get(ADK_LLM_REQUEST))
    if isinstance(request, Mapping):
        contents = request.get("contents")
        if isinstance(contents, list):
            messages = [m for m in (google_content_to_message(c) for c in contents) if m and m["parts"]]
            f.fill(sc.INPUT_MESSAGES, parse_messages(messages) if messages else None, "adk")
        config = request.get("config") if isinstance(request.get("config"), Mapping) else {}
        instruction = config.get("system_instruction")
        if isinstance(instruction, Mapping):
            instruction_parts = google_parts(instruction.get("parts") or [])
        elif isinstance(instruction, str) and instruction:
            instruction_parts = [text_part(instruction)]
        else:
            instruction_parts = []
        f.fill(sc.SYSTEM_INSTRUCTIONS, instruction_parts or None, "adk")
        tools = []
        for entry in config.get("tools") or []:
            for decl in (entry.get("function_declarations") or []) if isinstance(entry, Mapping) else []:
                if isinstance(decl, Mapping) and decl.get("name"):
                    tools.append(
                        {
                            "type": "function",
                            "name": decl["name"],
                            "description": decl.get("description"),
                            "parameters": decl.get("parameters") or decl.get("parameters_json_schema"),
                        }
                    )
        f.fill(sc.TOOL_DEFINITIONS, tools or None, "adk")
        if isinstance(request.get("model"), str):
            f.fill(sc.REQUEST_MODEL, request["model"], "adk")

    response = parse_json_value(raw.get(ADK_LLM_RESPONSE))
    if isinstance(response, Mapping):
        message = google_content_to_message(response.get("content"), default_role="assistant")
        if message and message["parts"]:
            f.fill(sc.OUTPUT_MESSAGES, parse_messages([message]), "adk")
        if isinstance(response.get("finish_reason"), str):
            f.fill(sc.RESPONSE_FINISH_REASONS, [response["finish_reason"].lower()], "adk")
        if isinstance(response.get("model_version"), str):
            f.fill(sc.RESPONSE_MODEL, response["model_version"], "adk")
        usage = response.get("usage_metadata") if isinstance(response.get("usage_metadata"), Mapping) else {}
        for src, dst in (
            ("prompt_token_count", sc.USAGE_INPUT_TOKENS),
            ("candidates_token_count", sc.USAGE_OUTPUT_TOKENS),
            ("cached_content_token_count", sc.USAGE_CACHE_READ_INPUT_TOKENS),
            ("thoughts_token_count", sc.USAGE_REASONING_OUTPUT_TOKENS),
        ):
            if isinstance(usage.get(src), int):
                f.fill(dst, usage[src], "adk")

    args = raw.get(ADK_TOOL_ARGS)
    if args is not None and args != "N/A":
        parsed = parse_json_value(args)
        f.fill(sc.TOOL_CALL_ARGUMENTS, args if parsed is None else parsed, "adk")
    result = raw.get(ADK_TOOL_RESPONSE)
    if result is not None:
        parsed = parse_json_value(result)
        value = result if parsed is None else parsed
        if not _is_tool_placeholder(value):
            f.fill(sc.TOOL_CALL_RESULT, value, "adk")


def _is_tool_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return value in sc.TOOL_PLACEHOLDERS
    if isinstance(value, Mapping) and set(value) == {"result"}:
        return value["result"] in sc.TOOL_PLACEHOLDERS
    return False


def _legacy_indexed(f: _Filler, raw: Mapping[str, Any]) -> None:
    """OpenLLMetry indexed messages: ``gen_ai.prompt.N.*`` and ``gen_ai.completion.N.*``.

    Legacy shim, kept for producers on OpenLLMetry 0.52 (kagent Python agents as of kagent
    v1.0.0-alpha8) and for traces already stored in that shape. Remove once no supported
    kagent release emits it (kagent#2907, "Moving Python off OpenLLMetry") and the Collector
    ``gen_ai_normalizer`` is the documented path for OpenLLMetry producers.
    """
    for prefix, key in (("gen_ai.prompt.", sc.INPUT_MESSAGES), ("gen_ai.completion.", sc.OUTPUT_MESSAGES)):
        if not f.missing(key):
            continue
        by_index: dict[int, dict[str, Any]] = {}
        for name, value in raw.items():
            if not name.startswith(prefix):
                continue
            index, _, sub = name[len(prefix) :].partition(".")
            if index.isdigit() and sub:
                by_index.setdefault(int(index), {})[sub] = value
        messages: list[Message] = []
        for index in sorted(by_index):
            entry = by_index[index]
            role = entry.get("role") if isinstance(entry.get("role"), str) else "user"
            content = entry.get("content")
            if role == "tool":
                parsed = parse_json_value(content) if isinstance(content, str) else content
                parts = [
                    {
                        "type": "tool_call_response",
                        "id": entry.get("tool_call_id"),
                        "response": content if parsed is None else parsed,
                    }
                ]
            else:
                parts = content_to_parts(content)
                calls: dict[int, dict[str, Any]] = {}
                for sub, value in entry.items():
                    head, _, rest = sub.partition(".")
                    if head == "tool_calls":
                        number, _, field_name = rest.partition(".")
                        if number.isdigit() and field_name:
                            calls.setdefault(int(number), {})[field_name] = value
                for number in sorted(calls):
                    call = calls[number]
                    args = call.get("arguments")
                    parsed_args = parse_json_value(args) if isinstance(args, str) else args
                    if isinstance(call.get("name"), str):
                        parts.append(
                            {
                                "type": "tool_call",
                                "id": call.get("id"),
                                "name": call["name"],
                                "arguments": args if parsed_args is None else parsed_args,
                            }
                        )
            message: Message = {"role": role, "parts": parts}
            if isinstance(entry.get("finish_reason"), str):
                message["finish_reason"] = entry["finish_reason"]
            messages.append(message)
        f.fill(key, parse_messages(messages) if messages else None, "legacy_indexed")


def _operation(span: Span, is_adk: bool) -> str | None:
    op = attr_str(span.attributes, sc.OPERATION_NAME)
    if op:
        return op
    if is_adk:
        if span.name == "call_llm":
            return sc.OP_GENERATE_CONTENT
        if span.name == "invocation":
            return sc.OP_INVOKE_WORKFLOW
    raw = span.attributes
    if raw.get(sc.REQUEST_MODEL) and (raw.get("gen_ai.system") or raw.get(sc.PROVIDER_NAME)) and _records_a_call(raw):
        return sc.OP_CHAT
    return None


_CALL_RECORD_PREFIXES = ("gen_ai.usage.", "gen_ai.prompt.", "gen_ai.completion.")


def _records_a_call(raw: Mapping[str, Any]) -> bool:
    """Usage or messages on the span itself. Model and provider alone are identity: some producers
    copy their resource attributes onto every span, which would make each of them a model call."""
    return any(k.startswith(_CALL_RECORD_PREFIXES) or k in (sc.INPUT_MESSAGES, sc.OUTPUT_MESSAGES) for k in raw)


def overlay(span: Span, logs: Sequence[LogRecord] = ()) -> SpanView:
    """Build the canonical view of ``span``. ``logs`` are the log records joined to it."""
    raw = span.attributes
    f = _Filler(raw)
    is_adk = _is_adk(span)

    for alias, key in sc.DEPRECATED_ALIASES.items():
        if alias not in raw:
            continue
        value = raw[alias]
        if alias == "gen_ai.system" and value in sc.FRAMEWORK_SYSTEM_VALUES:
            continue
        if key == sc.RESPONSE_FINISH_REASONS and isinstance(value, str):
            value = [value]
        f.fill(key, value, f"alias:{alias}")

    _apply_details(f, span, logs)
    _apply_legacy_events(f, span, logs)
    if is_adk:
        _apply_adk(f, raw)
    _legacy_indexed(f, raw)

    for key in (sc.TOOL_NAME, sc.TOOL_CALL_ID):
        if f.attrs.get(key) in sc.TOOL_PLACEHOLDERS:
            f.attrs.pop(key)

    ignored = span.name == ADK_MERGED_SPAN_NAME or raw.get(sc.TOOL_NAME) == ADK_MERGED_TOOL_NAME
    framework_ids = {}
    for key, label in ((ADK_INVOCATION_ID, "adk.invocation_id"), (ADK_SESSION_ID, "adk.session_id")):
        value = attr_str(raw, key)
        if value:
            framework_ids[label] = value
    exception_event = any(e.name == sc.EVENT_OPERATION_EXCEPTION for e in span.events) or any(
        r.event_name == sc.EVENT_OPERATION_EXCEPTION for r in logs
    )
    return SpanView(
        span=span,
        attrs=f.attrs,
        operation=_operation(span, is_adk),
        ignored=ignored,
        is_adk=is_adk,
        sources=f.sources,
        framework_ids=framework_ids,
        exception_event=exception_event,
    )


def span_events_named(span: Span, name: str) -> list[SpanEvent]:
    return [e for e in span.events if e.name == name]
