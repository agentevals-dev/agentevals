import { createContext, useContext } from 'react';
import type { TraceResult, ViewType, EvalSet, EvalSetMetadata, EvalCase } from '../lib/types';

export interface TraceState {
  // Upload state
  traceFiles: File[];
  evalSetFile: File | null;
  selectedMetrics: string[];
  judgeModel: string;
  threshold: number;

  // Evaluation state
  isEvaluating: boolean;
  results: TraceResult[];
  errors: string[];

  // UI state
  currentView: ViewType;
  selectedTraceId: string | null;
  selectedSpanId: string | null;

  // Builder state
  builderEvalSet: EvalSet | null;
  builderSelectedTraceIds: string[];
}

export interface TraceContextType {
  state: TraceState;
  actions: {
    setTraceFiles: (files: File[]) => void;
    setEvalSet: (file: File | null) => void;
    toggleMetric: (metric: string) => void;
    setJudgeModel: (model: string) => void;
    setThreshold: (threshold: number) => void;
    runEvaluation: () => Promise<void>;
    setCurrentView: (view: ViewType) => void;
    selectTrace: (traceId: string | null) => void;
    selectSpan: (spanId: string | null) => void;
    clearResults: () => void;

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
