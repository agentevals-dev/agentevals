import React, { useState, useMemo, useEffect } from 'react';
import type { ReactNode } from 'react';
import { TraceContext } from './TraceContext';
import type { TraceState } from './TraceContext';
import type { ViewType, EvalSet, EvalSetMetadata, EvalCase, LiveSession, AnnotationQueue, Annotation } from '../lib/types';
import { evaluateTracesStreaming, getConfig, healthCheck } from '../api/client';
import { extractMetadataFromTraceFile } from '../lib/trace-metadata';

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
    traceMetadata: new Map(),
    isLoadingMetadata: false,
    apiKeyStatus: null,
    isEvaluating: false,
    progressMessage: '',
    results: [],
    errors: [],
    tableRows: new Map(),
    expectedTraceCount: 0,
    currentView: 'welcome',
    evaluationOrigin: null,
    selectedTraceId: null,
    version: null,
    streamingSessions: new Map(),
    annotationQueues: [],
    currentAnnotationQueueId: null,
    pendingAnnotations: new Map(),
    builderEvalSet: null,
    builderSelectedTraceIds: [],
  });

  useEffect(() => {
    getConfig()
      .then((cfg) => setState((prev) => ({ ...prev, apiKeyStatus: cfg.apiKeys })))
      .catch(() => {});
    healthCheck()
      .then((data) => setState((prev) => ({ ...prev, version: data.version ?? null })))
      .catch(() => {});
  }, []);

  const actions = useMemo(
    () => ({
      setTraceFiles: async (files: File[]) => {
        setState((prev) => ({ ...prev, traceFiles: files, isLoadingMetadata: true }));

        const metadataMap = new Map();
        for (const file of files) {
          try {
            const metadataList = await extractMetadataFromTraceFile(file);
            for (const metadata of metadataList) {
              metadataMap.set(metadata.traceId, metadata);
            }
          } catch (error) {
            console.error(`Failed to extract metadata from ${file.name}:`, error);
          }
        }

        setState((prev) => ({ ...prev, traceMetadata: metadataMap, isLoadingMetadata: false }));
      },

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
        const initialRows = new Map();
        const metadataArray = Array.from(state.traceMetadata.values());

        for (let i = 0; i < metadataArray.length; i++) {
          const metadata = metadataArray[i];
          initialRows.set(metadata.traceId, {
            traceId: metadata.traceId,
            sessionId: metadata.sessionId,
            status: 'loading' as const,
            agentName: metadata.agentName,
            startTime: metadata.startTime,
            model: metadata.model,
            userInputPreview: metadata.userInputPreview,
            finalOutputPreview: metadata.finalOutputPreview,
            invocations: metadata.invocations,
            metricResults: new Map(),
            conversionWarnings: [],
          });
        }

        setState((prev) => ({
          ...prev,
          isEvaluating: true,
          progressMessage: '',
          errors: [],
          tableRows: initialRows,
          expectedTraceCount: state.traceFiles.length,
          currentView: 'dashboard',
        }));

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
            (traceId, _status, partialResult) => {
              setState((prev) => {
                if (!partialResult) return prev;

                const newRows = new Map(prev.tableRows);
                const existingRow = newRows.get(traceId);
                const metadata = prev.traceMetadata.get(traceId);

                const existingMetrics = existingRow?.metricResults || new Map();
                partialResult.metricResults.forEach(mr => {
                  existingMetrics.set(mr.metricName, mr);
                });

                const allMetricsComplete = prev.selectedMetrics.length === partialResult.metricResults.length;

                newRows.set(traceId, {
                  traceId,
                  sessionId: partialResult.sessionId ?? existingRow?.sessionId ?? metadata?.sessionId,
                  status: allMetricsComplete ? 'complete' : 'loading',
                  agentName: partialResult.agentName ?? existingRow?.agentName ?? metadata?.agentName,
                  startTime: partialResult.startTime ?? existingRow?.startTime ?? metadata?.startTime,
                  model: partialResult.model ?? existingRow?.model ?? metadata?.model,
                  userInputPreview: partialResult.userInputPreview ?? existingRow?.userInputPreview ?? metadata?.userInputPreview,
                  finalOutputPreview: partialResult.finalOutputPreview ?? existingRow?.finalOutputPreview ?? metadata?.finalOutputPreview,
                  invocations: metadata?.invocations,
                  metricResults: existingMetrics,
                  numInvocations: partialResult.numInvocations,
                  conversionWarnings: partialResult.conversionWarnings,
                  performanceMetrics: partialResult.performanceMetrics,
                });

                const newResults = [...prev.results];
                const existingResultIndex = newResults.findIndex(r => r.traceId === traceId);

                const resultWithSessionId = {
                  ...partialResult,
                  sessionId: partialResult.sessionId ?? metadata?.sessionId,
                };

                if (existingResultIndex >= 0) {
                  newResults[existingResultIndex] = resultWithSessionId;
                } else {
                  newResults.push(resultWithSessionId);
                }

                return {
                  ...prev,
                  tableRows: newRows,
                  results: newResults,
                };
              });
            },
            (result) => {
              setState((prev) => {
                const resultsWithSessionId = result.traceResults.map(tr => ({
                  ...tr,
                  sessionId: prev.traceMetadata.get(tr.traceId)?.sessionId,
                }));

                const mergedRows = new Map(prev.tableRows);
                if (prev.pendingAnnotations.size > 0) {
                  for (const [traceId, row] of mergedRows) {
                    const annotation = prev.pendingAnnotations.get(traceId);
                    if (annotation) {
                      mergedRows.set(traceId, { ...row, annotation });
                    }
                  }
                }

                return {
                  ...prev,
                  isEvaluating: false,
                  progressMessage: '',
                  results: resultsWithSessionId,
                  errors: result.errors,
                  tableRows: mergedRows,
                  pendingAnnotations: new Map(),
                };
              });
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

      setEvaluationOrigin: (view: ViewType | null) =>
        setState((prev) => ({ ...prev, evaluationOrigin: view })),

      setStreamingSessions: (updater: (prev: Map<string, LiveSession>) => Map<string, LiveSession>) =>
        setState((prev) => ({ ...prev, streamingSessions: updater(prev.streamingSessions) })),

      removeSession: (sessionId: string) =>
        setState((prev) => {
          const newMap = new Map(prev.streamingSessions);
          newMap.delete(sessionId);
          return { ...prev, streamingSessions: newMap };
        }),

      clearAllSessions: () =>
        setState((prev) => ({
          ...prev,
          streamingSessions: new Map(
            [...prev.streamingSessions].filter(([, s]) => s.status === 'active')
          ),
        })),

      selectTrace: (traceId: string | null) =>
        setState((prev) => ({ ...prev, selectedTraceId: traceId })),

      clearResults: () =>
        setState((prev) => ({
          ...prev,
          results: [],
          errors: [],
          currentView: 'upload',
        })),

      createAnnotationQueue: (name: string) => {
        const id = `queue_${Date.now()}`;
        const queue: AnnotationQueue = {
          id,
          name,
          items: [],
          createdAt: new Date().toISOString(),
        };
        setState((prev) => ({ ...prev, annotationQueues: [...prev.annotationQueues, queue] }));
        return id;
      },

      addToAnnotationQueue: (queueId: string, session: LiveSession) => {
        setState((prev) => {
          const queues = prev.annotationQueues.map((q) => {
            if (q.id !== queueId) return q;
            if (q.items.some((item) => item.sessionId === session.sessionId)) return q;
            const totalTokens =
              (session.liveStats?.totalInputTokens ?? 0) +
              (session.liveStats?.totalOutputTokens ?? 0);
            const newItem = {
              sessionId: session.sessionId,
              traceId: session.traceId,
              agentName: session.metadata?.agentName,
              startTime: session.startedAt,
              model: session.liveStats?.model || session.metadata?.model,
              totalTokens: totalTokens > 0 ? totalTokens : undefined,
              invocations: session.invocations,
              liveElements: session.liveElements,
              liveStats: session.liveStats,
              metadata: session.metadata,
            };
            return { ...q, items: [...q.items, newItem] };
          });
          return { ...prev, annotationQueues: queues };
        });
      },

      annotateQueueItem: (queueId: string, sessionId: string, annotation: Annotation) => {
        setState((prev) => ({
          ...prev,
          annotationQueues: prev.annotationQueues.map((q) =>
            q.id === queueId
              ? {
                  ...q,
                  items: q.items.map((item) =>
                    item.sessionId === sessionId ? { ...item, annotation } : item
                  ),
                }
              : q
          ),
        }));
      },

      setCurrentAnnotationQueueId: (id: string | null) =>
        setState((prev) => ({ ...prev, currentAnnotationQueueId: id })),

      setPendingAnnotations: (annotations: Map<string, Annotation>) =>
        setState((prev) => ({ ...prev, pendingAnnotations: annotations })),

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
    [state.traceFiles, state.traceMetadata, state.evalSetFile, state.selectedMetrics, state.judgeModel, state.threshold]
  );

  return (
    <TraceContext.Provider value={{ state, actions }}>
      {children}
    </TraceContext.Provider>
  );
};
