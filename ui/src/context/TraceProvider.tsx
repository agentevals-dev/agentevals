import React, { useState, useMemo } from 'react';
import type { ReactNode } from 'react';
import { TraceContext } from './TraceContext';
import type { TraceState } from './TraceContext';
import type { ViewType, EvalSet, EvalSetMetadata, EvalCase } from '../lib/types';
import { evaluateTracesStreaming } from '../api/client';

interface TraceProviderProps {
  children: ReactNode;
}

export const TraceProvider: React.FC<TraceProviderProps> = ({ children }) => {
  const [state, setState] = useState<TraceState>({
    traceFiles: [],
    evalSetFile: null,
    selectedMetrics: ['tool_trajectory_avg_score'],
    judgeModel: 'gemini-2.5-flash',
    threshold: 0.8,
    isEvaluating: false,
    progressMessage: '',
    results: [],
    errors: [],
    currentView: 'welcome',
    selectedTraceId: null,
    selectedSpanId: null,
    builderEvalSet: null,
    builderSelectedTraceIds: [],
  });

  const actions = useMemo(
    () => ({
      setTraceFiles: (files: File[]) =>
        setState((prev) => ({ ...prev, traceFiles: files })),

      setEvalSet: (file: File | null) =>
        setState((prev) => ({ ...prev, evalSetFile: file })),

      toggleMetric: (metric: string) =>
        setState((prev) => ({
          ...prev,
          selectedMetrics: prev.selectedMetrics.includes(metric)
            ? prev.selectedMetrics.filter((m) => m !== metric)
            : [...prev.selectedMetrics, metric],
        })),

      setJudgeModel: (model: string) =>
        setState((prev) => ({ ...prev, judgeModel: model })),

      setThreshold: (threshold: number) =>
        setState((prev) => ({ ...prev, threshold })),

      runEvaluation: async () => {
        setState((prev) => ({ ...prev, isEvaluating: true, progressMessage: '', errors: [] }));

        try {
          await evaluateTracesStreaming(
            state.traceFiles,
            state.evalSetFile,
            {
              metrics: state.selectedMetrics,
              judgeModel: state.judgeModel,
              threshold: state.threshold,
            },
            (message) => {
              setState((prev) => ({ ...prev, progressMessage: message }));
            },
            (result) => {
              setState((prev) => ({
                ...prev,
                isEvaluating: false,
                progressMessage: '',
                results: result.traceResults,
                errors: result.errors,
                currentView: 'dashboard',
              }));
            },
            (error) => {
              setState((prev) => ({
                ...prev,
                isEvaluating: false,
                progressMessage: '',
                errors: [error.message],
              }));
            }
          );
        } catch (error) {
          setState((prev) => ({
            ...prev,
            isEvaluating: false,
            progressMessage: '',
            errors: [error instanceof Error ? error.message : 'Unknown error occurred'],
          }));
        }
      },

      setCurrentView: (view: ViewType) =>
        setState((prev) => ({ ...prev, currentView: view })),

      selectTrace: (traceId: string | null) =>
        setState((prev) => ({ ...prev, selectedTraceId: traceId })),

      selectSpan: (spanId: string | null) =>
        setState((prev) => ({ ...prev, selectedSpanId: spanId })),

      clearResults: () =>
        setState((prev) => ({
          ...prev,
          results: [],
          errors: [],
          currentView: 'welcome',
        })),

      // Builder actions
      setBuilderEvalSet: (evalSet: EvalSet | null) =>
        setState((prev) => ({ ...prev, builderEvalSet: evalSet })),

      updateEvalSetMetadata: (metadata: Partial<EvalSetMetadata>) =>
        setState((prev) => ({
          ...prev,
          builderEvalSet: prev.builderEvalSet
            ? { ...prev.builderEvalSet, ...metadata }
            : null,
        })),

      updateEvalCase: (caseIndex: number, evalCase: EvalCase) =>
        setState((prev) => {
          if (!prev.builderEvalSet) return prev;
          const newCases = [...prev.builderEvalSet.eval_cases];
          newCases[caseIndex] = evalCase;
          return {
            ...prev,
            builderEvalSet: { ...prev.builderEvalSet, eval_cases: newCases },
          };
        }),

      addEvalCase: (evalCase: EvalCase) =>
        setState((prev) => {
          if (!prev.builderEvalSet) return prev;
          return {
            ...prev,
            builderEvalSet: {
              ...prev.builderEvalSet,
              eval_cases: [...prev.builderEvalSet.eval_cases, evalCase],
            },
          };
        }),

      removeEvalCase: (caseIndex: number) =>
        setState((prev) => {
          if (!prev.builderEvalSet) return prev;
          const newCases = prev.builderEvalSet.eval_cases.filter(
            (_, idx) => idx !== caseIndex
          );
          return {
            ...prev,
            builderEvalSet: { ...prev.builderEvalSet, eval_cases: newCases },
          };
        }),
    }),
    [state.traceFiles, state.evalSetFile, state.selectedMetrics, state.judgeModel, state.threshold]
  );

  return (
    <TraceContext.Provider value={{ state, actions }}>
      {children}
    </TraceContext.Provider>
  );
};
