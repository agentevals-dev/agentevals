"""Tolerant readers for GenAI messages and parts.

Messages are plain dicts in the shape of ``gen-ai-input-messages.json`` /
``gen-ai-output-messages.json`` (semantic-conventions-genai 4f85037): ``{"role", "parts", ...}``
with parts such as ``text``, ``tool_call``, ``tool_call_response``, ``reasoning``, ``blob``,
``uri``, ``file``, ``server_tool_call`` and ``compaction``. Unknown parts and extra fields are
kept verbatim. Nothing here raises on malformed input; unusable entries are skipped.

The converters at the bottom turn the legacy shapes the overlay shims read (Google Content
from ADK, OpenAI style bodies from deprecated message events) into that shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..otel.model import parse_json_value
from .semconv import MESSAGE_PLACEHOLDERS

Message = dict[str, Any]
Part = dict[str, Any]

TEXT = "text"
TOOL_CALL = "tool_call"
TOOL_CALL_RESPONSE = "tool_call_response"
REASONING = "reasoning"


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        return None


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip() in MESSAGE_PLACEHOLDERS


def text_part(content: Any) -> Part:
    return {"type": TEXT, "content": content}


def parse_parts(value: Any) -> list[Part] | None:
    """A parts list (``gen_ai.system_instructions``), a JSON string of one, or plain text."""
    data = parse_json_value(value)
    if data is None and isinstance(value, str):
        return None if is_placeholder(value) else [text_part(value)]
    if isinstance(data, str):
        return None if is_placeholder(data) else [text_part(data)]
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, list):
        return None
    parts = [dict(p) for p in data if isinstance(p, Mapping) and isinstance(p.get("type"), str)]
    return parts or None


def normalize_message(raw: Any) -> Message | None:
    if not isinstance(raw, Mapping):
        return None
    role = raw.get("role")
    role = role if isinstance(role, str) and role else "user"
    parts = raw.get("parts")
    if not isinstance(parts, list):
        parts = content_to_parts(raw.get("content")) if "content" in raw else []
    message: Message = {k: v for k, v in raw.items() if k not in ("role", "parts", "content")}
    message["role"] = role
    message["parts"] = [dict(p) for p in parts if isinstance(p, Mapping)]
    return message


def parse_messages(value: Any) -> list[Message] | None:
    """A message list, a JSON string of one, or a single message. ``None`` when absent or empty."""
    if is_placeholder(value):
        return None
    data = value if isinstance(value, (list, tuple, Mapping)) else parse_json_value(value)
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return None
    out = [m for m in (normalize_message(item) for item in data) if m is not None]
    return out if any(has_content([m]) for m in out) else None


def _parts(messages: Iterable[Message] | None, kind: str) -> list[Part]:
    return [p for m in messages or () for p in m.get("parts", ()) if p.get("type") == kind]


def text_of(messages: Iterable[Message] | None) -> str | None:
    """Joined content of the text parts. Non string content is JSON encoded, placeholders skipped."""
    chunks = []
    for part in _parts(messages, TEXT):
        content = part.get("content")
        if is_placeholder(content):
            continue
        text = _as_text(content)
        if text:
            chunks.append(text)
    joined = "".join(chunks)
    return joined or None


def has_text(messages: Iterable[Message] | None) -> bool:
    return text_of(messages) is not None


def has_content(messages: Iterable[Message] | None) -> bool:
    for message in messages or ():
        for part in message.get("parts", ()):
            kind = part.get("type")
            if kind == TEXT:
                if not is_placeholder(part.get("content")) and part.get("content") not in (None, ""):
                    return True
            elif kind:
                return True
    return False


def tool_calls_of(messages: Iterable[Message] | None) -> list[dict[str, Any]]:
    """Client tool calls. ``server_tool_call`` parts run at the provider and are not included."""
    out = []
    for part in _parts(messages, TOOL_CALL):
        name = part.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = part.get("arguments")
        out.append(
            {
                "id": part.get("id") or None,
                "name": name,
                "arguments": parse_json_value(args) if isinstance(args, str) else args,
            }
        )
    return out


def tool_responses_of(messages: Iterable[Message] | None) -> list[dict[str, Any]]:
    out = []
    for part in _parts(messages, TOOL_CALL_RESPONSE):
        response = part.get("response")
        if isinstance(response, str):
            parsed = parse_json_value(response)
            response = response if parsed is None else parsed
        out.append({"id": part.get("id") or None, "name": part.get("name"), "response": response})
    return out


def ends_with_tool_response(messages: Iterable[Message] | None) -> bool:
    """True when the last message returns tool results: role ``tool``, or only ``tool_call_response`` parts."""
    items = list(messages or ())
    if not items:
        return False
    last = items[-1]
    if last.get("role") == "tool":
        return True
    parts = last.get("parts") or []
    return bool(parts) and all(isinstance(p, dict) and p.get("type") == TOOL_CALL_RESPONSE for p in parts)


def user_turn_messages(messages: Iterable[Message] | None) -> list[Message]:
    """The trailing run of user messages that carry text (tool response messages do not count)."""
    out: list[Message] = []
    for message in reversed(list(messages or ())):
        if message.get("role") == "user" and has_text([message]):
            out.append(message)
        elif out:
            break
    return list(reversed(out))


# ---------------------------------------------------------------- legacy converters


def content_to_parts(content: Any) -> list[Part]:
    """OpenAI style ``content``: a string, a list of typed items, or Google Content."""
    if content is None or is_placeholder(content):
        return []
    if isinstance(content, str):
        return [text_part(content)]
    if isinstance(content, Mapping):
        if isinstance(content.get("parts"), list):
            return google_parts(content["parts"])
        return [text_part(_as_text(content))]
    if isinstance(content, list):
        parts: list[Part] = []
        for item in content:
            if isinstance(item, str):
                parts.append(text_part(item))
            elif isinstance(item, Mapping):
                kind = item.get("type")
                if kind == TEXT and "content" in item:
                    parts.append(dict(item))
                elif kind == TEXT or "text" in item:
                    parts.append(text_part(item.get("text")))
                elif isinstance(kind, str):
                    parts.append(dict(item))
        return parts
    return [text_part(_as_text(content))]


def openai_tool_calls_to_parts(tool_calls: Any) -> list[Part]:
    parts: list[Part] = []
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, Mapping):
            continue
        fn = call.get("function") if isinstance(call.get("function"), Mapping) else call
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        args = fn.get("arguments")
        parsed = parse_json_value(args) if isinstance(args, str) else args
        parts.append(
            {"type": TOOL_CALL, "id": call.get("id"), "name": name, "arguments": args if parsed is None else parsed}
        )
    return parts


def google_parts(parts: Iterable[Any]) -> list[Part]:
    """Google ``Content.parts`` to semconv parts. Inline data keeps only its mime type; URIs are never fetched."""
    out: list[Part] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        if part.get("text") is not None:
            kind = REASONING if part.get("thought") else TEXT
            out.append({"type": kind, "content": part["text"]})
        elif isinstance(part.get("function_call"), Mapping):
            fc = part["function_call"]
            out.append({"type": TOOL_CALL, "id": fc.get("id"), "name": fc.get("name"), "arguments": fc.get("args")})
        elif isinstance(part.get("function_response"), Mapping):
            fr = part["function_response"]
            out.append(
                {"type": TOOL_CALL_RESPONSE, "id": fr.get("id"), "name": fr.get("name"), "response": fr.get("response")}
            )
        elif isinstance(part.get("inline_data"), Mapping):
            out.append({"type": "blob", "mime_type": part["inline_data"].get("mime_type")})
        elif isinstance(part.get("file_data"), Mapping):
            fd = part["file_data"]
            out.append({"type": "uri", "uri": fd.get("file_uri"), "mime_type": fd.get("mime_type")})
        else:
            known = {k: v for k, v in part.items() if v is not None}
            if known:
                kind = next(iter(known))
                out.append({"type": kind, kind: known[kind]})
    return out


def google_content_to_message(content: Any, default_role: str = "user") -> Message | None:
    if not isinstance(content, Mapping):
        return None
    parts = google_parts(content.get("parts") or [])
    role = content.get("role") if isinstance(content.get("role"), str) else default_role
    if role == "model":
        role = "assistant"
    if parts and all(p["type"] == TOOL_CALL_RESPONSE for p in parts):
        role = "tool"
    return {"role": role, "parts": parts}
