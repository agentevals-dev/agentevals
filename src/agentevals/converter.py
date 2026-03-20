"""Convert trace spans into ADK Invocation objects.

Supports two trace formats:
1. ADK format (gcp.vertex.agent scope with ADK-specific attributes)
2. GenAI semantic conventions (standard gen_ai.* attributes from LangChain, LlamaIndex, etc.)

Automatically detects the format and routes to the appropriate converter.
Format detection checks span attributes and falls back to checking all spans if needed.
Explicit format can be specified via the format parameter to convert_trace().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.genai import types as genai_types

from .extraction import get_extractor, parse_json
from .loader.base import Span, Trace
from .trace_attrs import (
    ADK_INVOCATION_ID,
    ADK_LLM_REQUEST,
    ADK_LLM_RESPONSE,
    ADK_SCOPE_VALUE,
    ADK_TOOL_CALL_ARGS,
    ADK_TOOL_RESPONSE,
    OTEL_GENAI_AGENT_NAME,
    OTEL_GENAI_TOOL_CALL_ID,
    OTEL_GENAI_TOOL_NAME,
    OTEL_SCOPE,
)

logger = logging.getLogger(__name__)


@dataclass
class ConversionResult:
    trace_id: str
    invocations: list[Invocation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def convert_trace(trace: Trace, format: str | None = None) -> ConversionResult:
    """Convert a trace to Invocation objects.

    Args:
        trace: The trace to convert
        format: Optional explicit format ("adk" or "genai"). If None, auto-detects.

    Returns:
        ConversionResult with invocations and any warnings
    """
    if format is None:
        trace_format = _detect_trace_format(trace)
        logger.info(f"Auto-detected trace format: {trace_format} for trace {trace.trace_id}")
    else:
        trace_format = format
        logger.info(f"Using explicit trace format: {trace_format} for trace {trace.trace_id}")

    if trace_format == "genai":
        from .genai_converter import convert_genai_trace

        return convert_genai_trace(trace)
    else:
        return _convert_adk_trace(trace)


def _detect_trace_format(trace: Trace) -> str:
    """Detect trace format by delegating to the extractor registry."""
    return get_extractor(trace).format_name()


def _convert_adk_trace(trace: Trace) -> ConversionResult:
    result = ConversionResult(trace_id=trace.trace_id)

    invoke_spans = _find_adk_spans(trace, "invoke_agent")
    if not invoke_spans:
        result.warnings.append(f"Trace {trace.trace_id}: no invoke_agent spans found")
        return result

    for invoke_span in invoke_spans:
        try:
            invocation = _convert_invoke_span(invoke_span)
            result.invocations.append(invocation)
        except Exception as exc:
            msg = f"Trace {trace.trace_id}: failed to convert invoke_agent span {invoke_span.span_id}: {exc}"
            logger.warning(msg)
            result.warnings.append(msg)

    return result


def convert_traces(traces: list[Trace]) -> list[ConversionResult]:
    return [convert_trace(t) for t in traces]


def _find_adk_spans(trace: Trace, operation: str) -> list[Span]:
    """Find spans with ``otel.scope.name == "gcp.vertex.agent"`` matching an operation prefix."""
    matches = []
    for span in trace.all_spans:
        if span.get_tag(OTEL_SCOPE) != ADK_SCOPE_VALUE:
            continue
        # operationName is e.g. "invoke_agent helm_agent" or "call_llm"
        if span.operation_name.startswith(operation):
            matches.append(span)
    matches.sort(key=lambda s: s.start_time)
    return matches


def _convert_invoke_span(invoke_span: Span) -> Invocation:
    call_llm_spans = _find_children_by_op(invoke_span, "call_llm")
    if not call_llm_spans:
        raise ValueError(f"invoke_agent span {invoke_span.span_id} has no child call_llm spans")

    tool_spans = _find_children_by_op(invoke_span, "execute_tool")

    user_content = _extract_user_content(call_llm_spans[0])
    final_response = _extract_final_response(call_llm_spans[-1])
    tool_uses, tool_responses = _extract_tool_trajectory(call_llm_spans, tool_spans)

    intermediate_data = IntermediateData(
        tool_uses=tool_uses,
        tool_responses=tool_responses,
    )

    invocation_id = invoke_span.get_tag(ADK_INVOCATION_ID, invoke_span.span_id)

    return Invocation(
        invocation_id=invocation_id,
        user_content=user_content,
        final_response=final_response,
        intermediate_data=intermediate_data,
        creation_timestamp=invoke_span.start_time / 1_000_000.0,
    )


def _find_children_by_op(root: Span, op_prefix: str) -> list[Span]:
    results: list[Span] = []
    _walk(root, op_prefix, results)
    results.sort(key=lambda s: s.start_time)
    return results


def _walk(span: Span, op_prefix: str, acc: list[Span]) -> None:
    for child in span.children:
        if child.operation_name.startswith(op_prefix):
            acc.append(child)
        _walk(child, op_prefix, acc)


def _extract_user_content(first_call_llm: Span) -> genai_types.Content:
    """Extract user input from the first call_llm span's llm_request tag."""
    llm_request_raw = first_call_llm.get_tag(ADK_LLM_REQUEST, "{}")
    llm_request = parse_json(llm_request_raw)
    contents = llm_request.get("contents", [])

    for content_dict in reversed(contents):
        if content_dict.get("role") != "user":
            continue
        parts = content_dict.get("parts", [])
        # Skip function_response parts — only want actual user text messages
        text_parts = [p for p in parts if "text" in p]
        if text_parts:
            return genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=p["text"]) for p in text_parts],
            )

    for content_dict in contents:
        if content_dict.get("role") == "user":
            return _content_from_dict(content_dict)

    raise ValueError(f"call_llm span {first_call_llm.span_id}: no user content found in llm_request")


def _extract_final_response(last_call_llm: Span) -> genai_types.Content:
    """Extract final text response from the last call_llm span's llm_response tag."""
    llm_response_raw = last_call_llm.get_tag(ADK_LLM_RESPONSE, "{}")
    llm_response = parse_json(llm_response_raw)

    content_dict = llm_response.get("content", {})
    if not content_dict:
        raise ValueError(f"call_llm span {last_call_llm.span_id}: no content in llm_response")

    parts_dicts = content_dict.get("parts", [])
    # Final response should have text parts, not function_call parts
    text_parts = [p for p in parts_dicts if "text" in p]
    if text_parts:
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=p["text"]) for p in text_parts],
        )

    # If the last call_llm only has function_call parts, that's unexpected
    # for a final response — the agent may have been cut short.
    logger.warning(
        "call_llm span %s: last llm_response has no text parts, may not be the actual final response",
        last_call_llm.span_id,
    )
    return _content_from_dict(content_dict)


def _extract_tool_trajectory(
    call_llm_spans: list[Span],
    tool_spans: list[Span],
) -> tuple[list[genai_types.FunctionCall], list[genai_types.FunctionResponse]]:
    """Extract tool calls and responses.

    Prefers execute_tool spans (which have actual execution results) over
    function_call parts in call_llm responses (which only have the LLM's
    request to call the tool, not the result).
    """
    tool_uses: list[genai_types.FunctionCall] = []
    tool_responses: list[genai_types.FunctionResponse] = []

    if tool_spans:
        for tool_span in tool_spans:
            fc, fr = _extract_from_tool_span(tool_span)
            if fc:
                tool_uses.append(fc)
            if fr:
                tool_responses.append(fr)
    else:
        for call_llm in call_llm_spans:
            fcs = _extract_function_calls_from_llm_response(call_llm)
            tool_uses.extend(fcs)

    return tool_uses, tool_responses


def _extract_from_tool_span(
    tool_span: Span,
) -> tuple[genai_types.FunctionCall | None, genai_types.FunctionResponse | None]:
    tool_name = tool_span.get_tag(OTEL_GENAI_TOOL_NAME)
    tool_call_id = tool_span.get_tag(OTEL_GENAI_TOOL_CALL_ID)

    if not tool_name:
        # Fallback: parse tool name from operationName "execute_tool <name>"
        op = tool_span.operation_name
        if op.startswith("execute_tool "):
            tool_name = op[len("execute_tool ") :]
        else:
            logger.warning("execute_tool span %s: no tool name found", tool_span.span_id)
            return None, None

    args_raw = tool_span.get_tag(ADK_TOOL_CALL_ARGS, "{}")
    args = parse_json(args_raw)

    fc = genai_types.FunctionCall(
        name=tool_name,
        args=args if args else {},
        id=tool_call_id,
    )

    response_raw = tool_span.get_tag(ADK_TOOL_RESPONSE)
    fr = None
    if response_raw:
        response_data = parse_json(response_raw)
        # Response format varies: MCP uses {"content": [...], "isError": false},
        # other tools return flat dicts. We pass through as-is.
        fr = genai_types.FunctionResponse(
            name=tool_name,
            response=response_data if response_data else {},
            id=tool_call_id,
        )

    return fc, fr


def _extract_function_calls_from_llm_response(
    call_llm: Span,
) -> list[genai_types.FunctionCall]:
    llm_response_raw = call_llm.get_tag(ADK_LLM_RESPONSE, "{}")
    llm_response = parse_json(llm_response_raw)

    content_dict = llm_response.get("content", {})
    parts = content_dict.get("parts", [])

    calls = []
    for part in parts:
        fc_dict = part.get("function_call")
        if fc_dict:
            calls.append(
                genai_types.FunctionCall(
                    name=fc_dict.get("name", ""),
                    args=fc_dict.get("args", {}),
                    id=fc_dict.get("id"),
                )
            )
    return calls


def _content_from_dict(content_dict: dict[str, Any]) -> genai_types.Content:
    """Build a genai Content from a raw dict. Handles text, function_call, and function_response parts."""
    role = content_dict.get("role", "user")
    parts_dicts = content_dict.get("parts", [])

    parts: list[genai_types.Part] = []
    for p in parts_dicts:
        if "text" in p:
            parts.append(genai_types.Part(text=p["text"]))
        elif "function_call" in p:
            fc = p["function_call"]
            parts.append(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        name=fc.get("name", ""),
                        args=fc.get("args", {}),
                        id=fc.get("id"),
                    )
                )
            )
        elif "function_response" in p:
            fr = p["function_response"]
            parts.append(
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=fr.get("name", ""),
                        response=fr.get("response", {}),
                        id=fr.get("id"),
                    )
                )
            )

    return genai_types.Content(role=role, parts=parts)
