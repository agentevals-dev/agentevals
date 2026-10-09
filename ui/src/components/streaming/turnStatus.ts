import type { StreamingInvocation } from '../../lib/types';

type ToolCall = StreamingInvocation['toolCalls'][number];
type ToolResponse = NonNullable<StreamingInvocation['toolResponses']>[number];

export function isFailedTurn(inv: Pick<StreamingInvocation, 'status' | 'errorType'>): boolean {
  return inv.status === 'error' || !!inv.errorType;
}

/** True only when the server reported every turn as captured without message content. */
export function lacksContent(invocations: StreamingInvocation[] | undefined): boolean {
  return !!invocations?.length && invocations.every(inv => inv.contentCaptured === false);
}

/**
 * The response for each call, or undefined. Ids decide when both sides have one; otherwise the
 * next unused response with the same name, so repeated calls to one tool keep their own results.
 */
export function pairToolResponses(calls: ToolCall[], responses: ToolResponse[]): Array<ToolResponse | undefined> {
  const used = new Set<number>();
  const take = (match: (r: ToolResponse) => boolean) => {
    const i = responses.findIndex((r, j) => !used.has(j) && match(r));
    if (i < 0) return undefined;
    used.add(i);
    return responses[i];
  };
  return calls.map(tc =>
    (tc.id ? take(r => r.id === tc.id) : undefined)
      ?? take(r => r.name === tc.name && !(tc.id && r.id)),
  );
}
