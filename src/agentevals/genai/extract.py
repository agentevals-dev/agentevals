"""Turn extraction from GenAI telemetry.

Rules, in brief (``otel_native_core_202610.md`` sections 4.5 and 16):

* Spans are classified from the overlay's operation name, never from span names.
* A turn is anchored on a workflow or agent span with no workflow, agent or tool ancestor. An
  agent or workflow under a tool or another agent is a delegate inside that turn.
* A root whose inference spans no agentic anchor covers anchors its own turn, without agent
  attribution. An anchor with no inference, no tool span and no messages is not a turn.
* Inference spans nested without an agent or tool in between collapse into one logical call
  when the chain has exactly one leaf (wrapper spans over provider spans); with several leaves
  each leaf is its own call. Usage is counted once per logical call.
* The trajectory comes from tool spans when the turn has any, otherwise from ``tool_call``
  parts of the model outputs, with results joined from later ``tool_call_response`` parts.
  Tool spans without arguments or result are completed from those parts (by call id, else by
  tool name in order), never overriding what the span carries.
* Outputs of calls under a tool span (AgentTool delegates) are not the turn's own speech: they
  are excluded from intermediate and final outputs, but their usage still counts.
* Within a conversation, a root anchored turn whose first call's input ends with a tool response,
  following a turn whose last call requested tools, continues that turn (provider only telemetry
  puts each model call of a tool round in its own trace). This reads message content; with
  content capture off the turns stay split and the later one carries a warning.

Every walk is iterative, so adversarial nesting depth cannot exhaust the stack.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..otel.model import (
    STATUS_ERROR,
    STATUS_OK,
    LogRecord,
    Span,
    Trace,
    attr_float,
    attr_int,
    attr_str,
    parse_json_value,
)
from . import semconv as sc
from .messages import (
    Message,
    ends_with_tool_response,
    has_content,
    has_text,
    text_of,
    tool_calls_of,
    tool_responses_of,
    user_turn_messages,
)
from .model import Conversation, ExternalEvaluation, LlmCall, ToolCall, Turn, Usage
from .overlay import SpanView, overlay

INFERENCE = "inference"
AGENT = "agent"
WORKFLOW = "workflow"
TOOL = "tool"
OTHER = "other_genai"
NON_GENAI = "non_genai"
IGNORED = "ignored"

AGENTIC = frozenset({AGENT, WORKFLOW})
BREAKS_INFERENCE_CHAIN = frozenset({AGENT, WORKFLOW, TOOL})

TOOL_FINISH_REASONS = frozenset({"tool_calls", "tool_call", "tool_use", "function_call"})


def classify(view: SpanView) -> str:
    if view.ignored:
        return IGNORED
    op = view.operation
    if op in sc.INFERENCE_OPERATIONS:
        return INFERENCE
    if op == sc.OP_INVOKE_AGENT:
        return AGENT
    if op == sc.OP_INVOKE_WORKFLOW:
        return WORKFLOW
    if op == sc.OP_EXECUTE_TOOL:
        return TOOL
    if op:
        return OTHER
    return NON_GENAI


@dataclass(slots=True)
class _Info:
    kind: str
    view: SpanView
    covered: bool = False
    inference_parent: str | None = None
    agent_name: str | None = None
    under_tool: bool = False
    anchor: str | None = None


@dataclass(slots=True)
class TraceAnalysis:
    trace: Trace
    info: dict[str, _Info]
    anchors: list[str]
    members: dict[str, list[str]]
    warnings: list[str] = field(default_factory=list)

    def view(self, span_id: str) -> SpanView:
        return self.info[span_id].view


def _views(trace: Trace) -> dict[str, SpanView]:
    views = {sid: overlay(span, trace.logs_of(sid)) for sid, span in trace.spans.items()}
    loose = [r for r in trace.orphan_logs if not r.span_id]
    if loose:
        inference = [sid for sid, v in views.items() if classify(v) == INFERENCE]
        if len(inference) == 1:
            sid = inference[0]
            views[sid] = overlay(trace.spans[sid], [*trace.logs_of(sid), *loose])
    return views


def analyze(trace: Trace) -> TraceAnalysis:
    """Classify spans and find turn anchors. Two depth first passes, both O(spans)."""
    views = _views(trace)
    info = {sid: _Info(kind=classify(v), view=v) for sid, v in views.items()}

    agentic_anchors: set[str] = set()
    stack: list[tuple[str, bool, str | None, str | None, bool]] = [
        (r, False, None, None, False) for r in reversed(trace.roots)
    ]
    while stack:
        sid, covered, inf_parent, agent_name, under_tool = stack.pop()
        node = info[sid]
        node.covered, node.inference_parent, node.under_tool = covered, inf_parent, under_tool
        own_agent = attr_str(node.view.attrs, sc.AGENT_NAME)
        if node.kind in AGENTIC and own_agent:
            agent_name = own_agent
        node.agent_name = agent_name or (own_agent if node.kind in (INFERENCE, TOOL) else None)
        if node.kind in AGENTIC and not covered:
            agentic_anchors.add(sid)
        child_covered = covered or node.kind in BREAKS_INFERENCE_CHAIN
        if node.kind == INFERENCE:
            child_inf = sid
        elif node.kind in BREAKS_INFERENCE_CHAIN:
            child_inf = None
        else:
            child_inf = inf_parent
        child_tool = under_tool or node.kind == TOOL
        for child in reversed(trace.children.get(sid, ())):
            stack.append((child, child_covered, child_inf, agent_name, child_tool))

    for root in trace.roots:
        default_anchor = None if root in agentic_anchors else root
        stack2: list[tuple[str, str | None]] = [(root, default_anchor)]
        while stack2:
            sid, current = stack2.pop()
            if sid in agentic_anchors:
                current = sid
            info[sid].anchor = current
            for child in trace.children.get(sid, ()):
                stack2.append((child, current))

    members: dict[str, list[str]] = {}
    for sid, node in info.items():
        if node.anchor is not None:
            members.setdefault(node.anchor, []).append(sid)

    anchors = []
    warnings = list(trace.warnings)
    for sid in trace.walk():
        span_id = sid.span_id
        if span_id not in members:
            continue
        kinds = {info[m].kind for m in members[span_id]}
        if span_id in agentic_anchors:
            view = info[span_id].view
            has_messages = view.messages(sc.INPUT_MESSAGES) or view.messages(sc.OUTPUT_MESSAGES)
            if INFERENCE in kinds or TOOL in kinds or has_messages:
                anchors.append(span_id)
            else:
                warnings.append(f"agent span {span_id} has no inference, tools or messages; not a turn")
        elif INFERENCE in kinds:
            anchors.append(span_id)
    return TraceAnalysis(trace=trace, info=info, anchors=anchors, members=members, warnings=warnings)


# ---------------------------------------------------------------- logical LLM calls


def _usage(view: SpanView) -> Usage | None:
    attrs = view.attrs
    values = {
        "input_tokens": attr_int(attrs, sc.USAGE_INPUT_TOKENS),
        "output_tokens": attr_int(attrs, sc.USAGE_OUTPUT_TOKENS),
        "cache_read_input_tokens": attr_int(attrs, sc.USAGE_CACHE_READ_INPUT_TOKENS),
        "cache_write_input_tokens": attr_int(attrs, sc.USAGE_CACHE_WRITE_INPUT_TOKENS),
        "reasoning_output_tokens": attr_int(attrs, sc.USAGE_REASONING_OUTPUT_TOKENS),
    }
    if values["input_tokens"] is None and values["output_tokens"] is None:
        return None
    return Usage(**{k: max(0, v or 0) for k, v in values.items()})


def _status(span: Span) -> str:
    if span.status_code == STATUS_ERROR:
        return "error"
    if span.status_code == STATUS_OK:
        return "ok"
    return "unset"


def _chains(tops: list[str], inf_children: dict[str, list[str]]) -> list[list[str]]:
    """Logical calls as leaf first chains of span ids. One post order pass counts leaves, so the
    grouping is linear in the number of inference spans."""
    leaf_count: dict[str, int] = {}
    single_leaf: dict[str, str] = {}
    for top in tops:
        order: list[str] = []
        walk = [top]
        while walk:
            node = walk.pop()
            order.append(node)
            walk.extend(inf_children.get(node, ()))
        for node in reversed(order):
            kids = inf_children.get(node, ())
            if not kids:
                leaf_count[node] = 1
                single_leaf[node] = node
            else:
                leaf_count[node] = sum(leaf_count[k] for k in kids)
                if leaf_count[node] == 1:
                    single_leaf[node] = single_leaf[kids[0]]

    parent_of = {c: p for p, cs in inf_children.items() for c in cs}
    out: list[list[str]] = []
    pending = list(reversed(tops))
    while pending:
        node = pending.pop()
        if leaf_count[node] == 1:
            chain = [single_leaf[node]]
            while chain[-1] != node:
                chain.append(parent_of[chain[-1]])
            out.append(chain)
        else:
            pending.extend(reversed(inf_children.get(node, [])))
    return out


def _first(chain: Sequence[SpanView], fn) -> Any:
    for view in chain:
        value = fn(view)
        if value:
            return value
    return None


def _logical_call(analysis: TraceAnalysis, chain_ids: list[str]) -> LlmCall:
    chain = [analysis.view(s) for s in chain_ids]
    leaf = chain[0]
    status = _first(chain, lambda v: _status(v.span) if _status(v.span) != "unset" else None) or "unset"
    error_type = _first(chain, lambda v: attr_str(v.attrs, sc.ERROR_TYPE))
    if any(v.exception_event for v in chain) and status == "unset":
        status = "error"
    if status == "error" and not error_type:
        error_type = sc.ERROR_TYPE_OTHER
    info = analysis.info[chain_ids[0]]
    finish = _first(
        chain,
        lambda v: (
            v.attrs.get(sc.RESPONSE_FINISH_REASONS)
            if isinstance(v.attrs.get(sc.RESPONSE_FINISH_REASONS), list)
            else None
        ),
    )
    return LlmCall(
        ref=leaf.span.ref,
        wrapper_ref=chain[-1].span.ref if len(chain) > 1 else None,
        agent_name=info.agent_name or _first(chain, lambda v: attr_str(v.attrs, sc.AGENT_NAME)),
        operation=leaf.operation or sc.OP_CHAT,
        provider=_first(chain, lambda v: attr_str(v.attrs, sc.PROVIDER_NAME)),
        request_model=_first(chain, lambda v: attr_str(v.attrs, sc.REQUEST_MODEL)),
        response_model=_first(chain, lambda v: attr_str(v.attrs, sc.RESPONSE_MODEL)),
        response_id=_first(chain, lambda v: attr_str(v.attrs, sc.RESPONSE_ID)),
        usage=_first(chain, _usage),
        finish_reasons=[str(r) for r in finish or []],
        system_instructions=_first(chain, lambda v: v.messages(sc.SYSTEM_INSTRUCTIONS)) or [],
        input_messages=_first(chain, lambda v: v.messages(sc.INPUT_MESSAGES)) or [],
        output_messages=_first(chain, lambda v: v.messages(sc.OUTPUT_MESSAGES)) or [],
        tool_definitions=_first(chain, lambda v: v.messages(sc.TOOL_DEFINITIONS)) or [],
        status=status,
        error_type=error_type,
        delegated=info.under_tool,
        start_ns=min(v.span.start_time_unix_nano for v in chain),
        end_ns=max(v.span.end_time_unix_nano for v in chain),
    )


def _llm_calls(analysis: TraceAnalysis, member_ids: list[str]) -> list[LlmCall]:
    info = analysis.info
    inference = [m for m in member_ids if info[m].kind == INFERENCE]
    inf_set = set(inference)
    inf_children: dict[str, list[str]] = {}
    tops = []
    for sid in inference:
        parent = info[sid].inference_parent
        if parent in inf_set:
            inf_children.setdefault(parent, []).append(sid)
        else:
            tops.append(sid)
    calls = [_logical_call(analysis, chain) for chain in _chains(tops, inf_children)]
    calls.sort(key=lambda c: (c.start_ns, c.end_ns))
    return calls


# ---------------------------------------------------------------- tool calls


def _json_or_raw(value: Any) -> Any:
    if isinstance(value, str):
        parsed = parse_json_value(value)
        return value if parsed is None else parsed
    return value


def _tool_calls_from_spans(analysis: TraceAnalysis, tool_ids: list[str], warnings: list[str]) -> list[ToolCall]:
    out = []
    for sid in tool_ids:
        node = analysis.info[sid]
        view, span = node.view, node.view.span
        name = attr_str(view.attrs, sc.TOOL_NAME)
        if not name:
            tokens = span.name.split()
            name = tokens[1] if len(tokens) >= 2 and tokens[0] == sc.OP_EXECUTE_TOOL else span.name
            warnings.append(f"tool span {sid} has no gen_ai.tool.name; used span name token {name!r}")
        error_type = attr_str(view.attrs, sc.ERROR_TYPE)
        is_error = span.status_code == STATUS_ERROR or error_type is not None
        out.append(
            ToolCall(
                ref=span.ref,
                agent_name=node.agent_name,
                name=name,
                call_id=attr_str(view.attrs, sc.TOOL_CALL_ID),
                arguments=_json_or_raw(view.attrs.get(sc.TOOL_CALL_ARGUMENTS)),
                result=_json_or_raw(view.attrs.get(sc.TOOL_CALL_RESULT)),
                is_error=is_error,
                error_type=error_type or (sc.ERROR_TYPE_OTHER if is_error else None),
                start_ns=span.start_time_unix_nano,
            )
        )
    return out


def _tool_calls_from_parts(calls: list[LlmCall], warnings: list[str] | None = None) -> list[ToolCall]:
    """Tool calls from ``tool_call`` parts, results from later ``tool_call_response`` parts.

    A response pairs with its call by id. A response whose id matches no call in the turn (some
    producers number call and response parts independently) pairs with the next unclaimed call
    in order, by tool name when the response names one.
    """
    responses: list[dict[str, Any]] = []
    seen: set = set()
    for call in calls:
        for response in tool_responses_of(call.input_messages):
            key = response["id"] or (response.get("name"), repr(response["response"]))
            if key not in seen:
                seen.add(key)
                responses.append(response)
    requested = [(call, tc) for call in calls for tc in tool_calls_of(call.output_messages)]
    call_ids = {tc["id"] for _, tc in requested if tc["id"]}
    claimed: set[int] = set()
    out = []
    for call, tc in requested:
        match = next(
            (i for i, r in enumerate(responses) if i not in claimed and tc["id"] and r["id"] == tc["id"]),
            None,
        )
        if match is None:
            match = next(
                (
                    i
                    for i, r in enumerate(responses)
                    if i not in claimed and r["id"] not in call_ids and r.get("name") in (None, tc["name"])
                ),
                None,
            )
            if match is not None and responses[match]["id"] and warnings is not None:
                warnings.append(
                    f"tool {tc['name']} result joined by order (response id {responses[match]['id']!r} matches no call)"
                )
        if match is not None:
            claimed.add(match)
        out.append(
            ToolCall(
                ref=None,
                agent_name=call.agent_name,
                name=tc["name"],
                call_id=tc["id"],
                arguments=tc["arguments"],
                result=responses[match]["response"] if match is not None else None,
                start_ns=call.end_ns,
            )
        )
    return out


def _is_empty(value: Any) -> bool:
    return value is None or value == {} or value == "" or value == []


def _fill_from_parts(span_tools: list[ToolCall], calls: list[LlmCall], warnings: list[str]) -> None:
    """Complete tool spans that carry no arguments or result from the turn's message parts.

    Some producers (ADK with legacy content off) record arguments and results only as
    ``tool_call`` / ``tool_call_response`` parts and give the span a different call id than
    the part. Each span claims one part in order: by call id, else the first unclaimed part
    with the same tool name. Values already on the span are never replaced.
    """
    part_tools = _tool_calls_from_parts(calls, warnings)
    if not part_tools:
        return
    claimed: set[int] = set()
    for tool in span_tools:
        match = next(
            (i for i, p in enumerate(part_tools) if i not in claimed and tool.call_id and p.call_id == tool.call_id),
            None,
        )
        by_name = False
        if match is None:
            match = next((i for i, p in enumerate(part_tools) if i not in claimed and p.name == tool.name), None)
            by_name = match is not None
        if match is None:
            continue
        claimed.add(match)
        part = part_tools[match]
        filled = []
        if _is_empty(tool.arguments) and not _is_empty(part.arguments):
            tool.arguments = part.arguments
            filled.append("arguments")
        if _is_empty(tool.result) and not _is_empty(part.result):
            tool.result = part.result
            filled.append("result")
        if filled and by_name:
            warnings.append(
                f"tool {tool.name} {' and '.join(filled)} joined from message parts by name (call ids differ)"
            )


# ---------------------------------------------------------------- external evaluations


def _external_evaluations(view: SpanView, logs: Iterable[LogRecord]) -> list[ExternalEvaluation]:
    found: list[tuple[Any, str | None]] = [
        (e.attributes, None) for e in view.span.events if e.name == sc.EVENT_EVALUATION_RESULT
    ]
    found += [
        (r.attributes, attr_str(r.resource.attributes, sc.SERVICE_NAME))
        for r in logs
        if r.event_name == sc.EVENT_EVALUATION_RESULT
    ]
    out = []
    for attrs, service in found:
        name = attr_str(attrs, sc.EVALUATION_NAME)
        if not name:
            continue
        out.append(
            ExternalEvaluation(
                ref=view.span.ref,
                name=name,
                score_value=attr_float(attrs, sc.EVALUATION_SCORE_VALUE),
                score_label=attr_str(attrs, sc.EVALUATION_SCORE_LABEL),
                explanation=attr_str(attrs, sc.EVALUATION_EXPLANATION),
                source_service=service,
            )
        )
    return out


# ---------------------------------------------------------------- turns


def _turn(analysis: TraceAnalysis, anchor: str) -> Turn:
    trace, info = analysis.trace, analysis.info
    member_ids = analysis.members[anchor]
    member_set = set(member_ids)
    ordered = [s.span_id for s in trace.walk(anchor) if s.span_id in member_set]
    anchor_view = info[anchor].view
    warnings: list[str] = []

    calls = _llm_calls(analysis, ordered)
    tool_ids = sorted(
        (m for m in ordered if info[m].kind == TOOL),
        key=lambda s: (trace.spans[s].start_time_unix_nano, s),
    )
    if tool_ids:
        tools = _tool_calls_from_spans(analysis, tool_ids, warnings)
        _fill_from_parts(tools, calls, warnings)
    else:
        tools = _tool_calls_from_parts(calls, warnings)

    anchor_input = anchor_view.messages(sc.INPUT_MESSAGES)
    if anchor_input:
        user_input = user_turn_messages(anchor_input) or anchor_input
    else:
        first = next((c for c in calls if c.input_messages), None)
        user_input = user_turn_messages(first.input_messages) if first else []

    own = [c for c in calls if not c.delegated] or calls
    speaking = [c for c in own if has_text(c.output_messages)]
    anchor_output = anchor_view.messages(sc.OUTPUT_MESSAGES)
    final_call = max(speaking, key=lambda c: (c.end_ns, c.start_ns)) if speaking else None
    if anchor_output:
        final_output: list[Message] = anchor_output
        intermediate = [m for c in speaking for m in c.output_messages if has_text([m])]
        if intermediate and text_of(intermediate[-1:]) == text_of(anchor_output):
            intermediate = intermediate[:-1]
    else:
        final_output = final_call.output_messages if final_call else []
        intermediate = [m for c in speaking if c is not final_call for m in c.output_messages if has_text([m])]

    usage = Usage()
    for call in calls:
        if call.usage:
            usage = usage + call.usage

    agents: set[str] = set()
    for m in ordered:
        name = attr_str(info[m].view.attrs, sc.AGENT_NAME) if info[m].kind in AGENTIC else None
        if name:
            agents.add(name)
    agents.update(c.agent_name for c in calls if c.agent_name)
    agents.update(t.agent_name for t in tools if t.agent_name)

    anchor_span = anchor_view.span
    anchor_status = _status(anchor_span)
    error_type = attr_str(anchor_view.attrs, sc.ERROR_TYPE)
    if anchor_status == "error" and not error_type:
        error_type = sc.ERROR_TYPE_OTHER

    conversation_id = None
    framework_ids: dict[str, str] = {}
    external: list[ExternalEvaluation] = []
    for m in ordered:
        view = info[m].view
        conversation_id = conversation_id or attr_str(view.attrs, sc.CONVERSATION_ID)
        for k, v in view.framework_ids.items():
            framework_ids.setdefault(k, v)
        external.extend(_external_evaluations(view, trace.logs_of(m)))

    content = (
        bool(anchor_input or anchor_output)
        or any(has_content(c.input_messages) or has_content(c.output_messages) for c in calls)
        or any(t.arguments is not None or t.result is not None for t in tools)
    )

    spans = [trace.spans[m] for m in ordered]
    return Turn(
        ref=anchor_span.ref,
        anchor_kind=info[anchor].kind if info[anchor].kind in AGENTIC else "root",
        label=attr_str(anchor_view.attrs, sc.WORKFLOW_NAME) or attr_str(anchor_view.attrs, sc.AGENT_NAME),
        agent_name=attr_str(anchor_view.attrs, sc.AGENT_NAME)
        or next((c.agent_name for c in calls if c.agent_name), None),
        agents=sorted(agents),
        user_input=user_input,
        final_output=final_output,
        intermediate_outputs=intermediate,
        llm_calls=calls,
        tool_calls=tools,
        usage=usage,
        start_ns=min(s.start_time_unix_nano for s in spans),
        end_ns=max(s.end_time_unix_nano for s in spans),
        status=anchor_status,
        error_type=error_type,
        content_captured=content,
        conversation_id=conversation_id,
        framework_ids=framework_ids,
        external_evaluations=external,
        warnings=warnings,
    )


def extract_turns(trace: Trace) -> list[Turn]:
    analysis = analyze(trace)
    return [_turn(analysis, anchor) for anchor in analysis.anchors]


def _last_call(turn: Turn) -> LlmCall | None:
    return max(turn.llm_calls, key=lambda c: (c.end_ns, c.start_ns)) if turn.llm_calls else None


def _first_call(turn: Turn) -> LlmCall | None:
    return min(turn.llm_calls, key=lambda c: (c.start_ns, c.end_ns)) if turn.llm_calls else None


def _continues(previous: Turn, turn: Turn) -> bool:
    if turn.anchor_kind != "root":
        return False
    first, last = _first_call(turn), _last_call(previous)
    return (
        first is not None
        and last is not None
        and ends_with_tool_response(first.input_messages)
        and bool(tool_calls_of(last.output_messages))
    )


def _maybe_continues(previous: Turn, turn: Turn) -> bool:
    """A continuation that cannot be confirmed because message content was not captured."""
    if turn.anchor_kind != "root":
        return False
    first, last = _first_call(turn), _last_call(previous)
    return (
        first is not None
        and last is not None
        and not first.input_messages
        and bool(TOOL_FINISH_REASONS.intersection(last.finish_reasons))
    )


def _merge(previous: Turn, turn: Turn) -> Turn:
    calls = sorted([*previous.llm_calls, *turn.llm_calls], key=lambda c: (c.start_ns, c.end_ns))
    warnings = [*previous.warnings, *turn.warnings]
    span_tools = [t for t in (*previous.tool_calls, *turn.tool_calls) if t.ref is not None]
    if span_tools:
        tools = span_tools
        _fill_from_parts(tools, calls, warnings)
    else:
        tools = _tool_calls_from_parts(calls, warnings)

    if turn.final_output:
        final_output = turn.final_output
        intermediate = [*previous.intermediate_outputs, *previous.final_output, *turn.intermediate_outputs]
    else:
        final_output = previous.final_output
        intermediate = [*previous.intermediate_outputs, *turn.intermediate_outputs]

    failed = previous if previous.status == "error" else turn if turn.status == "error" else None
    framework_ids = dict(turn.framework_ids)
    framework_ids.update(previous.framework_ids)
    return previous.model_copy(
        update={
            "agent_name": previous.agent_name or turn.agent_name,
            "agents": sorted({*previous.agents, *turn.agents}),
            "user_input": previous.user_input or turn.user_input,
            "final_output": final_output,
            "intermediate_outputs": intermediate,
            "llm_calls": calls,
            "tool_calls": tools,
            "usage": previous.usage + turn.usage,
            "start_ns": min(previous.start_ns, turn.start_ns),
            "end_ns": max(previous.end_ns, turn.end_ns),
            "status": failed.status if failed else previous.status,
            "error_type": failed.error_type if failed else previous.error_type,
            "content_captured": previous.content_captured or turn.content_captured,
            "conversation_id": previous.conversation_id or turn.conversation_id,
            "framework_ids": framework_ids,
            "external_evaluations": [*previous.external_evaluations, *turn.external_evaluations],
            "warnings": list(dict.fromkeys(warnings)),
        }
    )


def _join_continuations(turns: list[Turn]) -> list[Turn]:
    out: list[Turn] = []
    for turn in turns:
        if out and _continues(out[-1], turn):
            out[-1] = _merge(out[-1], turn)
            continue
        if out and _maybe_continues(out[-1], turn):
            turn.warnings.append(
                f"turn {turn.ref.span_id} may continue turn {out[-1].ref.span_id} after a tool call, "
                "but message content was not captured; kept as its own turn"
            )
        out.append(turn)
    return out


def extract_conversation(traces: Iterable[Trace], key: str | None = None) -> Conversation:
    """All turns of ``traces`` in start time order, indexed, with tool round continuations joined."""
    traces = list(traces)
    turns = [t for trace in traces for t in extract_turns(trace)]
    turns.sort(key=lambda t: (t.start_ns, t.ref.span_id))
    turns = _join_continuations(turns)
    for i, turn in enumerate(turns):
        turn.index = i
    conversation_id = next((t.conversation_id for t in turns if t.conversation_id), None)
    trace_ids = sorted(
        {t.trace_id for t in traces},
        key=lambda tid: next(tr.start_time_unix_nano for tr in traces if tr.trace_id == tid),
    )
    return Conversation(
        key=key or (traces[0].trace_id if traces else ""),
        conversation_id=conversation_id,
        trace_ids=trace_ids,
        turns=turns,
    )
