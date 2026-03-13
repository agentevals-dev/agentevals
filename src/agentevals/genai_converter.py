"""Convert trace spans using GenAI semantic conventions into ADK Invocation objects.

Supports traces from frameworks using OpenTelemetry GenAI semantic conventions:
- LangChain (via LANGSMITH_OTEL_ENABLED)
- LlamaIndex
- Haystack
- Any framework using standard gen_ai.* attributes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.genai import types as genai_types

from .converter import ConversionResult
from .extraction import GenAIExtractor, is_invocation_span, is_llm_span
from .loader.base import Span, Trace
from .trace_attrs import (
    OTEL_GENAI_INPUT_MESSAGES,
    OTEL_GENAI_OUTPUT_MESSAGES,
    OTEL_GENAI_REQUEST_MODEL,
    OTEL_GENAI_TOOL_CALL_ARGUMENTS,
    OTEL_GENAI_TOOL_CALL_ID,
    OTEL_GENAI_TOOL_CALL_RESULT,
    OTEL_GENAI_TOOL_NAME,
)
from .utils.genai_messages import (
    ASSISTANT_ROLES,
    USER_ROLES,
    extract_text_from_message,
    extract_tool_call_args_from_messages,
    extract_tool_calls_from_message,
    parse_json_attr,
)

logger = logging.getLogger(__name__)


@dataclass
class _ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class _ToolResponse:
    name: str
    response: dict
    id: str | None = None


@dataclass
class _ConversationTurn:
    invocation_id: str
    user_text: str
    assistant_text: str
    tool_calls: list[_ToolCall] = field(default_factory=list)
    tool_responses: list[_ToolResponse] = field(default_factory=list)
    start_time: float = 0.0


def convert_genai_trace(trace: Trace) -> ConversionResult:
    result = ConversionResult(trace_id=trace.trace_id)

    logger.debug(f"Converting GenAI trace {trace.trace_id} ({len(trace.all_spans)} spans)")

    llm_root_spans = [s for s in trace.root_spans if is_llm_span(s)]

    if llm_root_spans:
        has_messages = any(
            s.get_tag(OTEL_GENAI_INPUT_MESSAGES) or s.get_tag(OTEL_GENAI_OUTPUT_MESSAGES)
            for s in llm_root_spans
        )
        if not has_messages:
            msg = (
                f"Trace {trace.trace_id}: GenAI LLM spans found but missing message content. "
                "This usually means logs were not enriched into spans. "
                "Conversion may fail or produce incomplete results."
            )
            logger.warning(msg)
            result.warnings.append(msg)

    if len(llm_root_spans) > 1:
        # Multi-turn extraction applies only to raw LLM spans whose message content accumulates
        # across calls (e.g. a framework that re-sends full history on each LLM call).
        # Invocation spans (e.g. invoke_agent) each contain self-contained message histories
        # and should each map to a separate invocation via _find_genai_invocation_spans().
        if not any(is_invocation_span(s) for s in llm_root_spans):
            has_enriched = any(
                s.get_tag(OTEL_GENAI_INPUT_MESSAGES) and s.get_tag(OTEL_GENAI_OUTPUT_MESSAGES)
                for s in llm_root_spans
            )

            if has_enriched:
                logger.debug(f"Multi-turn conversation: {len(llm_root_spans)} LLM spans")
                try:
                    turns = _extract_multiturn_turns(llm_root_spans)
                    for turn in turns:
                        result.invocations.append(_turn_to_invocation(turn))
                except Exception as exc:
                    msg = f"Trace {trace.trace_id}: failed to convert multi-turn conversation: {exc}"
                    logger.warning(msg)
                    result.warnings.append(msg)
                return result

    invocation_spans = _find_genai_invocation_spans(trace)
    logger.debug(f"Found {len(invocation_spans)} invocation spans")

    if not invocation_spans:
        result.warnings.append(
            f"Trace {trace.trace_id}: no GenAI invocation spans found"
        )
        return result

    for inv_span in invocation_spans:
        try:
            turn = _extract_single_turn(inv_span)
            result.invocations.append(_turn_to_invocation(turn))
        except Exception as exc:
            msg = f"Failed to convert span {inv_span.span_id}: {exc}"
            logger.warning(msg)
            result.warnings.append(msg)

    return result


def _find_genai_invocation_spans(trace: Trace) -> list[Span]:
    candidates = []

    for span in trace.root_spans:
        if is_invocation_span(span):
            candidates.append(span)

    if not candidates:
        for span in trace.root_spans:
            if _has_llm_children(span):
                candidates.append(span)

    if not candidates and trace.root_spans:
        llm_spans = [s for s in trace.root_spans if is_llm_span(s)]

        if len(llm_spans) > 1:
            has_enriched_messages = any(
                s.get_tag(OTEL_GENAI_INPUT_MESSAGES) or s.get_tag(OTEL_GENAI_OUTPUT_MESSAGES)
                for s in llm_spans
            )

            if has_enriched_messages:
                logger.debug(f"Found {len(llm_spans)} LLM spans with enriched messages, treating as single multi-turn conversation")
                return [llm_spans[0]]

        logger.debug("No clear invocation spans found, treating each root span as invocation")
        candidates = llm_spans if llm_spans else trace.root_spans

    if not candidates and trace.root_spans:
        logger.debug("Falling back to all root spans")
        candidates = list(trace.root_spans)

    candidates.sort(key=lambda s: s.start_time)
    return candidates


def _extract_single_turn(inv_span: Span) -> _ConversationTurn:
    llm_spans = _find_llm_spans(inv_span)

    logger.debug(f"Converting invocation span: {inv_span.operation_name}")
    logger.debug(f"Found {len(llm_spans)} LLM spans")

    if not llm_spans:
        if is_llm_span(inv_span):
            llm_spans = [inv_span]
        else:
            raise ValueError(
                f"Invocation span {inv_span.span_id} has no LLM call spans"
            )

    tool_spans = _find_tool_spans(inv_span)
    logger.debug(f"Found {len(tool_spans)} tool spans")

    user_text = _extract_user_text(llm_spans[0])
    assistant_text = _extract_assistant_text(llm_spans[-1])
    tool_calls, tool_responses = _extract_tool_calls(tool_spans, llm_spans)

    return _ConversationTurn(
        invocation_id=f"genai-{inv_span.span_id}",
        user_text=user_text,
        assistant_text=assistant_text,
        tool_calls=tool_calls,
        tool_responses=tool_responses,
        start_time=float(inv_span.start_time),
    )


def _extract_multiturn_turns(llm_spans: list[Span]) -> list[_ConversationTurn]:
    messages_raw = llm_spans[0].get_tag(OTEL_GENAI_INPUT_MESSAGES, "[]")
    all_input_messages = parse_json_attr(messages_raw, "gen_ai.input.messages")

    output_messages_raw = llm_spans[0].get_tag(OTEL_GENAI_OUTPUT_MESSAGES, "[]")
    all_output_messages = parse_json_attr(output_messages_raw, "gen_ai.output.messages")

    if not isinstance(all_input_messages, list) or not isinstance(all_output_messages, list):
        logger.warning("Messages are not lists, falling back to single invocation")
        user_text = _extract_user_text(llm_spans[0])
        assistant_text = _extract_assistant_text(llm_spans[-1])
        return [_ConversationTurn(
            invocation_id=f"genai-{llm_spans[0].span_id}",
            user_text=user_text,
            assistant_text=assistant_text,
            start_time=float(llm_spans[0].start_time),
        )]

    user_messages = [msg for msg in all_input_messages if msg.get("role") in USER_ROLES]
    assistant_messages = [msg for msg in all_output_messages if msg.get("role") in ASSISTANT_ROLES]

    logger.debug(f"Multi-turn: {len(user_messages)} user, {len(assistant_messages)} assistant messages")
    for i, msg in enumerate(assistant_messages):
        has_content = bool(msg.get("content"))
        has_tools = bool(msg.get("tool_calls"))
        logger.debug(f"  Assistant msg {i}: has_content={has_content}, has_tools={has_tools}")

    turns = []
    assistant_idx = 0

    for user_idx, user_msg in enumerate(user_messages):
        user_text = extract_text_from_message(user_msg)
        if not user_text:
            continue

        tool_calls: list[_ToolCall] = []
        assistant_text = ""

        while assistant_idx < len(assistant_messages):
            assistant_msg = assistant_messages[assistant_idx]

            for tc in extract_tool_calls_from_message(assistant_msg):
                tool_calls.append(_ToolCall(name=tc["name"], args=tc["arguments"], id=tc["id"]))

            content = extract_text_from_message(assistant_msg)
            if content:
                assistant_text = content
                assistant_idx += 1
                break

            assistant_idx += 1

        turns.append(_ConversationTurn(
            invocation_id=f"genai-turn-{user_idx + 1}-{llm_spans[0].span_id[:8]}",
            user_text=user_text if isinstance(user_text, str) else "",
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            start_time=float(llm_spans[0].start_time),
        ))

    return turns


def _turn_to_invocation(turn: _ConversationTurn) -> Invocation:
    user_content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=turn.user_text)],
    )
    final_response = genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=turn.assistant_text)],
    )
    tool_uses = [
        genai_types.FunctionCall(name=tc.name, args=tc.args, id=tc.id)
        for tc in turn.tool_calls
    ]
    tool_responses = [
        genai_types.FunctionResponse(name=tr.name, response=tr.response, id=tr.id)
        for tr in turn.tool_responses
    ]
    return Invocation(
        invocation_id=turn.invocation_id,
        user_content=user_content,
        final_response=final_response,
        intermediate_data=IntermediateData(tool_uses=tool_uses, tool_responses=tool_responses),
        creation_timestamp=turn.start_time / 1_000_000.0,
    )


def _extract_user_text(llm_span: Span) -> str:
    messages_raw = llm_span.get_tag(OTEL_GENAI_INPUT_MESSAGES, "[]")
    messages = parse_json_attr(messages_raw, "gen_ai.input.messages")

    if not isinstance(messages, list):
        messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in USER_ROLES:
            text = extract_text_from_message(msg)
            if text:
                logger.debug(f"Found user message: {text[:100]}")
                return text

    logger.warning(f"No user message found in {len(messages)} messages")
    raise ValueError(
        f"LLM span {llm_span.span_id}: no user message found in gen_ai.input.messages"
    )


def _extract_assistant_text(llm_span: Span) -> str:
    messages_raw = llm_span.get_tag(OTEL_GENAI_OUTPUT_MESSAGES, "[]")
    messages = parse_json_attr(messages_raw, "gen_ai.output.messages")

    if not isinstance(messages, list):
        messages = []

    logger.debug(f"Extracting final response from {len(messages)} output messages")
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            logger.debug(f"  Message {i}: role={msg.get('role')}, content_len={len(msg.get('content', ''))}, has_tool_calls={bool(msg.get('tool_calls'))}")

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in ASSISTANT_ROLES:
            text = extract_text_from_message(msg)
            if text:
                logger.debug(f"Found assistant message with text: {text[:100]}")
                return text

    logger.warning(
        f"LLM span {llm_span.span_id}: no assistant message with content in gen_ai.output.messages ({len(messages)} messages)"
    )
    return ""


def _extract_tool_calls(
    tool_spans: list[Span],
    llm_spans: list[Span] | None = None,
) -> tuple[list[_ToolCall], list[_ToolResponse]]:
    tool_calls_by_id: dict[str, _ToolCall] = {}
    tool_calls_no_id: list[_ToolCall] = []
    tool_responses: list[_ToolResponse] = []

    for tool_span in tool_spans:
        tool_name = tool_span.get_tag(OTEL_GENAI_TOOL_NAME)
        if not tool_name:
            logger.warning(f"Tool span missing gen_ai.tool.name: {tool_span.operation_name}")
            continue

        tool_call_id = tool_span.get_tag(OTEL_GENAI_TOOL_CALL_ID)

        args_raw = tool_span.get_tag(OTEL_GENAI_TOOL_CALL_ARGUMENTS, "{}")
        args = parse_json_attr(args_raw, "gen_ai.tool.call.arguments")
        if not isinstance(args, dict):
            args = {}

        if not args:
            input_msgs_raw = tool_span.get_tag(OTEL_GENAI_INPUT_MESSAGES)
            if input_msgs_raw:
                args, _ = extract_tool_call_args_from_messages(input_msgs_raw, tool_name)

        tc = _ToolCall(name=tool_name, args=args, id=tool_call_id)
        if tool_call_id:
            tool_calls_by_id[tool_call_id] = tc
        else:
            tool_calls_no_id.append(tc)

        result_raw = tool_span.get_tag(OTEL_GENAI_TOOL_CALL_RESULT)
        if result_raw:
            result_data = parse_json_attr(result_raw, "gen_ai.tool.call.result")
            if result_data is None:
                result_data = {}

            logger.debug(f"Tool {tool_name} result: {str(result_data)[:100]}")

            tool_responses.append(_ToolResponse(
                name=tool_name,
                response=result_data if isinstance(result_data, dict) else {"result": str(result_data)},
                id=tool_call_id,
            ))

    # Extract tool calls from LLM output messages, deduplicating by tool call ID
    if llm_spans:
        for llm_span in llm_spans:
            messages_raw = llm_span.get_tag(OTEL_GENAI_OUTPUT_MESSAGES, "[]")
            messages = parse_json_attr(messages_raw, "gen_ai.output.messages")

            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") not in ASSISTANT_ROLES:
                    continue
                for tc in extract_tool_calls_from_message(msg):
                    tc_id = tc["id"]
                    new_tc = _ToolCall(
                        name=tc["name"],
                        args=tc["arguments"],
                        id=tc_id,
                    )
                    if tc_id and tc_id in tool_calls_by_id:
                        # Prefer LLM message version if it has richer args
                        existing = tool_calls_by_id[tc_id]
                        if tc["arguments"] and not existing.args:
                            tool_calls_by_id[tc_id] = new_tc
                    elif tc_id:
                        tool_calls_by_id[tc_id] = new_tc
                    else:
                        tool_calls_no_id.append(new_tc)

    tool_calls = list(tool_calls_by_id.values()) + tool_calls_no_id
    logger.debug(f"Extracted {len(tool_calls)} tool calls, {len(tool_responses)} responses")
    return tool_calls, tool_responses


_genai_extractor = GenAIExtractor()


def _find_llm_spans(root: Span) -> list[Span]:
    return _genai_extractor.find_llm_spans_in(root)


def _find_tool_spans(root: Span) -> list[Span]:
    return _genai_extractor.find_tool_spans_in(root)


def _has_llm_children(span: Span) -> bool:
    return _genai_extractor._has_llm_children(span)


