import type { Trace, Span, Invocation, Content, ToolCall, ToolResponse, IntermediateData } from './types';
import { safeJsonParse } from './utils';

export const ADK_SCOPE = 'gcp.vertex.agent';

export const USER_ROLES = ['user', 'human'];
export const ASSISTANT_ROLES = ['assistant', 'model', 'ai'];

export function getInputMessagesAttr(span: Span): string | undefined {
  return span.tags['gen_ai.input.messages']
      || span.tags['gen_ai.prompt']
      || span.tags['gen_ai.request.messages'];
}

export function getOutputMessagesAttr(span: Span): string | undefined {
  return span.tags['gen_ai.output.messages']
      || span.tags['gen_ai.completion']
      || span.tags['gen_ai.response.messages'];
}

interface ConversionResult {
  invocations: Invocation[];
  warnings: string[];
}

export function detectTraceFormat(trace: Trace): 'adk' | 'genai' {
  const check = (spans: Span[]): 'adk' | 'genai' | null => {
    let hasGenai = false;
    for (const span of spans) {
      if (span.tags['otel.scope.name'] === ADK_SCOPE) {
        return 'adk';
      }
      if (!hasGenai && (span.tags['gen_ai.request.model'] || span.tags['gen_ai.system'])) {
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

export function convertTracesToInvocations(traces: Trace[]): Map<string, ConversionResult> {
  const results = new Map<string, ConversionResult>();

  for (const trace of traces) {
    const format = detectTraceFormat(trace);
    console.log(`Converting trace ${trace.traceId} (format: ${format}):`);
    console.log(`  Total spans: ${trace.allSpans.length}`);

    if (format === 'genai') {
      results.set(trace.traceId, convertGenAITrace(trace));
    } else {
      results.set(trace.traceId, convertADKTrace(trace));
    }
  }

  return results;
}

function convertADKTrace(trace: Trace): ConversionResult {
  const warnings: string[] = [];
  const invocations: Invocation[] = [];

  trace.allSpans.forEach((span, idx) => {
    console.log(`  Span ${idx}: ${span.operationName}, scope: ${span.tags['otel.scope.name']}`);
  });

  const agentSpans = trace.allSpans.filter(
    (span) =>
      span.operationName.includes('invoke_agent') &&
      span.tags['otel.scope.name'] === ADK_SCOPE
  );

  console.log(`  Found ${agentSpans.length} invoke_agent spans with ADK scope`);

  for (const agentSpan of agentSpans) {
    try {
      const invocation = convertAgentSpanToInvocation(agentSpan);
      if (invocation) {
        invocations.push(invocation);
        console.log(`  Created invocation: ${invocation.invocationId}`);
      } else {
        console.log(`  convertAgentSpanToInvocation returned null for span ${agentSpan.spanId}`);
      }
    } catch (error) {
      const errorMsg = `Failed to convert span ${agentSpan.spanId}: ${error instanceof Error ? error.message : 'Unknown error'}`;
      warnings.push(errorMsg);
      console.error(`  ${errorMsg}`);
    }
  }

  console.log(`  Final invocations count: ${invocations.length}`);
  return { invocations, warnings };
}

/**
 * Recursively find child spans by operation name prefix
 * (replicates Python's _find_children_by_op)
 */
export function findChildrenByOperation(root: Span, opPrefix: string): Span[] {
  const results: Span[] = [];
  walkSpanTree(root, opPrefix, results);
  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}

/**
 * Recursive walker for span tree
 * (replicates Python's _walk)
 */
function walkSpanTree(span: Span, opPrefix: string, acc: Span[]): void {
  for (const child of span.children) {
    if (child.operationName.startsWith(opPrefix)) {
      acc.push(child);
    }
    walkSpanTree(child, opPrefix, acc);
  }
}

/**
 * Convert single agent span to Invocation
 */
function convertAgentSpanToInvocation(agentSpan: Span): Invocation | null {
  console.log(`    Converting agent span ${agentSpan.spanId}:`);
  console.log(`      Children count: ${agentSpan.children.length}`);

  // Recursively find child spans by operation name (like Python's _find_children_by_op)
  const llmSpans = findChildrenByOperation(agentSpan, 'call_llm');
  const toolSpans = findChildrenByOperation(agentSpan, 'execute_tool');

  console.log(`      LLM spans: ${llmSpans.length}, Tool spans: ${toolSpans.length}`);

  if (llmSpans.length === 0) {
    console.log(`      Skipping: No LLM spans found`);
    return null; // No LLM calls, skip
  }

  // Extract user content from first LLM span
  const userContent = extractUserContent(llmSpans[0]);
  if (!userContent) {
    console.log(`      Skipping: Failed to extract user content`);
    return null;
  }

  // Extract final response from last LLM span
  const finalResponse = extractFinalResponse(llmSpans[llmSpans.length - 1]);
  if (!finalResponse) {
    console.log(`      Skipping: Failed to extract final response`);
    return null;
  }

  // Extract tool trajectory
  const { toolUses, toolResponses } = extractToolTrajectory(toolSpans, llmSpans);

  return {
    invocationId: agentSpan.spanId,
    userContent,
    finalResponse,
    intermediateData: {
      toolUses,
      toolResponses,
    },
    creationTimestamp: agentSpan.startTime,
  };
}

/**
 * Extract user content from LLM request
 */
function extractUserContent(llmSpan: Span): Content | null {
  const requestJson = llmSpan.tags['gcp.vertex.agent.llm_request'];
  if (!requestJson) return null;

  const request = safeJsonParse<any>(requestJson, null);
  if (!request || !request.contents) return null;

  // Find last user message with text parts (skip function_response parts)
  for (let i = request.contents.length - 1; i >= 0; i--) {
    const content = request.contents[i];
    if (content.role === 'user') {
      const textParts = content.parts?.filter((p: any) => p.text !== undefined);
      if (textParts && textParts.length > 0) {
        return {
          role: 'user',
          parts: textParts,
        };
      }
    }
  }

  return null;
}

/**
 * Extract final response from LLM response
 */
function extractFinalResponse(llmSpan: Span): Content | null {
  const responseJson = llmSpan.tags['gcp.vertex.agent.llm_response'];
  if (!responseJson) return null;

  const response = safeJsonParse<any>(responseJson, null);
  if (!response || !response.content) return null;

  // Extract text parts only (skip function_call parts for final response)
  const textParts = response.content.parts?.filter((p: any) => p.text !== undefined) || [];

  return {
    role: 'model',
    parts: textParts,
  };
}

/**
 * Extract tool trajectory from execute_tool spans or LLM function calls
 */
function extractToolTrajectory(
  toolSpans: Span[],
  llmSpans: Span[]
): { toolUses: ToolCall[]; toolResponses: ToolResponse[] } {
  const toolUses: ToolCall[] = [];
  const toolResponses: ToolResponse[] = [];

  // Prefer execute_tool spans if available
  if (toolSpans.length > 0) {
    for (const toolSpan of toolSpans) {
      const toolName = toolSpan.tags['gen_ai.tool.name'];
      const toolCallId = toolSpan.tags['gen_ai.tool.call.id'];
      const argsJson = toolSpan.tags['gcp.vertex.agent.tool_call_args'];
      const responseJson = toolSpan.tags['gcp.vertex.agent.tool_response'];

      if (toolName) {
        const args = safeJsonParse<Record<string, any>>(argsJson || '{}', {});
        toolUses.push({
          name: toolName,
          args,
          id: toolCallId,
        });

        if (responseJson) {
          const response = safeJsonParse<Record<string, any>>(responseJson, {});
          toolResponses.push({
            name: toolName,
            response,
            id: toolCallId,
          });
        }
      }
    }
  } else {
    // Fallback: extract from LLM function calls
    for (const llmSpan of llmSpans) {
      const responseJson = llmSpan.tags['gcp.vertex.agent.llm_response'];
      if (!responseJson) continue;

      const response = safeJsonParse<any>(responseJson, null);
      if (!response || !response.content || !response.content.parts) continue;

      const functionCalls = response.content.parts.filter((p: any) => p.functionCall);
      for (const part of functionCalls) {
        if (part.functionCall) {
          toolUses.push({
            name: part.functionCall.name,
            args: part.functionCall.args || {},
            id: part.functionCall.id,
          });
        }
      }
    }
  }

  return { toolUses, toolResponses };
}

function isBroadcastEnriched(span: Span): boolean {
  const messagesAttr = getInputMessagesAttr(span);
  if (!messagesAttr) return false;

  const messages = safeJsonParse<any[]>(messagesAttr, []);
  if (!Array.isArray(messages)) return false;

  const userCount = messages.filter(
    (m: any) => typeof m === 'object' && m !== null && USER_ROLES.includes(m.role)
  ).length;
  return userCount > 1;
}

function trimCumulativeOutput(span: Span, outputMessages: any[]): any[] {
  const inputAttr = getInputMessagesAttr(span);
  if (!inputAttr) return outputMessages;

  const inputMessages = safeJsonParse<any[]>(inputAttr, []);
  if (!Array.isArray(inputMessages)) return outputMessages;

  const userCount = inputMessages.filter(
    (m: any) => typeof m === 'object' && m !== null && USER_ROLES.includes(m.role)
  ).length;
  if (userCount <= 1) return outputMessages;

  const previousTurns = userCount - 1;
  let textResponsesSeen = 0;

  for (let i = 0; i < outputMessages.length; i++) {
    const msg = outputMessages[i];
    if (typeof msg !== 'object' || !msg || !ASSISTANT_ROLES.includes(msg.role)) continue;
    const content = extractTextFromGenAIMessage(msg);
    if (content) {
      textResponsesSeen++;
      if (textResponsesSeen >= previousTurns) {
        return outputMessages.slice(i + 1);
      }
    }
  }

  return outputMessages;
}

function isGenAIInvocationSpan(span: Span): boolean {
  const opLower = span.operationName.toLowerCase();
  return ['agent', 'chain', 'executor', 'workflow'].some(kw => opLower.includes(kw));
}

export function extractTextFromGenAIMessage(msg: any): string {
  if (typeof msg.content === 'string' && msg.content) {
    return msg.content;
  }
  if (Array.isArray(msg.content)) {
    const parts = msg.content
      .filter((item: any) => typeof item === 'object' && item.text)
      .map((item: any) => item.text as string);
    if (parts.length > 0) return parts.join(' ');
  }
  // Parts-based format (OTel GenAI semconv v1.36.0+)
  if (Array.isArray(msg.parts)) {
    const parts = msg.parts
      .filter((p: any) => typeof p === 'object' && p.type === 'text')
      .map((p: any) => (p.content || p.text || '') as string)
      .filter(Boolean);
    if (parts.length > 0) return parts.join(' ');
  }
  return '';
}

export function extractToolCallsFromGenAIMessage(msg: any): ToolCall[] {
  const result: ToolCall[] = [];
  if (Array.isArray(msg.tool_calls)) {
    for (const tc of msg.tool_calls) {
      if (tc.type === 'function' && tc.function) {
        const args = safeJsonParse<Record<string, any>>(tc.function.arguments || '{}', {});
        result.push({ name: tc.function.name, args, id: tc.id });
      }
    }
  }
  // Parts-based format (OTel GenAI semconv v1.36.0+)
  if (result.length === 0 && Array.isArray(msg.parts)) {
    for (const part of msg.parts) {
      if (typeof part === 'object' && part.type === 'tool_call') {
        const args = typeof part.arguments === 'string'
          ? safeJsonParse<Record<string, any>>(part.arguments, {})
          : (part.arguments || {});
        result.push({ name: part.name, args, id: part.id });
      }
    }
  }
  return result;
}

function convertGenAITrace(trace: Trace): ConversionResult {
  const warnings: string[] = [];
  const invocations: Invocation[] = [];

  const llmSpans = trace.allSpans.filter(span =>
    span.tags['gen_ai.request.model'] || span.tags['gen_ai.system']
  );

  console.log(`  Found ${llmSpans.length} GenAI LLM spans`);

  if (llmSpans.length === 0) {
    console.log(`  No GenAI LLM spans found, treating trace as single invocation`);
    return { invocations: [], warnings };
  }

  const llmRootSpans = trace.rootSpans.filter(span =>
    span.tags['gen_ai.request.model'] || span.tags['gen_ai.system']
  );

  // Multi-turn extraction applies only when message content is broadcast-enriched
  // (every span has the full history). Per-span enriched traces (OTLP path) have
  // each span with only its own messages — each should be a separate invocation.
  if (llmSpans.length > 1 && !llmRootSpans.some(isGenAIInvocationSpan) && isBroadcastEnriched(llmSpans[0])) {
    console.log(`  Multi-turn conversation detected (${llmSpans.length} LLM spans, broadcast-enriched)`);
    const multiTurnInvocations = convertGenAIMultiTurn(llmSpans, trace);
    invocations.push(...multiTurnInvocations);
  } else {
    const rootSpansToConvert = llmRootSpans.length > 0
      ? llmRootSpans
      : trace.rootSpans.slice(0, 1);
    for (const rootSpan of rootSpansToConvert) {
      try {
        const invocation = convertGenAIRootSpan(rootSpan, trace);
        if (invocation) {
          invocations.push(invocation);
        }
      } catch (error) {
        warnings.push(`Failed to convert root span: ${error instanceof Error ? error.message : 'Unknown error'}`);
      }
    }
  }

  const deduplicated = deduplicateInvocations(invocations);
  console.log(`  Final invocations count: ${deduplicated.length} (before dedup: ${invocations.length})`);
  return { invocations: deduplicated, warnings };
}

function deduplicateInvocations(invocations: Invocation[]): Invocation[] {
  if (invocations.length <= 1) return invocations;

  const getUserText = (inv: Invocation): string =>
    inv.userContent.parts
      .filter(p => p.text)
      .map(p => p.text)
      .join(' ');

  const seen = new Map<string, number>();
  const alwaysKeep = new Set<number>();
  invocations.forEach((inv, i) => {
    const text = getUserText(inv);
    if (!text.trim()) {
      alwaysKeep.add(i);
    } else {
      seen.set(text, i);
    }
  });

  if (seen.size + alwaysKeep.size === invocations.length) return invocations;

  const keep = new Set([...alwaysKeep, ...seen.values()]);
  return invocations.filter((_, i) => keep.has(i));
}

function convertGenAIMultiTurn(llmSpans: Span[], trace: Trace): Invocation[] {
  const invocations: Invocation[] = [];

  // Get messages from the first LLM span (should have full conversation history)
  const firstLlmSpan = llmSpans[0];
  const inputMessagesAttr = getInputMessagesAttr(firstLlmSpan) || '[]';
  const outputMessagesAttr = getOutputMessagesAttr(firstLlmSpan) || '[]';

  const allInputMessages = safeJsonParse<any[]>(inputMessagesAttr, []);
  const allOutputMessages = safeJsonParse<any[]>(outputMessagesAttr, []);

  if (!Array.isArray(allInputMessages) || !Array.isArray(allOutputMessages)) {
    console.warn('  Input or output messages are not arrays, falling back to single invocation');
    const invocation = convertGenAIRootSpan(firstLlmSpan, trace);
    return invocation ? [invocation] : [];
  }

  const userMessages = allInputMessages.filter(msg =>
    USER_ROLES.includes(msg.role)
  );
  const assistantMessages = allOutputMessages.filter(msg =>
    ASSISTANT_ROLES.includes(msg.role)
  );

  console.log(`  Multi-turn: ${userMessages.length} user, ${assistantMessages.length} assistant messages`);

  let assistantIdx = 0;

  for (let userIdx = 0; userIdx < userMessages.length; userIdx++) {
    const userMsg = userMessages[userIdx];
    const userText = extractTextFromGenAIMessage(userMsg);

    if (!userText) {
      continue;
    }

    const userContent: Content = {
      role: 'user',
      parts: [{ text: userText }]
    };

    const toolUses: ToolCall[] = [];
    let finalResponseText = '';

    while (assistantIdx < assistantMessages.length) {
      const assistantMsg = assistantMessages[assistantIdx];

      for (const tc of extractToolCallsFromGenAIMessage(assistantMsg)) {
        toolUses.push(tc);
      }

      const content = extractTextFromGenAIMessage(assistantMsg);
      if (content) {
        finalResponseText = content;
        assistantIdx++;
        break;
      }

      assistantIdx++;
    }

    const finalResponse: Content = {
      role: 'model',
      parts: [{ text: finalResponseText }]
    };

    const intermediateData: IntermediateData = {
      toolUses,
      toolResponses: []
    };

    invocations.push({
      invocationId: `genai-turn-${userIdx + 1}-${firstLlmSpan.spanId.substring(0, 8)}`,
      userContent,
      finalResponse,
      intermediateData,
      creationTimestamp: firstLlmSpan.startTime
    });
  }

  return invocations;
}

function convertGenAIRootSpan(rootSpan: Span, _trace: Trace): Invocation | null {
  const llmSpans = findDescendantLLMSpans(rootSpan);
  const toolSpans = findDescendantToolSpans(rootSpan);

  console.log(`    Converting GenAI root span ${rootSpan.spanId}:`);
  console.log(`      LLM spans: ${llmSpans.length}, Tool spans: ${toolSpans.length}`);

  if (llmSpans.length === 0) {
    console.log(`      Skipping: No LLM spans found`);
    return null;
  }

  const userContent = extractGenAIUserContent(llmSpans[0]);
  if (!userContent) {
    console.log(`      Skipping: Failed to extract user content`);
    return null;
  }

  const finalResponse = extractGenAIFinalResponse(llmSpans[llmSpans.length - 1]);
  if (!finalResponse) {
    console.log(`      Skipping: Failed to extract final response`);
    return null;
  }

  // Extract tool calls from both tool spans and LLM output messages
  const { toolUses, toolResponses } = extractGenAIToolTrajectory(toolSpans, llmSpans);

  return {
    invocationId: rootSpan.spanId,
    userContent,
    finalResponse,
    intermediateData: {
      toolUses,
      toolResponses,
    },
    creationTimestamp: rootSpan.startTime,
  };
}

export function findDescendantLLMSpans(root: Span): Span[] {
  const results: Span[] = [];
  const queue = [root];

  while (queue.length > 0) {
    const span = queue.shift()!;
    if (span.tags['gen_ai.request.model'] || span.tags['gen_ai.system']) {
      results.push(span);
    }
    queue.push(...span.children);
  }

  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}

function findDescendantToolSpans(root: Span): Span[] {
  const results: Span[] = [];
  const queue = [root];

  while (queue.length > 0) {
    const span = queue.shift()!;
    if (span.tags['gen_ai.tool.name']) {
      results.push(span);
    }
    queue.push(...span.children);
  }

  results.sort((a, b) => a.startTime - b.startTime);
  return results;
}

function extractGenAIUserContent(llmSpan: Span): Content | null {
  const messagesAttr = getInputMessagesAttr(llmSpan);
  if (!messagesAttr) return null;

  const messages = safeJsonParse<any[] | null>(messagesAttr, null);
  if (!messages || !Array.isArray(messages)) return null;

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (USER_ROLES.includes(msg.role)) {
      const text = extractTextFromGenAIMessage(msg);
      if (text) {
        return { role: 'user', parts: [{ text }] };
      }
    }
  }

  return null;
}

function extractGenAIFinalResponse(llmSpan: Span): Content | null {
  const completionAttr = getOutputMessagesAttr(llmSpan);
  if (!completionAttr) return null;

  const messages = safeJsonParse<any[] | null>(completionAttr, null);
  if (!messages || !Array.isArray(messages)) return null;

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (ASSISTANT_ROLES.includes(msg.role)) {
      const text = extractTextFromGenAIMessage(msg);
      return { role: 'model', parts: [{ text }] };
    }
  }

  return null;
}

function extractGenAIToolTrajectory(toolSpans: Span[], llmSpans: Span[]): { toolUses: ToolCall[]; toolResponses: ToolResponse[] } {
  const toolCallsById = new Map<string, ToolCall>();
  const toolCallsNoId: ToolCall[] = [];
  const toolResponses: ToolResponse[] = [];

  for (const toolSpan of toolSpans) {
    const toolName = toolSpan.tags['gen_ai.tool.name'];
    const toolCallId = toolSpan.tags['gen_ai.tool.call.id'];
    const argsAttr = toolSpan.tags['gen_ai.tool.call.arguments'] || toolSpan.tags['gen_ai.tool.arguments'];
    const resultAttr = toolSpan.tags['gen_ai.tool.call.result'] || toolSpan.tags['gen_ai.tool.result'];

    if (toolName) {
      let args = safeJsonParse<Record<string, any>>(argsAttr || '{}', {});

      // Fallback: extract args from gen_ai.input.messages when tool span
      // doesn't have gen_ai.tool.call.arguments (e.g. Strands)
      if (Object.keys(args).length === 0) {
        const inputMsgsAttr = toolSpan.tags['gen_ai.input.messages'];
        if (inputMsgsAttr) {
          const inputMsgs = safeJsonParse<any[]>(inputMsgsAttr, []);
          if (Array.isArray(inputMsgs)) {
            for (const msg of inputMsgs) {
              if (typeof msg !== 'object') continue;
              for (const tc of extractToolCallsFromGenAIMessage(msg)) {
                if (tc.name === toolName && Object.keys(tc.args).length > 0) {
                  args = tc.args;
                  break;
                }
              }
              if (Object.keys(args).length > 0) break;
            }
          }
        }
      }

      const tc: ToolCall = { name: toolName, args, id: toolCallId };
      if (toolCallId) {
        toolCallsById.set(toolCallId, tc);
      } else {
        toolCallsNoId.push(tc);
      }

      if (resultAttr) {
        const response = safeJsonParse<Record<string, any>>(resultAttr, {});
        toolResponses.push({
          name: toolName,
          response,
          id: toolCallId,
        });
      }
    }
  }

  for (const llmSpan of llmSpans) {
    const completionAttr = getOutputMessagesAttr(llmSpan);
    if (!completionAttr) continue;

    let messages = safeJsonParse<any[] | null>(completionAttr, null);
    if (!messages || !Array.isArray(messages)) continue;

    messages = trimCumulativeOutput(llmSpan, messages);

    for (const msg of messages) {
      if (ASSISTANT_ROLES.includes(msg.role)) {
        for (const tc of extractToolCallsFromGenAIMessage(msg)) {
          if (tc.id && toolCallsById.has(tc.id)) {
            // Prefer LLM message version if it has richer args
            const existing = toolCallsById.get(tc.id)!;
            if (Object.keys(tc.args).length > 0 && Object.keys(existing.args).length === 0) {
              toolCallsById.set(tc.id, tc);
            }
          } else if (tc.id) {
            toolCallsById.set(tc.id, tc);
          } else {
            toolCallsNoId.push(tc);
          }
        }
      }
    }
  }

  const toolUses = [...toolCallsById.values(), ...toolCallsNoId];
  return { toolUses, toolResponses };
}

/**
 * Get invocations for a specific trace
 */
export function getInvocationsForTrace(
  trace: Trace,
  conversionResults: Map<string, ConversionResult>
): ConversionResult | undefined {
  return conversionResults.get(trace.traceId);
}
