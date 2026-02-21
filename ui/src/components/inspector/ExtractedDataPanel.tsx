import React from 'react';
import { css } from '@emotion/react';
import { InvocationCard } from './InvocationCard';
import { MetricResultsSection } from './MetricResultsSection';
import type { Invocation, MetricResult } from '../../lib/types';

interface ExtractedDataPanelProps {
  invocations: Invocation[];
  metricResults: MetricResult[];
  threshold: number;
  selectedInvocationId?: string | null;
  highlightedPaths?: Set<string>;
  onSelectData?: (dataPath: string) => void;
  onSelectInvocation?: (invocationId: string) => void;
  selectedMetrics?: string[];
  isEvaluating?: boolean;
}

export const ExtractedDataPanel: React.FC<ExtractedDataPanelProps> = ({
  invocations,
  metricResults,
  threshold,
  selectedInvocationId = null,
  highlightedPaths = new Set(),
  onSelectData,
  onSelectInvocation,
  selectedMetrics = [],
  isEvaluating = false,
}) => {
  if (invocations.length === 0) {
    return (
      <div css={emptyStateStyles}>
        <h3>No Invocations Found</h3>
        <p>This trace doesn't contain any invocation data.</p>
      </div>
    );
  }

  return (
    <div css={panelContainerStyles}>
      <div css={panelHeaderStyles}>
        <h2>Extracted Data</h2>
        <span css={invocationCountStyles}>
          {invocations.length} {invocations.length === 1 ? 'Invocation' : 'Invocations'}
        </span>
      </div>

      <div css={panelContentStyles}>
        <div css={invocationsListStyles}>
          {invocations.map((invocation, index) => (
            <InvocationCard
              key={invocation.invocationId}
              invocation={invocation}
              index={index}
              isSelected={selectedInvocationId === invocation.invocationId}
              highlightedPaths={highlightedPaths}
              onSelectData={onSelectData}
              onSelectInvocation={onSelectInvocation}
            />
          ))}
        </div>

        <MetricResultsSection
          metricResults={metricResults}
          threshold={threshold}
          selectedMetrics={selectedMetrics}
          isEvaluating={isEvaluating}
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

const invocationCountStyles = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-weight: 500;
  padding: 4px 12px;
  background: var(--bg-primary);
  border-radius: 12px;
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

const invocationsListStyles = css`
  display: flex;
  flex-direction: column;
  gap: 16px;
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

  h3 {
    font-size: 1.25rem;
    margin-bottom: 8px;
    color: var(--text-primary);
  }

  p {
    font-size: 0.875rem;
    margin: 0;
  }
`;
