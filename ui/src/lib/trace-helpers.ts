import type { Trace, Span } from './types';

export const ADK_SCOPE = 'gcp.vertex.agent';

export const USER_ROLES = ['user', 'human'];
export const ASSISTANT_ROLES = ['assistant', 'model', 'ai'];

function isGenAISpan(span: Span): boolean {
  return !!(
    span.tags['gen_ai.request.model'] ||
    span.tags['gen_ai.system'] ||
    span.tags['gen_ai.input.messages'] ||
    span.tags['gen_ai.prompt'] ||
    span.tags['gen_ai.request.messages']
  );
}

export function detectTraceFormat(trace: Trace): 'adk' | 'genai' {
  const check = (spans: Span[]): 'adk' | 'genai' | null => {
    let hasGenai = false;
    for (const span of spans) {
      if (span.tags['otel.scope.name'] === ADK_SCOPE) {
        return 'adk';
      }
      if (!hasGenai && isGenAISpan(span)) {
        hasGenai = true;
      }
    }
    return hasGenai ? 'genai' : null;
  };

  const initial = check(trace.allSpans.slice(0, 10));
  if (initial) return initial;

  if (trace.allSpans.length > 10) {
    const full = check(trace.allSpans);
    if (full) return full;
  }

  return 'adk';
}

export function findChildrenByOperation(root: Span, opPrefix: string): Span[] {
  const results: Span[] = [];
  walkSpanTree(root, opPrefix, results);
  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}

function walkSpanTree(span: Span, opPrefix: string, acc: Span[]): void {
  for (const child of span.children) {
    if (child.operationName.startsWith(opPrefix)) {
      acc.push(child);
    }
    walkSpanTree(child, opPrefix, acc);
  }
}

export function findDescendantLLMSpans(root: Span): Span[] {
  const results: Span[] = [];
  const queue = [root];

  while (queue.length > 0) {
    const span = queue.shift()!;
    if (isGenAISpan(span)) {
      results.push(span);
    }
    queue.push(...span.children);
  }

  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}
