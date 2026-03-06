import React, { useState, useMemo, useEffect } from 'react';
import { css } from '@emotion/react';
import { useTraceContext } from '../../context/TraceContext';
import { InspectorHeader } from './InspectorHeader';
import { InspectorLayout } from './InspectorLayout';
import { InvocationSummaryPanel } from './InvocationSummaryPanel';
import { SpanTreePanel } from './SpanTreePanel';
import { ComparisonPanel } from './ComparisonPanel';
import type { InspectorUIState, Trace, Invocation } from '../../lib/types';
import { loadJaegerTraces } from '../../lib/trace-loader';
import { convertTracesToInvocations } from '../../lib/trace-converter';
import { readFileAsText } from '../../lib/utils';

const SHOW_EXECUTION_KEY = 'inspector-show-execution';

export const InspectorView: React.FC = () => {
  const { state, actions } = useTraceContext();

  // Local inspector UI state
  const [inspectorState, setInspectorState] = useState<InspectorUIState>(() => {
    const showExecution = localStorage.getItem(SHOW_EXECUTION_KEY) === 'true';
    return {
      selectedInvocationId: null,
      selectedDataPath: null,
      highlightedSpanIds: new Set(),
      hoveredElement: null,
      leftPanelWidth: 400,
      rightPanelWidth: 400,
      jsonScrollPosition: 0,
      showExecution,
    };
  });

  // Loaded trace data
  const [trace, setTrace] = useState<Trace | null>(null);
  const [invocations, setInvocations] = useState<Invocation[]>([]);
  const [expectedInvocations, setExpectedInvocations] = useState<Invocation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Find the selected trace result (check tableRows first for partial data during evaluation)
  const traceResult = useMemo(() => {
    const tableRow = state.tableRows.get(state.selectedTraceId || '');
    if (tableRow) {
      return {
        traceId: tableRow.traceId,
        numInvocations: tableRow.numInvocations || 0,
        metricResults: Array.from(tableRow.metricResults.values()),
        conversionWarnings: tableRow.conversionWarnings,
        performanceMetrics: tableRow.performanceMetrics,
      };
    }
    return state.results.find(r => r.traceId === state.selectedTraceId);
  }, [state.tableRows, state.results, state.selectedTraceId]);

  // Load trace data when component mounts
  useEffect(() => {
    const loadTraceData = async () => {
      if (!traceResult || !state.traceFiles.length) {
        setError('Trace data not available');
        setLoading(false);
        return;
      }

      try {
        setLoading(true);
        setError(null);

        // Find the matching trace file by reading and parsing each one
        let foundTrace: Trace | null = null;
        for (const file of state.traceFiles) {
          const content = await readFileAsText(file);
          const traces = await loadJaegerTraces(content);
          const matchingTrace = traces.find(t => t.traceId === traceResult.traceId);
          if (matchingTrace) {
            foundTrace = matchingTrace;
            break;
          }
        }

        if (!foundTrace) {
          setError('Could not find trace in uploaded files');
          setLoading(false);
          return;
        }

        setTrace(foundTrace);

        // Convert to invocations
        const conversionResults = convertTracesToInvocations([foundTrace]);
        const result = conversionResults.get(foundTrace.traceId);

        if (result) {
          setInvocations(result.invocations);
        } else {
          setInvocations([]);
        }

        // Load evalset if available
        if (state.evalSetFile) {
          try {
            const evalSetContent = await readFileAsText(state.evalSetFile);
            const evalSet = JSON.parse(evalSetContent);

            // Extract expected invocations from eval cases
            const expectedInvs: Invocation[] = [];
            if (evalSet.eval_cases) {
              for (const evalCase of evalSet.eval_cases) {
                if (evalCase.conversation) {
                  for (const inv of evalCase.conversation) {
                    // Map eval case format to Invocation format
                    const intermediateData = inv.intermediate_data || {};
                    expectedInvs.push({
                      invocationId: inv.invocationId || evalCase.eval_id,
                      userContent: inv.user_content || { role: 'user', parts: [] },
                      finalResponse: inv.final_response || { role: 'model', parts: [] },
                      intermediateData: {
                        toolUses: intermediateData.tool_uses || [],
                        toolResponses: intermediateData.tool_responses || [],
                      },
                    });
                  }
                }
              }
            }
            console.log('Loaded expected invocations:', expectedInvs);
            setExpectedInvocations(expectedInvs);
          } catch (err) {
            console.error('Error loading evalset:', err);
            setExpectedInvocations([]);
          }
        }

        setLoading(false);
      } catch (err) {
        console.error('Error loading trace data:', err);
        setError(err instanceof Error ? err.message : 'Failed to load trace data');
        setLoading(false);
      }
    };

    loadTraceData();
  }, [traceResult, state.traceFiles]);

  // Handle back to dashboard
  const handleBack = () => {
    actions.setCurrentView('dashboard');
  };

  // Handle execution toggle
  const handleToggleExecution = (show: boolean) => {
    setInspectorState(prev => ({ ...prev, showExecution: show }));
    localStorage.setItem(SHOW_EXECUTION_KEY, String(show));
  };

  // Handle span selection
  const handleSelectSpan = (spanId: string) => {
    console.log('Selected span:', spanId);
    actions.selectSpan(spanId);
  };

  // Handle span hover
  const handleHoverSpan = (spanId: string | null) => {
    setInspectorState(prev => ({
      ...prev,
      hoveredElement: spanId ? { type: 'span', id: spanId } : null,
    }));
  };

  // Handle invocation selection
  const handleSelectInvocation = (invocationId: string) => {
    setInspectorState(prev => ({
      ...prev,
      selectedInvocationId: invocationId,
    }));
  };

  if (!traceResult) {
    return (
      <div css={errorContainerStyles}>
        <div css={errorMessageStyles}>
          <h2>Trace not found</h2>
          <p>The selected trace could not be found in the results.</p>
          <button onClick={handleBack} css={backButtonStyles}>
            Back to Dashboard
          </button>
        </div>
      </div>
    );
  }

  // Calculate span count for the toggle button
  const spanCount = trace?.allSpans?.length || 0;

  if (loading) {
    return (
      <div css={containerStyles}>
        <InspectorHeader
          traceResult={traceResult}
          onBack={handleBack}
          showExecution={inspectorState.showExecution}
          onToggleExecution={handleToggleExecution}
          spanCount={spanCount}
        />
        <div css={loadingContainerStyles}>
          <div css={loadingSpinnerStyles} />
          <p>Loading trace data...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div css={containerStyles}>
        <InspectorHeader
          traceResult={traceResult}
          onBack={handleBack}
          showExecution={inspectorState.showExecution}
          onToggleExecution={handleToggleExecution}
          spanCount={spanCount}
        />
        <div css={errorContainerStyles}>
          <div css={errorMessageStyles}>
            <h2>Error loading trace</h2>
            <p>{error}</p>
            <button onClick={handleBack} css={backButtonStyles}>
              Back to Dashboard
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Build panels
  const leftPanel = (
    <InvocationSummaryPanel
      invocations={invocations}
      selectedInvocationId={inspectorState.selectedInvocationId}
      onSelectInvocation={handleSelectInvocation}
    />
  );

  const centerPanel = trace && inspectorState.showExecution ? (
    <SpanTreePanel
      trace={trace}
      spanToDataMapping={new Map()} // Will be populated in Step 5
      highlightedSpanIds={state.selectedSpanId ? new Set([state.selectedSpanId]) : new Set()}
      onSelectSpan={handleSelectSpan}
      onHoverSpan={handleHoverSpan}
    />
  ) : null;

  // Find selected or first invocation for comparison
  const selectedInvocation = inspectorState.selectedInvocationId
    ? invocations.find(inv => inv.invocationId === inspectorState.selectedInvocationId)
    : invocations[0];

  // Match with expected invocation
  const matchExpectedInvocation = (actual: Invocation): Invocation | null => {
    if (expectedInvocations.length === 0) return null;

    // If only one expected invocation, use it
    if (expectedInvocations.length === 1) {
      return expectedInvocations[0];
    }

    // Try to match by user content text
    const actualUserText = actual.userContent.parts
      .filter(p => p.text)
      .map(p => p.text)
      .join(' ')
      .toLowerCase()
      .trim();

    return expectedInvocations.find(exp => {
      const expUserText = exp.userContent.parts
        .filter(p => p.text)
        .map(p => p.text)
        .join(' ')
        .toLowerCase()
        .trim();
      return expUserText === actualUserText;
    }) || null;
  };

  const expectedInvocation = selectedInvocation ? matchExpectedInvocation(selectedInvocation) : null;

  const rightPanel = (
    <ComparisonPanel
      actualInvocation={selectedInvocation || null}
      expectedInvocation={expectedInvocation}
      metricResults={traceResult.metricResults}
      threshold={state.threshold}
      selectedMetrics={state.selectedMetrics}
      isEvaluating={state.isEvaluating}
      performanceMetrics={traceResult.performanceMetrics}
      allActualInvocations={invocations}
      allExpectedInvocations={expectedInvocations}
    />
  );

  return (
    <div css={containerStyles}>
      <InspectorHeader
        traceResult={traceResult}
        onBack={handleBack}
        showExecution={inspectorState.showExecution}
        onToggleExecution={handleToggleExecution}
        spanCount={spanCount}
      />
      <InspectorLayout
        leftPanel={leftPanel}
        centerPanel={centerPanel}
        rightPanel={rightPanel}
        showCenterPanel={inspectorState.showExecution}
      />
    </div>
  );
};

const containerStyles = css`
  width: 100%;
  height: 100vh;
  background: var(--bg-primary);
  overflow: hidden;
`;

const loadingContainerStyles = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: calc(100vh - 64px);
  gap: 16px;
  color: var(--text-secondary);

  p {
    font-size: 0.875rem;
    margin: 0;
  }
`;

const loadingSpinnerStyles = css`
  width: 40px;
  height: 40px;
  border: 3px solid var(--border-default);
  border-top-color: var(--accent-cyan);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
`;

const errorContainerStyles = css`
  width: 100%;
  height: calc(100vh - 64px);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-primary);
`;

const errorMessageStyles = css`
  text-align: center;
  color: var(--text-primary);

  h2 {
    font-size: 1.5rem;
    margin-bottom: 8px;
    color: var(--status-failure);
  }

  p {
    font-size: 1rem;
    color: var(--text-secondary);
    margin-bottom: 24px;
  }
`;

const backButtonStyles = css`
  padding: 12px 24px;
  background: var(--accent-cyan);
  border: none;
  border-radius: 6px;
  color: var(--bg-primary);
  font-family: var(--font-display);
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--accent-purple);
    box-shadow: 0 0 20px rgba(0, 217, 255, 0.3);
  }
`;


