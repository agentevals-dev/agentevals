import React from 'react';
import { css } from '@emotion/react';
import { AlertCircle, CheckCircle, XCircle } from 'lucide-react';
import type { Invocation, MetricResult, PerformanceMetrics } from '../../lib/types';
import { MetricsComparisonSection } from './MetricsComparisonSection';
import { PerformanceSection } from './PerformanceSection';

interface ComparisonPanelProps {
  actualInvocation: Invocation | null;
  expectedInvocation: Invocation | null;
  metricResults: MetricResult[];
  threshold: number;
  selectedMetrics: string[];
  isEvaluating: boolean;
  performanceMetrics?: PerformanceMetrics;
  allActualInvocations?: Invocation[];
  allExpectedInvocations?: Invocation[];
}

export const ComparisonPanel: React.FC<ComparisonPanelProps> = ({
  actualInvocation,
  expectedInvocation,
  metricResults,
  threshold,
  selectedMetrics,
  isEvaluating,
  performanceMetrics,
  allActualInvocations,
  allExpectedInvocations,
}) => {
  if (!actualInvocation) {
    return (
      <div css={emptyStateStyles}>
        <AlertCircle size={32} />
        <p>Select an invocation to see comparison</p>
      </div>
    );
  }

  // Find failed metrics
  const failedMetrics = metricResults.filter(m => m.evalStatus === 'FAILED');

  return (
    <div css={panelContainerStyles}>
      <div css={panelHeaderStyles}>
        <h2>Evaluation Results</h2>
        {failedMetrics.length > 0 ? (
          <div css={failedBadgeStyles}>
            <XCircle size={14} />
            {failedMetrics.length} Failed
          </div>
        ) : (
          <div css={passedBadgeStyles}>
            <CheckCircle size={14} />
            All Passed
          </div>
        )}
      </div>

      <div css={panelContentStyles}>
        {performanceMetrics && (
          <div css={performanceSectionContainerStyles}>
            <PerformanceSection metrics={performanceMetrics} />
          </div>
        )}

        <MetricsComparisonSection
          metricResults={metricResults}
          expectedInvocation={expectedInvocation}
          actualInvocation={actualInvocation}
          threshold={threshold}
          selectedMetrics={selectedMetrics}
          isEvaluating={isEvaluating}
          allActualInvocations={allActualInvocations}
          allExpectedInvocations={allExpectedInvocations}
        />
      </div>
    </div>
  );
};

const panelContainerStyles = css`
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-surface);
`;

const panelHeaderStyles = css`
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-elevated);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;

  h2 {
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }
`;

const passedBadgeStyles = css`
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  background: rgba(124, 255, 107, 0.1);
  border: 1px solid var(--status-success);
  border-radius: 12px;
  color: var(--status-success);
  font-size: 0.75rem;
  font-weight: 600;
`;

const failedBadgeStyles = css`
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  background: rgba(255, 87, 87, 0.1);
  border: 1px solid var(--status-failure);
  border-radius: 12px;
  color: var(--status-failure);
  font-size: 0.75rem;
  font-weight: 600;
`;

const panelContentStyles = css`
  flex: 1;
  overflow-y: auto;
  padding: 16px;

  &::-webkit-scrollbar {
    width: 8px;
  }

  &::-webkit-scrollbar-track {
    background: var(--bg-primary);
  }

  &::-webkit-scrollbar-thumb {
    background: var(--border-default);
    border-radius: 4px;
  }

  &::-webkit-scrollbar-thumb:hover {
    background: var(--accent-cyan);
  }
`;

const performanceSectionContainerStyles = css`
  margin-bottom: 16px;
`;

const emptyStateStyles = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 32px;
  text-align: center;
  color: var(--text-secondary);
  gap: 12px;

  svg {
    opacity: 0.3;
  }

  p {
    font-size: 0.875rem;
    margin: 0;
    color: var(--text-primary);
  }
`;

