import type { Trace, Span, Invocation, ParsedTraceFile, SpanEditMapping, SpanLocationRef } from './types';
import {
  ADK_SCOPE,
  detectTraceFormat,
  findChildrenByOperation,
  findDescendantLLMSpans,
  USER_ROLES,
  ASSISTANT_ROLES,
} from './trace-helpers';

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

export function buildEditMappings(traces: Trace[], _parsedFile: ParsedTraceFile): SpanEditMapping[] {
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

    mappings.push({
      invocationId: rootSpan.spanId,
      format: 'genai',
      userInputSpanId: firstLlm.spanId,
      finalResponseSpanId: lastLlm.spanId,
      toolSpanIds: [],
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

export function applyEditsAndSerialize(
  parsedFile: ParsedTraceFile,
  invocations: Invocation[],
  editMappings: SpanEditMapping[]
): string {
  const mappingByInvId = new Map(editMappings.map(m => [m.invocationId, m]));

  for (const inv of invocations) {
    const mapping = mappingByInvId.get(inv.invocationId);
    if (!mapping) continue;

    const userText = inv.userContent?.parts?.[0]?.text;
    const responseText = inv.finalResponse?.parts?.[0]?.text;

    if (userText !== undefined) {
      patchAttribute(parsedFile, mapping.userInputSpanId, mapping.userInputAttrKey, mapping.format, 'user', userText);
    }
    if (responseText !== undefined) {
      patchAttribute(parsedFile, mapping.finalResponseSpanId, mapping.finalResponseAttrKey, mapping.format, 'response', responseText);
    }
  }

  return serialize(parsedFile);
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
