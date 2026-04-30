import type { Trace, Span, Log } from './types';

interface JaegerTag {
  key: string;
  type: string;
  value: any;
}

interface JaegerLog {
  timestamp: number;
  fields: JaegerTag[];
}

interface JaegerReference {
  refType: string;
  traceID: string;
  spanID: string;
}

interface JaegerSpan {
  traceID: string;
  spanID: string;
  operationName: string;
  references?: JaegerReference[];
  startTime: number;
  duration: number;
  tags?: JaegerTag[];
  logs?: JaegerLog[];
}

interface JaegerTrace {
  traceID: string;
  spans: JaegerSpan[];
}

interface JaegerData {
  data: JaegerTrace[];
}

interface OtlpAttribute {
  key: string;
  value?: {
    stringValue?: string;
    intValue?: number | string;
    doubleValue?: number;
    boolValue?: boolean;
  };
}

// OTLP/JSON encodes int64 values as strings to preserve precision past
// Number.MAX_SAFE_INTEGER (2^53 - 1). Convert only when the value still
// fits a JavaScript safe integer; otherwise keep the original string so
// large IDs/counters/timestamps round-trip without silent corruption.
function parseOtlpIntValue(value: number | string): number | string {
  if (typeof value === 'number') return value;
  if (!/^-?\d+$/.test(value)) return value;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : value;
}

function extractOtlpAttributes(attrs: OtlpAttribute[] | undefined): Record<string, any> {
  const tags: Record<string, any> = {};
  for (const attr of attrs || []) {
    const v = attr.value;
    if (!v) continue;
    if (v.stringValue !== undefined) tags[attr.key] = v.stringValue;
    else if (v.intValue !== undefined) tags[attr.key] = parseOtlpIntValue(v.intValue);
    else if (v.doubleValue !== undefined) tags[attr.key] = v.doubleValue;
    else if (v.boolValue !== undefined) tags[attr.key] = v.boolValue;
  }
  return tags;
}

function buildOtlpSpan(otlpSpan: any, extraTags: Record<string, any> = {}): Span {
  const tags = { ...extraTags, ...extractOtlpAttributes(otlpSpan.attributes) };

  const startTimeNs = parseInt(otlpSpan.startTimeUnixNano || '0');
  const endTimeNs = parseInt(otlpSpan.endTimeUnixNano || '0');
  const startTimeUs = Math.floor(startTimeNs / 1000);
  const durationUs = Math.floor((endTimeNs - startTimeNs) / 1000);

  return {
    traceId: otlpSpan.traceId,
    spanId: otlpSpan.spanId,
    parentSpanId: otlpSpan.parentSpanId || null,
    operationName: otlpSpan.name,
    startTime: startTimeUs,
    duration: durationUs,
    tags,
    logs: [],
    children: [],
  };
}

function buildTracesFromSpans(spans: Span[]): Trace[] {
  const spansByTrace = new Map<string, Span[]>();
  for (const span of spans) {
    if (!spansByTrace.has(span.traceId)) spansByTrace.set(span.traceId, []);
    spansByTrace.get(span.traceId)!.push(span);
  }

  const traces: Trace[] = [];
  for (const [traceId, traceSpans] of spansByTrace.entries()) {
    const spanMap = new Map<string, Span>();
    traceSpans.forEach(span => spanMap.set(span.spanId, span));

    const rootSpans: Span[] = [];
    for (const span of traceSpans) {
      if (span.parentSpanId) {
        const parent = spanMap.get(span.parentSpanId);
        if (parent) {
          parent.children.push(span);
        } else {
          rootSpans.push(span);
        }
      } else {
        rootSpans.push(span);
      }
    }

    const sortSpans = (s: Span[]) => {
      s.sort((a, b) => a.startTime - b.startTime);
      s.forEach((span) => sortSpans(span.children));
    };
    sortSpans(rootSpans);

    traces.push({
      traceId,
      rootSpans,
      allSpans: traceSpans.sort((a, b) => a.startTime - b.startTime),
    });
  }
  return traces;
}

/**
 * Load traces from OTLP JSONL file (one span per line)
 */
function loadOtlpJsonlTraces(fileContent: string): Trace[] {
  const lines = fileContent.trim().split('\n');
  const spans: Span[] = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    spans.push(buildOtlpSpan(JSON.parse(line)));
  }
  return buildTracesFromSpans(spans);
}

/**
 * Load traces from a parsed full OTLP export object (resourceSpans, legacy
 * batches, or Tempo v2 ``trace`` wrapper). The object should already be
 * unwrapped from any ``trace`` envelope.
 */
function loadOtlpDocTraces(doc: any): Trace[] {
  const resourceSpans: any[] = doc.resourceSpans || doc.batches || [];
  const spans: Span[] = [];

  for (const rs of resourceSpans) {
    const resourceTags = extractOtlpAttributes(rs.resource?.attributes);
    const scopeSpans: any[] = rs.scopeSpans || rs.instrumentationLibrarySpans || [];
    for (const ss of scopeSpans) {
      const scope = ss.scope || ss.instrumentationLibrary || {};
      const scopeTags: Record<string, any> = { ...resourceTags };
      if (scope.name) scopeTags['otel.scope.name'] = scope.name;
      if (scope.version) scopeTags['otel.scope.version'] = scope.version;
      for (const otlpSpan of ss.spans || []) {
        spans.push(buildOtlpSpan(otlpSpan, scopeTags));
      }
    }
  }
  return buildTracesFromSpans(spans);
}

function parseJaegerData(jaegerData: JaegerData): Trace[] {
  return jaegerData.data.map((jTrace) => {
    const spanMap = new Map<string, Span>();

    for (const jSpan of jTrace.spans) {
      const span: Span = {
        traceId: jSpan.traceID,
        spanId: jSpan.spanID,
        parentSpanId: extractParentSpanId(jSpan.references),
        operationName: jSpan.operationName,
        startTime: jSpan.startTime,
        duration: jSpan.duration,
        tags: flattenTags(jSpan.tags || []),
        logs: flattenLogs(jSpan.logs || []),
        children: [],
      };

      spanMap.set(span.spanId, span);
    }

    const rootSpans: Span[] = [];
    for (const span of spanMap.values()) {
      if (span.parentSpanId) {
        const parent = spanMap.get(span.parentSpanId);
        if (parent) {
          parent.children.push(span);
        } else {
          rootSpans.push(span);
        }
      } else {
        rootSpans.push(span);
      }
    }

    const sortSpans = (spans: Span[]) => {
      spans.sort((a, b) => a.startTime - b.startTime);
      spans.forEach((span) => sortSpans(span.children));
    };
    sortSpans(rootSpans);

    return {
      traceId: jTrace.traceID,
      rootSpans,
      allSpans: Array.from(spanMap.values()).sort((a, b) => a.startTime - b.startTime),
    };
  });
}

/**
 * Load traces from a trace file. Auto-detects between Jaeger JSON
 * (top-level ``data``), full OTLP JSON (top-level ``resourceSpans``),
 * legacy/Tempo v1 OTLP (top-level ``batches``), Tempo v2 wrapped
 * (``{"trace": {...}}``), and OTLP JSONL (one span per line).
 */
export async function loadTraces(fileContent: string): Promise<Trace[]> {
  const trimmed = fileContent.trim();
  if (!trimmed) return [];

  let singleDoc: any = null;
  try {
    singleDoc = JSON.parse(trimmed);
  } catch {
    if (trimmed.includes('\n')) {
      return loadOtlpJsonlTraces(fileContent);
    }
    throw new Error('Invalid trace file: not valid JSON or JSONL.');
  }

  if (singleDoc && typeof singleDoc === 'object' && !Array.isArray(singleDoc)) {
    let doc = singleDoc;
    if (doc.trace && typeof doc.trace === 'object' &&
        ('resourceSpans' in doc.trace || 'batches' in doc.trace)) {
      doc = doc.trace;
    }

    if ('resourceSpans' in doc || 'batches' in doc) {
      return loadOtlpDocTraces(doc);
    }
    if (Array.isArray(doc.data)) {
      return parseJaegerData(doc as JaegerData);
    }
  }

  throw new Error(
    'Unrecognized trace file format. Expected one of: Jaeger JSON ' +
    '({"data": [...]}), OTLP JSON ({"resourceSpans": [...]} or {"batches": [...]}), ' +
    'or OTLP JSONL (one span per line).'
  );
}

/**
 * Extract parent span ID from references
 */
function extractParentSpanId(references?: JaegerReference[]): string | null {
  if (!references) return null;

  const parentRef = references.find((ref) => ref.refType === 'CHILD_OF');
  return parentRef ? parentRef.spanID : null;
}

/**
 * Flatten Jaeger tags array to a key-value object
 */
function flattenTags(tags: JaegerTag[]): Record<string, any> {
  const result: Record<string, any> = {};

  for (const tag of tags) {
    result[tag.key] = tag.value;
  }

  return result;
}

/**
 * Flatten Jaeger logs
 */
function flattenLogs(logs: JaegerLog[]): Log[] {
  return logs.map((log) => ({
    timestamp: log.timestamp,
    fields: flattenTags(log.fields),
  }));
}

/**
 * Find spans by operation name
 */
export function findSpansByOperation(trace: Trace, operationName: string): Span[] {
  return trace.allSpans.filter((span) => span.operationName.includes(operationName));
}

/**
 * Find spans by tag key-value
 */
export function findSpansByTag(trace: Trace, tagKey: string, tagValue?: any): Span[] {
  return trace.allSpans.filter((span) => {
    if (!(tagKey in span.tags)) return false;
    if (tagValue === undefined) return true;
    return span.tags[tagKey] === tagValue;
  });
}

/**
 * Get span by ID
 */
export function getSpanById(trace: Trace, spanId: string): Span | undefined {
  return trace.allSpans.find((span) => span.spanId === spanId);
}
