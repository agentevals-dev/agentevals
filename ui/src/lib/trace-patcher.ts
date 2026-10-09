import type { Trace, Span, Invocation, ParsedTraceFile, SpanEditMapping, SpanLocationRef } from './types';
import { USER_ROLES, ASSISTANT_ROLES } from './trace-helpers';

interface RawAttr {
  key: string;
  value?: { stringValue?: string };
}

interface RawTag {
  key: string;
  value?: unknown;
}

interface RawSpan {
  spanId?: string;
  spanID?: string;
  attributes?: RawAttr[];
  events?: { attributes?: RawAttr[] }[];
  tags?: RawTag[];
}

interface RawScopeSpans {
  spans?: RawSpan[];
}

interface RawOtlp {
  spanId?: string;
  resourceSpans?: { scopeSpans?: RawScopeSpans[]; instrumentationLibrarySpans?: RawScopeSpans[] }[];
  batches?: { scopeSpans?: RawScopeSpans[]; instrumentationLibrarySpans?: RawScopeSpans[] }[];
}

const ADK_REQUEST_KEY = 'gcp.vertex.agent.llm_request';
const ADK_RESPONSE_KEY = 'gcp.vertex.agent.llm_response';

/**
 * Parse a trace file so its message attributes can be patched in place.
 *
 * Accepts Jaeger JSON, an OTLP/JSON document (``resourceSpans``), and JSONL whose lines are OTLP
 * export requests (the Collector file exporter, live session exports) or bare OTLP spans. Every
 * raw span object is indexed by span id, so patches edit the original structure.
 */
export function parseTraceFileForEditing(content: string, fileName: string): ParsedTraceFile {
  const trimmed = content.trim();
  const spanIndex = new Map<string, SpanLocationRef>();

  const whole = parseJson(trimmed);
  const jaeger = whole as { data?: { spans?: RawSpan[] }[] } | undefined;

  if (jaeger && typeof jaeger === 'object' && !Array.isArray(jaeger) && Array.isArray(jaeger.data)) {
    for (const trace of jaeger.data) {
      for (const span of trace.spans || []) {
        if (span.spanID) spanIndex.set(span.spanID, { raw: span });
      }
    }
    return { format: 'jaeger', fileName, rawData: whole, spanIndex };
  }

  if (whole !== undefined) {
    indexOtlp(whole as RawOtlp, spanIndex);
    return { format: 'otlp-json', fileName, rawData: whole, spanIndex };
  }

  const lines = trimmed.split('\n').filter(l => l.trim()).map(line => JSON.parse(line) as RawOtlp);
  lines.forEach(line => indexOtlp(line, spanIndex));
  return { format: 'otlp-jsonl', fileName, rawData: lines, spanIndex };
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function indexOtlp(value: RawOtlp | null, spanIndex: Map<string, SpanLocationRef>): void {
  if (!value || typeof value !== 'object') return;
  const resources = value.resourceSpans || value.batches;
  if (Array.isArray(resources)) {
    for (const rs of resources) {
      for (const ss of rs.scopeSpans || rs.instrumentationLibrarySpans || []) {
        for (const span of ss.spans || []) {
          if (span.spanId) spanIndex.set(span.spanId, { raw: span });
        }
      }
    }
  } else if (value.spanId) {
    spanIndex.set(value.spanId, { raw: value });
  }
}

const MESSAGE_KEYS = {
  user: ['gen_ai.input.messages', 'gen_ai.prompt', 'gen_ai.request.messages'],
  response: ['gen_ai.output.messages', 'gen_ai.completion', 'gen_ai.response.messages'],
};
const INDEXED_PREFIX = { user: 'gen_ai.prompt.', response: 'gen_ai.completion.' };

type Field = 'user' | 'response';

function isModelCall(span: Span): boolean {
  return (
    !!span.tags[ADK_REQUEST_KEY] ||
    !!span.tags[ADK_RESPONSE_KEY] ||
    [...MESSAGE_KEYS.user, ...MESSAGE_KEYS.response].some(key => !!span.tags[key]) ||
    span.tags['gen_ai.prompt.0.content'] !== undefined ||
    span.tags['gen_ai.completion.0.content'] !== undefined
  );
}

/** Messages on the span itself win over its model calls, as in the backend. */
function carries(span: Span, field: Field): boolean {
  return span.tags[MESSAGE_KEYS[field][0]] !== undefined;
}

/**
 * Edit targets for every span that can anchor a turn, keyed by span id (the ``invocationId``
 * the backend reports). A span's target is the first and last innermost model call in its
 * subtree: like the backend, a wrapper call defers to the provider call inside it, so agent,
 * workflow and bare root anchors all edit the content the backend reads.
 */
export function buildEditMappings(traces: Trace[]): SpanEditMapping[] {
  const mappings: SpanEditMapping[] = [];

  for (const trace of traces) {
    const range = new Map<string, { first: Span; last: Span }>();
    const order: Span[] = [];
    const stack: Span[] = [...trace.rootSpans];
    while (stack.length > 0) {
      const span = stack.pop()!;
      order.push(span);
      stack.push(...span.children);
    }
    for (let i = order.length - 1; i >= 0; i--) {
      const span = order[i];
      let first: Span | undefined;
      let last: Span | undefined;
      for (const child of span.children) {
        const r = range.get(child.spanId);
        if (!r) continue;
        if (!first || r.first.startTime < first.startTime) first = r.first;
        if (!last || r.last.startTime > last.startTime) last = r.last;
      }
      if (!first && isModelCall(span)) first = last = span;
      if (first && last) range.set(span.spanId, { first, last });
    }

    const byId = new Map(order.map(span => [span.spanId, span]));
    for (const [spanId, { first, last }] of range) {
      const anchor = byId.get(spanId)!;
      mappings.push({
        invocationId: spanId,
        userInputSpanId: carries(anchor, 'user') ? spanId : first.spanId,
        finalResponseSpanId: carries(anchor, 'response') ? spanId : last.spanId,
      });
    }
  }

  return mappings;
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
      patchSpan(parsedFile, mapping.userInputSpanId, 'user', userText);
    }
    if (responseText !== undefined) {
      patchSpan(parsedFile, mapping.finalResponseSpanId, 'response', responseText);
    }
  }

  return serialize(parsedFile);
}

interface AttrStore {
  get(key: string): unknown;
  set(key: string, value: string): void;
}

function otlpStore(rawSpan: RawSpan): AttrStore {
  // Span attributes first, then span event attributes (some frameworks put messages in events).
  const lists = [rawSpan.attributes, ...(rawSpan.events || []).map(e => e.attributes)].filter(
    (attrs): attrs is RawAttr[] => Array.isArray(attrs)
  );
  const find = (key: string) => {
    for (const attrs of lists) {
      const attr = attrs.find(a => a.key === key);
      if (attr) return attr;
    }
    return undefined;
  };
  return {
    get: key => find(key)?.value?.stringValue,
    set: (key, value) => {
      for (const attrs of lists) {
        for (const attr of attrs) {
          if (attr.key === key && attr.value?.stringValue !== undefined) attr.value = { stringValue: value };
        }
      }
    },
  };
}

function jaegerStore(rawSpan: RawSpan): AttrStore {
  const tags: RawTag[] = Array.isArray(rawSpan.tags) ? rawSpan.tags : [];
  return {
    get: key => tags.find(t => t.key === key)?.value,
    set: (key, value) => {
      const tag = tags.find(t => t.key === key);
      if (tag) tag.value = value;
    },
  };
}

/** Patch every representation of the message the span carries, so the file stays consistent
 * whichever one a reader prefers. */
function patchSpan(parsedFile: ParsedTraceFile, spanId: string, field: Field, newText: string): void {
  const locRef = parsedFile.spanIndex.get(spanId);
  if (!locRef) return;
  const store = parsedFile.format === 'jaeger' ? jaegerStore(locRef.raw) : otlpStore(locRef.raw);

  for (const key of MESSAGE_KEYS[field]) {
    const value = store.get(key);
    if (typeof value !== 'string') continue;
    const patched = patchJson(value, data => patchGenAIJsonValue(data, field, newText));
    if (patched !== null) store.set(key, patched);
  }

  const adkKey = field === 'user' ? ADK_REQUEST_KEY : ADK_RESPONSE_KEY;
  const adkValue = store.get(adkKey);
  if (typeof adkValue === 'string') {
    const patched = patchJson(adkValue, data => patchAdkJsonValue(data, field, newText));
    if (patched !== null) store.set(adkKey, patched);
  }

  patchIndexed(store, field, newText);
}

/** OpenLLMetry style ``gen_ai.prompt.N.role`` / ``.content``: the last message with a matching role. */
function patchIndexed(store: AttrStore, field: Field, newText: string): void {
  const prefix = INDEXED_PREFIX[field];
  const roles = field === 'user' ? USER_ROLES : ASSISTANT_ROLES;
  let target: number | null = null;
  for (let n = 0; store.get(`${prefix}${n}.role`) !== undefined || store.get(`${prefix}${n}.content`) !== undefined; n++) {
    const role = store.get(`${prefix}${n}.role`);
    const roleMatches = role === undefined || (typeof role === 'string' && roles.includes(role));
    if (roleMatches && typeof store.get(`${prefix}${n}.content`) === 'string') {
      target = n;
    }
  }
  if (target !== null) store.set(`${prefix}${target}.content`, newText);
}

function patchJson(jsonStr: string, patch: (data: any) => string): string | null {
  try {
    return patch(JSON.parse(jsonStr));
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

function hasText(msg: any): boolean {
  if (typeof msg.content === 'string' && msg.content) return true;
  if (Array.isArray(msg.content) && msg.content.some((item: any) => typeof item === 'object' && item.text)) return true;
  return Array.isArray(msg.parts) && msg.parts.some((p: any) => typeof p === 'object' && p.type === 'text');
}

function patchGenAIJsonValue(data: any, field: 'user' | 'response', newText: string): string {
  if (!Array.isArray(data)) return JSON.stringify(data);
  // A tool request has no text to replace; leave it rather than invent an answer.
  if (field === 'response' && !data.some((msg: any) => ASSISTANT_ROLES.includes(msg.role) && hasText(msg))) {
    return JSON.stringify(data);
  }

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
  if (parsedFile.format === 'otlp-json') {
    return JSON.stringify(parsedFile.rawData);
  }
  return JSON.stringify(parsedFile.rawData, null, 2);
}
