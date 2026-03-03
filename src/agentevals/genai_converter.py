"""Convert trace spans using GenAI semantic conventions into ADK Invocation objects.

Supports traces from frameworks using OpenTelemetry GenAI semantic conventions:
- LangChain (via LANGSMITH_OTEL_ENABLED)
- LlamaIndex
- Haystack
- Any framework using standard gen_ai.* attributes
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.genai import types as genai_types

from .loader.base import Span, Trace

logger = logging.getLogger(__name__)

_TAG_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
_TAG_GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
_TAG_GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
_TAG_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_TAG_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_TAG_GEN_AI_TOOL_NAME = "gen_ai.tool.name"
_TAG_GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
_TAG_GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
_TAG_GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"


@dataclass
class ConversionResult:
    trace_id: str
    invocations: list[Invocation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def convert_genai_trace(trace: Trace) -> ConversionResult:
    result = ConversionResult(trace_id=trace.trace_id)

    logger.debug(f"Converting GenAI trace {trace.trace_id} ({len(trace.all_spans)} spans)")

    llm_root_spans = [s for s in trace.root_spans if _is_llm_span(s)]

    if llm_root_spans:
        has_messages = any(
            s.get_tag(_TAG_GEN_AI_INPUT_MESSAGES) or s.get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES)
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
        has_enriched = any(
            s.get_tag(_TAG_GEN_AI_INPUT_MESSAGES) and s.get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES)
            for s in llm_root_spans
        )

        if has_enriched:
            logger.debug(f"Multi-turn conversation: {len(llm_root_spans)} LLM spans")
            try:
                invocations = _convert_multiturn_conversation(llm_root_spans, trace)
                result.invocations.extend(invocations)
                return result
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
            invocation = _convert_genai_invocation(inv_span)
            result.invocations.append(invocation)
        except Exception as exc:
            msg = f"Failed to convert span {inv_span.span_id}: {exc}"
            logger.warning(msg)
            result.warnings.append(msg)

    return result


def _find_genai_invocation_spans(trace: Trace) -> list[Span]:
    candidates = []

    for span in trace.root_spans:
        if _is_genai_invocation_span(span):
            candidates.append(span)

    if not candidates:
        for span in trace.root_spans:
            if _has_llm_children(span):
                candidates.append(span)

    if not candidates and trace.root_spans:
        llm_spans = [s for s in trace.root_spans if _is_llm_span(s)]

        if len(llm_spans) > 1:
            has_enriched_messages = any(
                s.get_tag(_TAG_GEN_AI_INPUT_MESSAGES) or s.get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES)
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


def _is_genai_invocation_span(span: Span) -> bool:
    op_lower = span.operation_name.lower()
    invocation_keywords = ["agent", "chain", "executor", "workflow"]
    return any(keyword in op_lower for keyword in invocation_keywords)


def _has_llm_children(span: Span) -> bool:
    for child in span.children:
        if _is_llm_span(child):
            return True
        if _has_llm_children(child):
            return True
    return False


def _is_llm_span(span: Span) -> bool:
    return (
        span.get_tag(_TAG_GEN_AI_REQUEST_MODEL) is not None
        or span.get_tag(_TAG_GEN_AI_INPUT_MESSAGES) is not None
    )


def _convert_multiturn_conversation(llm_spans: list[Span], trace: Trace) -> list[Invocation]:

    messages_raw = llm_spans[0].get_tag(_TAG_GEN_AI_INPUT_MESSAGES, "[]")
    all_input_messages = _parse_json(messages_raw, "gen_ai.input.messages")

    output_messages_raw = llm_spans[0].get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES, "[]")
    all_output_messages = _parse_json(output_messages_raw, "gen_ai.output.messages")

    if not isinstance(all_input_messages, list) or not isinstance(all_output_messages, list):
        logger.warning("Messages are not lists, falling back to single invocation")
        user_content = _extract_genai_user_content(llm_spans[0])
        final_response = _extract_genai_final_response(llm_spans[-1])
        return [Invocation(
            invocation_id=f"genai-{llm_spans[0].span_id}",
            user_content=user_content,
            final_response=final_response,
            intermediate_data=IntermediateData(),
        )]

    user_messages = [msg for msg in all_input_messages if msg.get("role") in ("user", "human")]
    assistant_messages = [msg for msg in all_output_messages if msg.get("role") in ("assistant", "model", "ai")]

    logger.debug(f"Multi-turn: {len(user_messages)} user, {len(assistant_messages)} assistant messages")
    for i, msg in enumerate(assistant_messages):
        has_content = bool(msg.get("content"))
        has_tools = bool(msg.get("tool_calls"))
        logger.debug(f"  Assistant msg {i}: has_content={has_content}, has_tools={has_tools}")

    invocations = []

    assistant_idx = 0

    for user_idx, user_msg in enumerate(user_messages):
        user_text = user_msg.get("content", "")
        if not user_text:
            continue

        user_content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_text)]
        )

        tool_uses = []
        final_response_text = ""

        while assistant_idx < len(assistant_messages):
            assistant_msg = assistant_messages[assistant_idx]

            if assistant_msg.get("tool_calls"):
                for tc in assistant_msg.get("tool_calls", []):
                    if tc.get("type") == "function" and "function" in tc:
                        func = tc["function"]
                        args = _parse_json(func.get("arguments", "{}"), "tool_call.arguments")
                        if not isinstance(args, dict):
                            args = {}
                        tool_uses.append(genai_types.FunctionCall(
                            name=func.get("name"),
                            args=args,
                            id=tc.get("id"),
                        ))

            content = assistant_msg.get("content", "")
            if content:
                final_response_text = content
                assistant_idx += 1
                break

            assistant_idx += 1

        final_response = genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=final_response_text)]
        )

        invocation_id = f"genai-turn-{user_idx+1}-{llm_spans[0].span_id[:8]}"

        invocations.append(Invocation(
            invocation_id=invocation_id,
            user_content=user_content,
            final_response=final_response,
            intermediate_data=IntermediateData(
                tool_uses=tool_uses,
                tool_responses=[],
            ),
        ))

    return invocations


def _convert_genai_invocation(inv_span: Span) -> Invocation:
    llm_spans = _find_genai_llm_spans(inv_span)

    logger.debug(f"Converting invocation span: {inv_span.operation_name}")
    logger.debug(f"Found {len(llm_spans)} LLM spans")

    if not llm_spans:
        if _is_llm_span(inv_span):
            llm_spans = [inv_span]
        else:
            raise ValueError(
                f"Invocation span {inv_span.span_id} has no LLM call spans"
            )

    tool_spans = _find_genai_tool_spans(inv_span)
    logger.debug(f"Found {len(tool_spans)} tool spans")

    user_content = _extract_genai_user_content(llm_spans[0])
    final_response = _extract_genai_final_response(llm_spans[-1])
    tool_uses, tool_responses = _extract_genai_tools(tool_spans, llm_spans)

    intermediate_data = IntermediateData(
        tool_uses=tool_uses,
        tool_responses=tool_responses,
    )

    invocation_id = f"genai-{inv_span.span_id}"

    return Invocation(
        invocation_id=invocation_id,
        user_content=user_content,
        final_response=final_response,
        intermediate_data=intermediate_data,
        creation_timestamp=inv_span.start_time / 1_000_000.0,
    )


def _find_genai_llm_spans(root: Span) -> list[Span]:
    results = []
    _walk_for_llm_spans(root, results)
    results.sort(key=lambda s: s.start_time)
    return results


def _walk_for_llm_spans(span: Span, acc: list[Span]) -> None:
    if _is_llm_span(span):
        acc.append(span)
    for child in span.children:
        _walk_for_llm_spans(child, acc)


def _find_genai_tool_spans(root: Span) -> list[Span]:
    results = []
    _walk_for_tool_spans(root, results)
    results.sort(key=lambda s: s.start_time)
    return results


def _walk_for_tool_spans(span: Span, acc: list[Span]) -> None:
    if span.get_tag(_TAG_GEN_AI_TOOL_NAME) is not None:
        acc.append(span)
    for child in span.children:
        _walk_for_tool_spans(child, acc)


def _extract_genai_user_content(llm_span: Span) -> genai_types.Content:
    messages_raw = llm_span.get_tag(_TAG_GEN_AI_INPUT_MESSAGES, "[]")
    messages = _parse_json(messages_raw, "gen_ai.input.messages")


    if not isinstance(messages, list):
        messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in ("user", "human"):
            content_text = msg.get("content", "")
            if isinstance(content_text, str):
                logger.debug(f"Found user message: {content_text[:100]}")
                return genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=content_text)],
                )
            elif isinstance(content_text, list):
                parts = []
                for item in content_text:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(genai_types.Part(text=item["text"]))
                if parts:
                    return genai_types.Content(role="user", parts=parts)

    logger.warning(f"No user message found in {len(messages)} messages")
    raise ValueError(
        f"LLM span {llm_span.span_id}: no user message found in gen_ai.input.messages"
    )


def _extract_genai_final_response(llm_span: Span) -> genai_types.Content:
    messages_raw = llm_span.get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES, "[]")
    messages = _parse_json(messages_raw, "gen_ai.output.messages")


    if not isinstance(messages, list):
        messages = []

    logger.debug(f"Extracting final response from {len(messages)} output messages")
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            logger.debug(f"  Message {i}: role={msg.get('role')}, content_len={len(msg.get('content', ''))}, has_tool_calls={bool(msg.get('tool_calls'))}")

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in ("assistant", "model", "ai"):
            content_text = msg.get("content", "")
            if isinstance(content_text, str) and content_text:
                logger.debug(f"Found assistant message with text: {content_text[:100]}")
                return genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=content_text)],
                )
            elif isinstance(content_text, list):
                parts = []
                for item in content_text:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(genai_types.Part(text=item["text"]))
                if parts:
                    return genai_types.Content(role="model", parts=parts)

    logger.warning(
        f"LLM span {llm_span.span_id}: no assistant message with content in gen_ai.output.messages ({len(messages)} messages)"
    )
    return genai_types.Content(role="model", parts=[genai_types.Part(text="")])


def _extract_genai_tools(
    tool_spans: list[Span],
    llm_spans: list[Span] = None,
) -> tuple[list[genai_types.FunctionCall], list[genai_types.FunctionResponse]]:
    tool_uses = []
    tool_responses = []

    for tool_span in tool_spans:
        tool_name = tool_span.get_tag(_TAG_GEN_AI_TOOL_NAME)
        if not tool_name:
            logger.warning(f"Tool span missing gen_ai.tool.name: {tool_span.operation_name}")
            continue

        tool_call_id = tool_span.get_tag(_TAG_GEN_AI_TOOL_CALL_ID)

        args_raw = tool_span.get_tag(_TAG_GEN_AI_TOOL_CALL_ARGUMENTS, "{}")
        args = _parse_json(args_raw, "gen_ai.tool.call.arguments")
        if not isinstance(args, dict):
            args = {}

        fc = genai_types.FunctionCall(
            name=tool_name,
            args=args,
            id=tool_call_id,
        )
        tool_uses.append(fc)

        result_raw = tool_span.get_tag(_TAG_GEN_AI_TOOL_CALL_RESULT)
        if result_raw:
            result_data = _parse_json(result_raw, "gen_ai.tool.call.result")
            if result_data is None:
                result_data = {}

            logger.debug(f"Tool {tool_name} result: {str(result_data)[:100]}")

            fr = genai_types.FunctionResponse(
                name=tool_name,
                response=result_data if isinstance(result_data, dict) else {"result": str(result_data)},
                id=tool_call_id,
            )
            tool_responses.append(fr)

    if llm_spans:
        for llm_span in llm_spans:
            messages_raw = llm_span.get_tag(_TAG_GEN_AI_OUTPUT_MESSAGES, "[]")
            messages = _parse_json(messages_raw, "gen_ai.output.messages")

            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue

                if msg.get("role") in ("assistant", "model", "ai") and "tool_calls" in msg:
                    tool_calls = msg.get("tool_calls", [])
                    if not isinstance(tool_calls, list):
                        continue

                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue

                        if tool_call.get("type") == "function" and "function" in tool_call:
                            func_data = tool_call["function"]
                            tool_name = func_data.get("name")
                            if not tool_name:
                                continue

                            args_raw = func_data.get("arguments", "{}")
                            args = _parse_json(args_raw, "tool_call.function.arguments")
                            if not isinstance(args, dict):
                                args = {}

                            fc = genai_types.FunctionCall(
                                name=tool_name,
                                args=args,
                                id=tool_call.get("id"),
                            )
                            tool_uses.append(fc)

    logger.debug(f"Extracted {len(tool_uses)} tool calls, {len(tool_responses)} responses")
    return tool_uses, tool_responses


def _parse_json(raw: str | dict | list | Any, tag_name: str) -> dict | list | Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON in {tag_name}: {raw[:200]}")
            return {}
    return {}
