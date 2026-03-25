"""Convert Claude Code OTEL logs and hook events into ADK Invocation objects.

Claude Code emits two complementary data streams:
1. OTEL logs (claude_code.* events): user prompts, tool metadata, API usage
2. HTTP hooks (PostToolUse, Stop): full tool I/O and agent response text

This converter merges both streams, grouped by prompt.id, to produce standard
Invocation objects compatible with all existing eval metrics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.genai import types as genai_types

from .converter import ConversionResult
from .trace_attrs import (
    CC_EVENT_API_REQUEST,
    CC_EVENT_TOOL_RESULT,
    CC_EVENT_USER_PROMPT,
    CC_PROMPT_ID,
    CC_SESSION_ID,
)

logger = logging.getLogger(__name__)


@dataclass
class _ToolCall:
    name: str
    args: dict
    tool_use_id: str = ""
    success: bool = True
    duration_ms: int = 0


@dataclass
class _ToolResponse:
    name: str
    response: dict
    tool_use_id: str = ""


@dataclass
class _ClaudeCodeTurn:
    prompt_id: str
    user_text: str = ""
    assistant_text: str = ""
    tool_calls: list[_ToolCall] = field(default_factory=list)
    tool_responses: list[_ToolResponse] = field(default_factory=list)
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    start_time: float = 0.0
    sequence: int = 0


def convert_claude_code_session(
    logs: list[dict],
    hook_events: list[dict] | None = None,
) -> ConversionResult:
    """Convert Claude Code OTEL logs and hook events into Invocation objects.

    Args:
        logs: OTEL log events (claude_code.* events from the session)
        hook_events: Claude Code hook payloads (PostToolUse, Stop)

    Returns:
        ConversionResult with invocations and any warnings
    """
    warnings: list[str] = []
    turns = _group_logs_into_turns(logs, warnings)
    _merge_hook_events(turns, hook_events or [], warnings)

    trace_id = ""
    if logs:
        trace_id = logs[0].get("attributes", {}).get(CC_SESSION_ID, "")

    invocations = []
    for prompt_id in sorted(turns, key=lambda pid: turns[pid].sequence):
        turn = turns[prompt_id]
        invocations.append(_turn_to_invocation(turn))

    if not invocations:
        warnings.append("No invocations extracted from Claude Code session")

    return ConversionResult(
        trace_id=trace_id,
        invocations=invocations,
        warnings=warnings,
    )


def _group_logs_into_turns(
    logs: list[dict], warnings: list[str]
) -> dict[str, _ClaudeCodeTurn]:
    """Group OTEL log events by prompt.id into conversation turns."""
    turns: dict[str, _ClaudeCodeTurn] = {}

    sorted_logs = sorted(
        logs,
        key=lambda l: l.get("attributes", {}).get("event.sequence", 0),
    )

    for log in sorted_logs:
        event_name = log.get("event_name", "")
        attrs = log.get("attributes", {})
        prompt_id = attrs.get(CC_PROMPT_ID, "")

        if not prompt_id:
            continue

        if prompt_id not in turns:
            turns[prompt_id] = _ClaudeCodeTurn(
                prompt_id=prompt_id,
                sequence=attrs.get("event.sequence", 0),
            )

        turn = turns[prompt_id]

        if event_name == CC_EVENT_USER_PROMPT:
            prompt_text = attrs.get("prompt", "")
            prompt_length = attrs.get("prompt_length", 0)
            if prompt_text:
                turn.user_text = prompt_text
            elif prompt_length:
                turn.user_text = f"[User prompt: {prompt_length} chars]"
            turn.start_time = _get_timestamp(log)

        elif event_name == CC_EVENT_TOOL_RESULT:
            tool_name = attrs.get("tool_name", "unknown")
            success = attrs.get("success") in (True, "true", "True")
            duration_ms = int(attrs.get("duration_ms", 0))
            tool_params = _parse_tool_parameters(attrs.get("tool_parameters", "{}"))
            turn.tool_calls.append(
                _ToolCall(
                    name=tool_name,
                    args=tool_params,
                    success=success,
                    duration_ms=duration_ms,
                )
            )

        elif event_name == CC_EVENT_API_REQUEST:
            turn.model = attrs.get("model", "unknown")
            turn.input_tokens += int(attrs.get("input_tokens", 0))
            turn.output_tokens += int(attrs.get("output_tokens", 0))
            turn.cost_usd += float(attrs.get("cost_usd", 0.0))

    return turns


def _merge_hook_events(
    turns: dict[str, _ClaudeCodeTurn],
    hook_events: list[dict],
    warnings: list[str],
) -> None:
    """Merge Claude Code hook payloads into existing turns.

    Hook events provide full tool I/O (PostToolUse) and agent response (Stop)
    that are not available in OTEL logs.
    """
    tool_use_id_to_prompt: dict[str, str] = {}
    for turn in turns.values():
        for tc in turn.tool_calls:
            if tc.tool_use_id:
                tool_use_id_to_prompt[tc.tool_use_id] = turn.prompt_id

    last_prompt_id = ""
    if turns:
        last_prompt_id = max(turns, key=lambda pid: turns[pid].sequence)

    for event in hook_events:
        hook_name = event.get("hook_event_name", "")

        if hook_name == "PostToolUse":
            tool_use_id = event.get("tool_use_id", "")
            tool_name = event.get("tool_name", "unknown")
            tool_input = event.get("tool_input", {})
            tool_response = event.get("tool_response", {})

            prompt_id = tool_use_id_to_prompt.get(tool_use_id)

            if not prompt_id:
                prompt_id = _find_turn_with_tool(turns, tool_name, tool_use_id)

            if not prompt_id and last_prompt_id:
                prompt_id = last_prompt_id

            if prompt_id and prompt_id in turns:
                turn = turns[prompt_id]
                matched = False
                for tc in turn.tool_calls:
                    if tc.name == tool_name and not tc.tool_use_id:
                        tc.tool_use_id = tool_use_id
                        if tool_input and not tc.args:
                            tc.args = tool_input if isinstance(tool_input, dict) else {}
                        elif tool_input:
                            tc.args = tool_input if isinstance(tool_input, dict) else tc.args
                        matched = True
                        break

                if not matched:
                    turn.tool_calls.append(
                        _ToolCall(
                            name=tool_name,
                            args=tool_input if isinstance(tool_input, dict) else {},
                            tool_use_id=tool_use_id,
                        )
                    )

                if tool_response:
                    resp = tool_response if isinstance(tool_response, dict) else {"result": str(tool_response)}
                    turn.tool_responses.append(
                        _ToolResponse(
                            name=tool_name,
                            response=resp,
                            tool_use_id=tool_use_id,
                        )
                    )

        elif hook_name == "Stop":
            last_message = event.get("last_assistant_message", "")
            if last_message and last_prompt_id and last_prompt_id in turns:
                turns[last_prompt_id].assistant_text = last_message
            elif last_message:
                warnings.append(
                    "Stop hook received but no turns exist to attach response to"
                )


def _find_turn_with_tool(
    turns: dict[str, _ClaudeCodeTurn], tool_name: str, tool_use_id: str
) -> str | None:
    """Find the turn containing a tool call matching the given name (reverse order)."""
    for prompt_id in sorted(turns, key=lambda pid: turns[pid].sequence, reverse=True):
        for tc in turns[prompt_id].tool_calls:
            if tc.name == tool_name and not tc.tool_use_id:
                return prompt_id
    return None


def _turn_to_invocation(turn: _ClaudeCodeTurn) -> Invocation:
    """Convert a ClaudeCodeTurn into a standard ADK Invocation."""
    user_content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=turn.user_text or "")],
    )
    final_response = genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=turn.assistant_text or "")],
    )

    tool_uses = [
        genai_types.FunctionCall(name=tc.name, args=tc.args, id=tc.tool_use_id or None)
        for tc in turn.tool_calls
    ]
    tool_responses = [
        genai_types.FunctionResponse(name=tr.name, response=tr.response, id=tr.tool_use_id or None)
        for tr in turn.tool_responses
    ]

    return Invocation(
        invocation_id=f"cc-{turn.prompt_id}",
        user_content=user_content,
        final_response=final_response,
        intermediate_data=IntermediateData(tool_uses=tool_uses, tool_responses=tool_responses),
        creation_timestamp=turn.start_time,
    )


def _get_timestamp(log: dict) -> float:
    """Extract timestamp from a log event as seconds since epoch."""
    ts = log.get("timestamp")
    if ts is None:
        return 0.0
    try:
        ns = int(ts)
    except (TypeError, ValueError):
        return 0.0
    if ns > 1e15:
        return ns / 1e9
    return float(ns)


def _parse_tool_parameters(raw: str | dict) -> dict:
    """Parse tool_parameters JSON string into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
