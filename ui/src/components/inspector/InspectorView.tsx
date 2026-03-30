import React, { useState, useMemo, useEffect } from 'react';
import { css } from '@emotion/react';
import { useTraceContext } from '../../context/TraceContext';
import { InspectorHeader } from './InspectorHeader';
import { InspectorLayout } from './InspectorLayout';
import { InvocationSummaryPanel } from './InvocationSummaryPanel';
import { ComparisonPanel } from './ComparisonPanel';
import type { Invocation } from '../../lib/types';
import { readFileAsText } from '../../lib/utils';

export const InspectorView: React.FC = () => {
  const { state, actions } = useTraceContext();

  // Local inspector UI state
  const [selectedInvocationId, setSelectedInvocationId] = useState<string | null>(null);

  // Loaded trace data
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

  // Load invocations from state (already converted by backend) and eval set from file
  useEffect(() => {
    const loadData = async () => {
      if (!traceResult) {
        setError('Trace data not available');
        setLoading(false);
        return;
      }

      try {
        setLoading(true);
        setError(null);

        const tableRow = state.tableRows.get(traceResult.traceId);
        const metadata = state.traceMetadata.get(traceResult.traceId);
        const invs = tableRow?.invocations || metadata?.invocations || [];
        setInvocations(invs);

        if (state.evalSetFile) {
          try {
            const evalSetContent = await readFileAsText(state.evalSetFile);
            const evalSet = JSON.parse(evalSetContent);

            const expectedInvs: Invocation[] = [];
            if (evalSet.eval_cases) {
              for (const evalCase of evalSet.eval_cases) {
                if (evalCase.conversation) {
                  for (const inv of evalCase.conversation) {
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

    loadData();
  }, [traceResult, state.traceMetadata, state.tableRows, state.evalSetFile]);

  // Handle back to dashboard
  const handleBack = () => {
    actions.setCurrentView('dashboard');
  };

  // Handle invocation selection
  const handleSelectInvocation = (invocationId: string) => {
    setSelectedInvocationId(invocationId);
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

  if (loading) {
    return (
      <div css={containerStyles}>
        <InspectorHeader
          traceResult={traceResult}
          onBack={handleBack}
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
      selectedInvocationId={selectedInvocationId}
      onSelectInvocation={handleSelectInvocation}
    />
  );

  // Find selected or first invocation for comparison
  const selectedInvocation = selectedInvocationId
    ? invocations.find(inv => inv.invocationId === selectedInvocationId)
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
      />
      <InspectorLayout
        leftPanel={leftPanel}
        rightPanel={rightPanel}
      />
    </div>
  );
};

const containerStyles = css`
  width: 100%;
  height: 100%;
  background: var(--bg-primary);
  overflow: hidden;
`;

const loadingContainerStyles = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: calc(100% - 64px);
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
  border-top-color: var(--accent-primary);
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
  height: calc(100% - 64px);
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
  background: var(--accent-primary);
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
    box-shadow: 0 0 20px rgba(168, 85, 247, 0.3);
  }
`;
