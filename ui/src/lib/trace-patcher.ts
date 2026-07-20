import type {
  Trace,
  Span,
  Invocation,
  ParsedTraceFile,
  SpanEditMapping,
  SpanLocationRef,
  ToolCall,
  ToolMessageEditLocation,
  ToolResponse,
} from './types';
import {
  ADK_SCOPE,
  detectTraceFormat,
  findChildrenByOperation,
  findDescendantLLMSpans,
  USER_ROLES,
  ASSISTANT_ROLES,
} from './trace-helpers';

const ADK_TOOL_CALL_ARGS_ATTR = 'gcp.vertex.agent.tool_call_args';
const ADK_TOOL_RESPONSE_ATTR = 'gcp.vertex.agent.tool_response';
const OTEL_TOOL_NAME_ATTR = 'gen_ai.tool.name';
const OTEL_TOOL_CALL_ID_ATTR = 'gen_ai.tool.call.id';
const OTEL_TOOL_CALL_ARGS_ATTR = 'gen_ai.tool.call.arguments';
const OTEL_TOOL_CALL_RESULT_ATTR = 'gen_ai.tool.call.result';

export function parseTraceFileForEditing(content: string, fileName: string): ParsedTraceFile {
  const trimmed = content.trim();
  const isOtlpJsonl = detectOtlpJsonl(trimmed);

  if (isOtlpJsonl) {
    return parseOtlpJsonl(trimmed, fileName);
  }
  return parseJaegerJson(trimmed, fileName);
}

function detectOtlpJsonl(content: string): boolean {
  if (!content.includes('\n') || content.startsWith('[')) return false;
  try {
    const firstLine = content.split('\n')[0].trim();
    const parsed = JSON.parse(firstLine);
    return !('data' in parsed);
  } catch {
    return false;
  }
}

function parseOtlpJsonl(content: string, fileName: string): ParsedTraceFile {
  const lines = content.split('\n').filter(l => l.trim());
  const rawData = lines.map(line => JSON.parse(line));
  const spanIndex = new Map<string, SpanLocationRef>();

  rawData.forEach((span, lineIndex) => {
    if (span.spanId) {
      spanIndex.set(span.spanId, { lineIndex });
    }
  });

  return { format: 'otlp-jsonl', fileName, rawData, spanIndex };
}

function parseJaegerJson(content: string, fileName: string): ParsedTraceFile {
  const rawData = JSON.parse(content);
  const spanIndex = new Map<string, SpanLocationRef>();

  if (rawData.data && Array.isArray(rawData.data)) {
    rawData.data.forEach((trace: any, traceIndex: number) => {
      if (trace.spans && Array.isArray(trace.spans)) {
        trace.spans.forEach((span: any, spanIdx: number) => {
          if (span.spanID) {
            spanIndex.set(span.spanID, { traceIndex, spanIndex: spanIdx });
          }
        });
      }
    });
  }

  return { format: 'jaeger', fileName, rawData, spanIndex };
}

export function buildEditMappings(traces: Trace[]): SpanEditMapping[] {
  const mappings: SpanEditMapping[] = [];

  for (const trace of traces) {
    const format = detectTraceFormat(trace);

    if (format === 'adk') {
      mappings.push(...buildAdkMappings(trace));
    } else {
      mappings.push(...buildGenAIMappings(trace));
    }
  }

  return mappings;
}

function buildAdkMappings(trace: Trace): SpanEditMapping[] {
  const mappings: SpanEditMapping[] = [];

  const agentSpans = trace.allSpans.filter(
    (span) =>
      span.operationName.includes('invoke_agent') &&
      span.tags['otel.scope.name'] === ADK_SCOPE
  );

  for (const agentSpan of agentSpans) {
    const llmSpans = findChildrenByOperation(agentSpan, 'call_llm');
    const toolSpans = findChildrenByOperation(agentSpan, 'execute_tool');

    if (llmSpans.length === 0) continue;

    mappings.push({
      invocationId: agentSpan.spanId,
      format: 'adk',
      userInputSpanId: llmSpans[0].spanId,
      finalResponseSpanId: llmSpans[llmSpans.length - 1].spanId,
      toolSpanIds: toolSpans.map(s => s.spanId),
      userInputAttrKey: 'gcp.vertex.agent.llm_request',
      finalResponseAttrKey: 'gcp.vertex.agent.llm_response',
    });
  }

  return mappings;
}

function buildGenAIMappings(trace: Trace): SpanEditMapping[] {
  const mappings: SpanEditMapping[] = [];

  const llmRootSpans = trace.rootSpans.filter(span =>
    span.tags['gen_ai.request.model'] || span.tags['gen_ai.system']
  );

  const rootSpansToCheck = llmRootSpans.length > 0
    ? llmRootSpans
    : trace.rootSpans.slice(0, 1);

  for (const rootSpan of rootSpansToCheck) {
    const llmSpans = findDescendantLLMSpans(rootSpan);
    if (llmSpans.length === 0) continue;

    const firstLlm = llmSpans[0];
    const lastLlm = llmSpans[llmSpans.length - 1];

    const userInputAttrKey = resolveInputAttrKey(firstLlm);
    const finalResponseAttrKey = resolveOutputAttrKey(lastLlm);

    if (!userInputAttrKey || !finalResponseAttrKey) continue;

    const toolMessageLocations = llmSpans
      .map((span): ToolMessageEditLocation | null => {
        const attrKey = resolveOutputAttrKey(span);
        return attrKey ? { spanId: span.spanId, attrKey } : null;
      })
      .filter((location): location is ToolMessageEditLocation => location !== null);
    const toolSpans = findGenAIToolSpans(rootSpan);

    mappings.push({
      invocationId: rootSpan.spanId,
      format: 'genai',
      userInputSpanId: firstLlm.spanId,
      finalResponseSpanId: lastLlm.spanId,
      toolSpanIds: toolSpans.map(s => s.spanId),
      toolMessageLocations,
      userInputAttrKey,
      finalResponseAttrKey,
    });
  }

  return mappings;
}

function resolveInputAttrKey(span: Span): string | null {
  if (span.tags['gen_ai.input.messages']) return 'gen_ai.input.messages';
  if (span.tags['gen_ai.prompt']) return 'gen_ai.prompt';
  if (span.tags['gen_ai.request.messages']) return 'gen_ai.request.messages';
  return null;
}

function resolveOutputAttrKey(span: Span): string | null {
  if (span.tags['gen_ai.output.messages']) return 'gen_ai.output.messages';
  if (span.tags['gen_ai.completion']) return 'gen_ai.completion';
  if (span.tags['gen_ai.response.messages']) return 'gen_ai.response.messages';
  return null;
}

function findGenAIToolSpans(root: Span): Span[] {
  const results: Span[] = [];
  const queue = [...root.children];

  while (queue.length > 0) {
    const span = queue.shift()!;
    if (
      span.operationName.startsWith('execute_tool') ||
      span.tags[OTEL_TOOL_NAME_ATTR] ||
      span.tags[OTEL_TOOL_CALL_ARGS_ATTR] ||
      span.tags[OTEL_TOOL_CALL_RESULT_ATTR]
    ) {
      results.push(span);
    }
    queue.push(...span.children);
  }

  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}

export function applyEditsAndSerialize(
  parsedFile: ParsedTraceFile,
  invocations: Invocation[],
  editMappings: SpanEditMapping[]
): string {
  const mappingByInvId = new Map(editMappings.map(m => [m.invocationId, m]));

  for (const inv of invocations) {
    const mapping = findMappingForInvocation(inv.invocationId, mappingByInvId);
    if (!mapping) continue;

    const userText = inv.userContent?.parts?.[0]?.text;
    const responseText = inv.finalResponse?.parts?.[0]?.text;

    if (userText !== undefined) {
      patchAttribute(parsedFile, mapping.userInputSpanId, mapping.userInputAttrKey, mapping.format, 'user', userText);
    }
    if (responseText !== undefined) {
      patchAttribute(parsedFile, mapping.finalResponseSpanId, mapping.finalResponseAttrKey, mapping.format, 'response', responseText);
    }

    const toolUses = inv.intermediateData?.toolUses || [];
    const toolResponses = inv.intermediateData?.toolResponses || [];
    if (toolUses.length > 0 || toolResponses.length > 0) {
      patchToolTrajectory(parsedFile, mapping, toolUses, toolResponses);
    }
  }

  return serialize(parsedFile);
}

function findMappingForInvocation(
  invocationId: string,
  mappingByInvId: Map<string, SpanEditMapping>
): SpanEditMapping | undefined {
  const direct = mappingByInvId.get(invocationId);
  if (direct) return direct;

  if (invocationId.startsWith('genai-')) {
    return mappingByInvId.get(invocationId.slice('genai-'.length));
  }

  return undefined;
}

function patchToolTrajectory(
  parsedFile: ParsedTraceFile,
  mapping: SpanEditMapping,
  toolUses: ToolCall[],
  toolResponses: ToolResponse[]
): void {
  mapping.toolSpanIds.forEach((spanId, index) => {
    const toolUse = toolUses[index];
    if (!toolUse) return;

    const rawSpan = getRawSpan(parsedFile, spanId);
    if (!rawSpan) return;

    patchRawToolSpan(rawSpan, parsedFile.format, mapping.format, toolUse, toolResponses[index]);
  });

  if (mapping.toolMessageLocations && toolUses.length > 0) {
    patchToolMessageLocations(parsedFile, mapping.toolMessageLocations, toolUses);
  }
}

function getRawSpan(parsedFile: ParsedTraceFile, spanId: string): any | null {
  const locRef = parsedFile.spanIndex.get(spanId);
  if (!locRef) return null;

  if (parsedFile.format === 'otlp-jsonl') {
    return parsedFile.rawData[locRef.lineIndex!];
  }

  return parsedFile.rawData.data?.[locRef.traceIndex!]?.spans?.[locRef.spanIndex!] || null;
}

function patchRawToolSpan(
  rawSpan: any,
  rawFormat: ParsedTraceFile['format'],
  traceFormat: SpanEditMapping['format'],
  toolUse: ToolCall,
  toolResponse?: ToolResponse
): void {
  patchRawToolOperationName(rawSpan, toolUse.name);

  if (traceFormat === 'adk' || hasRawStringAttribute(rawSpan, rawFormat, ADK_TOOL_CALL_ARGS_ATTR)) {
    setRawStringAttribute(rawSpan, rawFormat, ADK_TOOL_CALL_ARGS_ATTR, JSON.stringify(toolUse.args || {}), traceFormat === 'adk');
  }

  if (hasRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_NAME_ATTR)) {
    setRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_NAME_ATTR, toolUse.name, false);
  }
  if (hasRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_ARGS_ATTR)) {
    setRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_ARGS_ATTR, JSON.stringify(toolUse.args || {}), false);
  }

  if (toolUse.id !== undefined || hasRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_ID_ATTR)) {
    setRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_ID_ATTR, toolUse.id || '', toolUse.id !== undefined);
  }

  if (toolResponse) {
    if (traceFormat === 'adk' || hasRawStringAttribute(rawSpan, rawFormat, ADK_TOOL_RESPONSE_ATTR)) {
      setRawStringAttribute(rawSpan, rawFormat, ADK_TOOL_RESPONSE_ATTR, JSON.stringify(toolResponse.response || {}), traceFormat === 'adk');
    }

    if (hasRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_RESULT_ATTR)) {
      setRawStringAttribute(rawSpan, rawFormat, OTEL_TOOL_CALL_RESULT_ATTR, JSON.stringify(toolResponse.response || {}), false);
    }
  }
}

function patchRawToolOperationName(rawSpan: any, toolName: string): void {
  const nextOperationName = `execute_tool ${toolName}`;

  if (typeof rawSpan.operationName === 'string' && rawSpan.operationName.startsWith('execute_tool')) {
    rawSpan.operationName = nextOperationName;
  }
  if (typeof rawSpan.name === 'string' && rawSpan.name.startsWith('execute_tool')) {
    rawSpan.name = nextOperationName;
  }
}

function hasRawStringAttribute(rawSpan: any, format: ParsedTraceFile['format'], attrKey: string): boolean {
  const attrs = format === 'otlp-jsonl' ? rawSpan.attributes : rawSpan.tags;
  return Array.isArray(attrs) && attrs.some((attr: any) => attr.key === attrKey);
}

function readRawStringAttribute(rawSpan: any, format: ParsedTraceFile['format'], attrKey: string): string | null {
  const attrs = format === 'otlp-jsonl' ? rawSpan.attributes : rawSpan.tags;
  if (!Array.isArray(attrs)) return null;

  const attr = attrs.find((candidate: any) => candidate.key === attrKey);
  if (!attr) return null;

  if (format === 'otlp-jsonl') {
    return typeof attr.value?.stringValue === 'string' ? attr.value.stringValue : null;
  }
  return typeof attr.value === 'string' ? attr.value : null;
}

function setRawStringAttribute(
  rawSpan: any,
  format: ParsedTraceFile['format'],
  attrKey: string,
  value: string,
  createIfMissing: boolean
): void {
  const containerKey = format === 'otlp-jsonl' ? 'attributes' : 'tags';
  if (!Array.isArray(rawSpan[containerKey])) {
    if (!createIfMissing) return;
    rawSpan[containerKey] = [];
  }

  const attrs = rawSpan[containerKey];
  const attr = attrs.find((candidate: any) => candidate.key === attrKey);
  if (attr) {
    if (format === 'otlp-jsonl') {
      attr.value = { stringValue: value };
    } else {
      attr.type = 'string';
      attr.value = value;
    }
    return;
  }

  if (!createIfMissing) return;

  if (format === 'otlp-jsonl') {
    attrs.push({ key: attrKey, value: { stringValue: value } });
  } else {
    attrs.push({ key: attrKey, type: 'string', value });
  }
}

function patchToolMessageLocations(
  parsedFile: ParsedTraceFile,
  locations: ToolMessageEditLocation[],
  toolUses: ToolCall[]
): void {
  for (const location of locations) {
    const rawSpan = getRawSpan(parsedFile, location.spanId);
    if (!rawSpan) continue;

    const current = readRawStringAttribute(rawSpan, parsedFile.format, location.attrKey);
    if (!current) continue;

    try {
      const data = JSON.parse(current);
      const changed = patchGenAIToolMessages(data, toolUses);
      if (changed) {
        setRawStringAttribute(rawSpan, parsedFile.format, location.attrKey, JSON.stringify(data), false);
      }
    } catch {
      continue;
    }
  }
}

function patchGenAIToolMessages(messages: any, toolUses: ToolCall[]): boolean {
  if (!Array.isArray(messages)) return false;

  const toolsById = new Map(
    toolUses
      .filter((toolUse) => toolUse.id)
      .map((toolUse) => [toolUse.id, toolUse])
  );
  let nextToolIndex = 0;
  let changed = false;

  const nextTool = (toolCallId: unknown): ToolCall | null => {
    const positionalToolUse = toolUses[nextToolIndex];
    nextToolIndex += 1;

    if (typeof toolCallId === 'string') {
      const byId = toolsById.get(toolCallId);
      if (byId) return byId;
    }

    return positionalToolUse || null;
  };

  for (const msg of messages) {
    if (!msg || typeof msg !== 'object') continue;
    if (msg.role && !ASSISTANT_ROLES.includes(msg.role)) continue;

    if (Array.isArray(msg.tool_calls)) {
      for (const toolCall of msg.tool_calls) {
        const toolUse = nextTool(toolCall?.id);
        if (toolUse && patchOpenAIToolCall(toolCall, toolUse)) {
          changed = true;
        }
      }
    }

    if (Array.isArray(msg.parts)) {
      for (const part of msg.parts) {
        if (!part || typeof part !== 'object' || part.type !== 'tool_call') continue;

        const toolUse = nextTool(part.id);
        if (toolUse && patchGenAIToolPart(part, toolUse)) {
          changed = true;
        }
      }
    }
  }

  return changed;
}

function patchOpenAIToolCall(toolCall: any, toolUse: ToolCall): boolean {
  if (!toolCall || typeof toolCall !== 'object') return false;

  let changed = false;
  if (toolUse.id !== undefined && toolCall.id !== toolUse.id) {
    toolCall.id = toolUse.id;
    changed = true;
  }

  const fn = toolCall.function;
  if (fn && typeof fn === 'object') {
    if (fn.name !== toolUse.name) {
      fn.name = toolUse.name;
      changed = true;
    }
    const nextArgs = JSON.stringify(toolUse.args || {});
    if (fn.arguments !== nextArgs) {
      fn.arguments = nextArgs;
      changed = true;
    }
  }

  return changed;
}

function patchGenAIToolPart(part: any, toolUse: ToolCall): boolean {
  let changed = false;

  if (toolUse.id !== undefined && part.id !== toolUse.id) {
    part.id = toolUse.id;
    changed = true;
  }
  if (part.name !== toolUse.name) {
    part.name = toolUse.name;
    changed = true;
  }

  const nextArgs = typeof part.arguments === 'string'
    ? JSON.stringify(toolUse.args || {})
    : toolUse.args || {};
  if (JSON.stringify(part.arguments) !== JSON.stringify(nextArgs)) {
    part.arguments = nextArgs;
    changed = true;
  }

  return changed;
}

function patchAttribute(
  parsedFile: ParsedTraceFile,
  spanId: string,
  attrKey: string,
  format: 'adk' | 'genai',
  field: 'user' | 'response',
  newText: string
): void {
  const locRef = parsedFile.spanIndex.get(spanId);
  if (!locRef) return;

  if (parsedFile.format === 'otlp-jsonl') {
    patchOtlpAttribute(parsedFile.rawData[locRef.lineIndex!], attrKey, format, field, newText);
  } else {
    const span = parsedFile.rawData.data[locRef.traceIndex!].spans[locRef.spanIndex!];
    patchJaegerAttribute(span, attrKey, format, field, newText);
  }
}

function patchOtlpAttribute(
  rawSpan: any,
  attrKey: string,
  format: 'adk' | 'genai',
  field: 'user' | 'response',
  newText: string
): void {
  const attrs = rawSpan.attributes;
  if (!Array.isArray(attrs)) return;

  const attr = attrs.find((a: any) => a.key === attrKey);
  if (!attr?.value?.stringValue) return;

  const patched = patchJsonValue(attr.value.stringValue, format, field, newText);
  if (patched !== null) {
    attr.value.stringValue = patched;
  }
}

function patchJaegerAttribute(
  rawSpan: any,
  attrKey: string,
  format: 'adk' | 'genai',
  field: 'user' | 'response',
  newText: string
): void {
  const tags = rawSpan.tags;
  if (!Array.isArray(tags)) return;

  const tag = tags.find((t: any) => t.key === attrKey);
  if (!tag) return;

  const patched = patchJsonValue(tag.value, format, field, newText);
  if (patched !== null) {
    tag.value = patched;
  }
}

function patchJsonValue(
  jsonStr: string,
  format: 'adk' | 'genai',
  field: 'user' | 'response',
  newText: string
): string | null {
  try {
    const data = JSON.parse(jsonStr);

    if (format === 'adk') {
      return patchAdkJsonValue(data, field, newText);
    } else {
      return patchGenAIJsonValue(data, field, newText);
    }
  } catch {
    return null;
  }
}

function patchAdkJsonValue(data: any, field: 'user' | 'response', newText: string): string {
  if (field === 'user') {
    const contents = data.contents;
    if (Array.isArray(contents)) {
      for (let i = contents.length - 1; i >= 0; i--) {
        if (contents[i].role === 'user') {
          const textParts = contents[i].parts?.filter((p: any) => p.text !== undefined);
          if (textParts && textParts.length > 0) {
            textParts[0].text = newText;
            break;
          }
        }
      }
    }
  } else {
    const parts = data.content?.parts;
    if (Array.isArray(parts)) {
      const textParts = parts.filter((p: any) => p.text !== undefined);
      if (textParts.length > 0) {
        textParts[0].text = newText;
      }
    }
  }

  return JSON.stringify(data);
}

function patchGenAIJsonValue(data: any, field: 'user' | 'response', newText: string): string {
  if (!Array.isArray(data)) return JSON.stringify(data);

  const targetRoles = field === 'user' ? USER_ROLES : ASSISTANT_ROLES;

  for (let i = data.length - 1; i >= 0; i--) {
    const msg = data[i];
    if (!targetRoles.includes(msg.role)) continue;

    if (typeof msg.content === 'string') {
      msg.content = newText;
      break;
    }
    if (Array.isArray(msg.content)) {
      const textItem = msg.content.find((item: any) => typeof item === 'object' && item.text);
      if (textItem) {
        textItem.text = newText;
        break;
      }
    }
    if (Array.isArray(msg.parts)) {
      const textPart = msg.parts.find((p: any) => typeof p === 'object' && p.type === 'text');
      if (textPart) {
        textPart.content = newText;
        break;
      }
    }
    msg.content = newText;
    break;
  }

  return JSON.stringify(data);
}

function serialize(parsedFile: ParsedTraceFile): string {
  if (parsedFile.format === 'otlp-jsonl') {
    return parsedFile.rawData.map((line: any) => JSON.stringify(line)).join('\n');
  }
  return JSON.stringify(parsedFile.rawData, null, 2);
}
