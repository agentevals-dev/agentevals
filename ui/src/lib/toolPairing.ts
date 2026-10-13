interface Named {
  name: string;
  id?: string | null;
}

/**
 * The response for each call, or undefined. Ids decide when both sides have one; otherwise the
 * next unused response with the same name, so repeated calls to one tool keep their own results.
 */
export function pairToolResponses<C extends Named, R extends Named>(calls: C[], responses: R[]): Array<R | undefined> {
  const used = new Set<number>();
  const take = (match: (r: R) => boolean) => {
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
