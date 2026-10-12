import { fromJson, toJson } from '@bufbuild/protobuf';
import { timestampMs, ValueSchema } from '@bufbuild/protobuf/wkt';
import type { Value } from '@bufbuild/protobuf/wkt';
import { WatchSessionsResponseSchema } from '../generated/agentevals/api/v1alpha1/sessions_pb';
import type { WatchSessionsResponse } from '../generated/agentevals/api/v1alpha1/sessions_pb';
import { SessionState } from '../generated/agentevals/api/v1alpha1/views_pb';
import type { Session, TurnView, Usage } from '../generated/agentevals/api/v1alpha1/views_pb';
import { Status_StatusCode } from '../generated/opentelemetry/proto/trace/v1/trace_pb';

export type { Session, TurnView } from '../generated/agentevals/api/v1alpha1/views_pb';

/** One frame of ``/stream/ui-updates``: a protojson ``WatchSessionsResponse``. */
export function decodeFrame(data: string): WatchSessionsResponse {
  return fromJson(WatchSessionsResponseSchema, JSON.parse(data), { ignoreUnknownFields: true });
}

export function sessionId(session: Session): string {
  return session.summary?.id ?? '';
}

export function isComplete(session: Session): boolean {
  return session.summary?.state === SessionState.COMPLETE;
}

export function startedAtMs(session: Session): number {
  const startedAt = session.summary?.startedAt;
  return startedAt ? timestampMs(startedAt) : 0;
}

export function startedAtIso(session: Session): string | undefined {
  const ms = startedAtMs(session);
  return ms ? new Date(ms).toISOString() : undefined;
}

/** The session's earliest trace, the id evaluation results are keyed by. */
export function primaryTraceId(session: Session): string {
  return session.summary?.traceIds[0] ?? '';
}

export function isFailed(turn: TurnView): boolean {
  return turn.status === Status_StatusCode.ERROR;
}

/** Token counts as numbers; ``undefined`` where the telemetry recorded none. */
export function tokens(usage: Usage | undefined) {
  const n = (v: bigint | undefined) => (v === undefined ? undefined : Number(v));
  return {
    input: n(usage?.inputTokens),
    output: n(usage?.outputTokens),
    cacheRead: n(usage?.cacheReadInputTokens),
    cacheWrite: n(usage?.cacheWriteInputTokens),
  };
}

export function totalTokens(usage: Usage | undefined): number {
  const t = tokens(usage);
  return (t.input ?? 0) + (t.output ?? 0);
}

/** A tool argument or result as plain JSON, or ``undefined`` when not recorded. */
export function jsonValue(value: Value | undefined): unknown {
  return value === undefined ? undefined : toJson(ValueSchema, value);
}

/** Every gap reason of the session and its turns, once each. */
export function gapReasons(session: Session): string[] {
  const gaps = [...(session.summary?.gaps ?? []), ...session.turns.flatMap(t => t.gaps)];
  return Array.from(new Set(gaps.map(g => g.reason)));
}

export function firstProvider(session: Session): string | undefined {
  for (const turn of session.turns) {
    const provider = turn.modelCalls.find(c => c.provider)?.provider;
    if (provider) return provider;
  }
  return undefined;
}
