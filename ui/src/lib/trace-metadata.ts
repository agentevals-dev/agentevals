import { readFileAsText, safeJsonParse } from './utils';
import type { Trace, Span, Invocation } from './types';
import { convertTracesToInvocations } from './trace-converter';

export interface TraceMetadata {
  traceId: string;
  sessionId?: string;
  agentName?: string;
  startTime?: number;
  model?: string;
  userInputPreview?: string;
  finalOutputPreview?: string;
  invocations?: Invocation[];
}

const TAG_SCOPE = 'otel.scope.name';
const ADK_SCOPE = 'gcp.vertex.agent';
const TAG_AGENT_NAME = 'gen_ai.agent.name';
const TAG_MODEL = 'gen_ai.request.model';
const TAG_LLM_REQUEST = 'gcp.vertex.agent.llm_request';
const TAG_LLM_RESPONSE = 'gcp.vertex.agent.llm_response';

function findAdkSpans(trace: Trace, operation: string): Span[] {
  const matches: Span[] = [];

  for (const span of trace.allSpans) {
    if (span.tags?.[TAG_SCOPE] !== ADK_SCOPE) {
      continue;
    }
    if (span.operationName.startsWith(operation)) {
      matches.push(span);
    }
  }

  matches.sort((a, b) => a.startTime - b.startTime);
  return matches;
}

function extractTextPreview(text: string, maxLength: number = 100): string {
  if (!text) return '';
  return text.length > maxLength ? text.substring(0, maxLength) + '...' : text;
}

function extractUserInputPreview(llmRequestTag: string): string {
  const llmRequest = safeJsonParse<any>(llmRequestTag, {});
  const contents = llmRequest.contents || [];

  for (let i = contents.length - 1; i >= 0; i--) {
    const content = contents[i];
    if (content.role === 'user') {
      const parts = content.parts || [];
      const textParts = parts
        .filter((p: any) => p.text)
        .map((p: any) => p.text);
      if (textParts.length > 0) {
        const fullText = textParts.join(' ');
        return extractTextPreview(fullText);
      }
    }
  }

  return '';
}

function extractFinalOutputPreview(llmResponseTag: string): string {
  const llmResponse = safeJsonParse<any>(llmResponseTag, {});
  const content = llmResponse.content || {};
  const parts = content.parts || [];
  const textParts = parts
    .filter((p: any) => p.text)
    .map((p: any) => p.text);

  if (textParts.length > 0) {
    const fullText = textParts.join(' ');
    return extractTextPreview(fullText);
  }

  return '';
}

function buildSpanTree(trace: Trace): void {
  const spanMap = new Map<string, Span>();
  for (const span of trace.allSpans) {
    spanMap.set(span.spanId, span);
    span.children = [];
  }

  for (const span of trace.allSpans) {
    if (span.parentSpanId) {
      const parent = spanMap.get(span.parentSpanId);
      if (parent) {
        parent.children.push(span);
      }
    }
  }

  trace.rootSpans = trace.allSpans.filter(
    span => !span.parentSpanId || !spanMap.has(span.parentSpanId)
  );
}

export function extractTraceMetadata(trace: Trace): TraceMetadata {
  const metadata: TraceMetadata = {
    traceId: trace.traceId,
  };

  const invokeSpans = findAdkSpans(trace, 'invoke_agent');
  if (invokeSpans.length > 0) {
    const invokeSpan = invokeSpans[0];
    metadata.agentName = invokeSpan.tags?.[TAG_AGENT_NAME];
    metadata.startTime = invokeSpan.startTime;
  }

  const callLlmSpans = findAdkSpans(trace, 'call_llm');
  if (callLlmSpans.length > 0) {
    const firstLlm = callLlmSpans[0];
    metadata.model = firstLlm.tags?.[TAG_MODEL];

    const llmRequestTag = firstLlm.tags?.[TAG_LLM_REQUEST];
    if (llmRequestTag) {
      metadata.userInputPreview = extractUserInputPreview(llmRequestTag);
    }

    const lastLlm = callLlmSpans[callLlmSpans.length - 1];
    const llmResponseTag = lastLlm.tags?.[TAG_LLM_RESPONSE];
    if (llmResponseTag) {
      metadata.finalOutputPreview = extractFinalOutputPreview(llmResponseTag);
    }
  }

  metadata.sessionId = metadata.agentName || trace.traceId.substring(0, 12);

  return metadata;
}

function parseOtlpJsonl(content: string): Trace[] {
  const lines = content.trim().split('\n');
  const spansByTrace: Map<string, Span[]> = new Map();

  for (const line of lines) {
    if (!line.trim()) continue;

    const otlpSpan = safeJsonParse<any>(line, null);
    if (!otlpSpan) continue;

    const traceId = otlpSpan.traceId;
    if (!traceId) continue;

    const tags: Record<string, any> = {};
    for (const attr of otlpSpan.attributes || []) {
      const value = attr.value?.stringValue || attr.value?.intValue || attr.value?.doubleValue || attr.value?.boolValue;
      if (value !== undefined) {
        tags[attr.key] = value;
      }
    }

    const startTimeNs = parseInt(otlpSpan.startTimeUnixNano || '0');
    const endTimeNs = parseInt(otlpSpan.endTimeUnixNano || '0');
    const startTimeUs = Math.floor(startTimeNs / 1000);
    const durationUs = Math.floor((endTimeNs - startTimeNs) / 1000);

    const span: Span = {
      traceId,
      spanId: otlpSpan.spanId,
      parentSpanId: otlpSpan.parentSpanId || null,
      operationName: otlpSpan.name,
      startTime: startTimeUs,
      duration: durationUs,
      tags,
      logs: [],
      children: [],
    };

    if (!spansByTrace.has(traceId)) {
      spansByTrace.set(traceId, []);
    }
    spansByTrace.get(traceId)!.push(span);
  }

  const traces: Trace[] = [];
  for (const [traceId, spans] of spansByTrace.entries()) {
    traces.push({
      traceId,
      rootSpans: [],
      allSpans: spans,
    });
  }

  return traces;
}

export async function extractMetadataFromTraceFile(file: File): Promise<TraceMetadata[]> {
  const content = await readFileAsText(file);

  let traces: Trace[] = [];

  const trimmedContent = content.trim();
  if (trimmedContent.startsWith('{') && !trimmedContent.startsWith('{"data"')) {
    traces = parseOtlpJsonl(content);
  } else {
    const jaegerData = safeJsonParse<any>(content, null);

    if (!jaegerData || !jaegerData.data) {
      throw new Error('Invalid trace format');
    }

    for (const jaegerTrace of jaegerData.data) {
      const traceId = jaegerTrace.traceID;
      const spans: Span[] = [];

      for (const jaegerSpan of jaegerTrace.spans || []) {
        const tags: Record<string, any> = {};
        for (const tag of jaegerSpan.tags || []) {
          tags[tag.key] = tag.value;
        }

        spans.push({
          traceId,
          spanId: jaegerSpan.spanID,
          parentSpanId: jaegerSpan.references?.[0]?.spanID || null,
          operationName: jaegerSpan.operationName,
          startTime: jaegerSpan.startTime,
          duration: jaegerSpan.duration,
          tags,
          logs: [],
          children: [],
        });
      }

      traces.push({
        traceId,
        rootSpans: [],
        allSpans: spans,
      });
    }
  }

  for (const trace of traces) {
    buildSpanTree(trace);
  }

  const invocationsMap = convertTracesToInvocations(traces);

  return traces.map(trace => {
    const metadata = extractTraceMetadata(trace);
    const conversionResult = invocationsMap.get(trace.traceId);
    if (conversionResult) {
      metadata.invocations = conversionResult.invocations;
    }
    return metadata;
  });
}
