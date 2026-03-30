import type { Invocation } from './types';

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
