import { createContext, useContext } from 'react';
import type { TraceResult, ViewType, EvalSet, EvalSetMetadata, EvalCase, TraceTableRow, LiveSession, AnnotationQueue, Annotation } from '../lib/types';
import type { TraceMetadata } from '../lib/trace-metadata';

export interface ApiKeyStatus {
  google: boolean;
  anthropic: boolean;
  openai: boolean;
}

export interface TraceState {
  // Upload state
  traceFiles: File[];
  evalSetFile: File | null;
  selectedMetrics: string[];
  judgeModel: string;
  threshold: number;
  trajectoryMatchType: string;
  traceMetadata: Map<string, TraceMetadata>;
  isLoadingMetadata: boolean;
  apiKeyStatus: ApiKeyStatus | null;

  // Evaluation state
  isEvaluating: boolean;
  progressMessage: string;
  results: TraceResult[];
  errors: string[];
  tableRows: Map<string, TraceTableRow>;
  expectedTraceCount: number;

  // UI state
  currentView: ViewType;
  evaluationOrigin: ViewType | null;
  selectedTraceId: string | null;
  version: string | null;

  // Streaming state
  streamingSessions: Map<string, LiveSession>;

  // Annotation queue state
  annotationQueues: AnnotationQueue[];
  currentAnnotationQueueId: string | null;
  pendingAnnotations: Map<string, Annotation>;

  // Builder state
  builderEvalSet: EvalSet | null;
  builderSelectedTraceIds: string[];
}

export interface TraceContextType {
  state: TraceState;
  actions: {
    setTraceFiles: (files: File[]) => Promise<void>;
    setEvalSet: (file: File | null) => void;
    toggleMetric: (metric: string) => void;
    setJudgeModel: (model: string) => void;
    setThreshold: (threshold: number) => void;
    setTrajectoryMatchType: (matchType: string) => void;
    runEvaluation: () => Promise<void>;
    setCurrentView: (view: ViewType) => void;
    setEvaluationOrigin: (view: ViewType | null) => void;
    setStreamingSessions: (updater: (prev: Map<string, LiveSession>) => Map<string, LiveSession>) => void;
    removeSession: (sessionId: string) => void;
    clearAllSessions: () => void;
    selectTrace: (traceId: string | null) => void;
    clearResults: () => void;

    // Annotation queue actions
    createAnnotationQueue: (name: string) => string;
    addToAnnotationQueue: (queueId: string, session: LiveSession) => void;
    annotateQueueItem: (queueId: string, sessionId: string, annotation: Annotation) => void;
    setCurrentAnnotationQueueId: (id: string | null) => void;
    setPendingAnnotations: (annotations: Map<string, Annotation>) => void;

    // Builder actions
    setBuilderEvalSet: (evalSet: EvalSet | null) => void;
    updateEvalSetMetadata: (metadata: Partial<EvalSetMetadata>) => void;
    updateEvalCase: (caseIndex: number, evalCase: EvalCase) => void;
    addEvalCase: (evalCase: EvalCase) => void;
    removeEvalCase: (caseIndex: number) => void;
  };
}

export const TraceContext = createContext<TraceContextType | undefined>(undefined);

export const useTraceContext = () => {
  const context = useContext(TraceContext);
  if (!context) {
    throw new Error('useTraceContext must be used within TraceProvider');
  }
  return context;
};
