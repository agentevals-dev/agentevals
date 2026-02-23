import React from 'react';
import { css } from '@emotion/react';
import type { RunResultPerformanceMetrics } from '../../lib/types';

interface PerformanceCardProps {
  metrics: RunResultPerformanceMetrics;
}

export const PerformanceCard: React.FC<PerformanceCardProps> = ({ metrics }) => {
  if (!metrics || !metrics.tokens) {
    return null;
  }

  return (
    <div css={cardStyle}>
      <h3>Overall Performance</h3>
      <div className="metrics-grid">
        <div className="metric-section">
          <h4>Token Usage</h4>
          <div className="stat">
            <span className="label">Total:</span>
            <span className="value">{metrics.tokens.total?.toLocaleString() || '0'}</span>
          </div>
          <div className="stat">
            <span className="label">Prompt:</span>
            <span className="value">{metrics.tokens.totalPrompt?.toLocaleString() || '0'}</span>
          </div>
          <div className="stat">
            <span className="label">Output:</span>
            <span className="value">{metrics.tokens.totalOutput?.toLocaleString() || '0'}</span>
          </div>
          <div className="stat">
            <span className="label">Avg/Trace:</span>
            <span className="value">
              {Math.round(metrics.tokens.avgPerTrace?.prompt || 0)} + {Math.round(metrics.tokens.avgPerTrace?.output || 0)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};

const cardStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;

  h3 {
    margin: 0 0 16px 0;
    font-size: 1.25rem;
    color: var(--text-primary);
  }

  h4 {
    margin: 0 0 12px 0;
    font-size: 1rem;
    color: var(--text-secondary);
  }

  .metrics-grid {
    display: grid;
    gap: 16px;
  }

  .stat {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid var(--border-light);

    &:last-child {
      border-bottom: none;
    }

    .label {
      color: var(--text-secondary);
      font-weight: 500;
    }

    .value {
      color: var(--text-primary);
      font-weight: 600;
      font-family: 'Courier New', monospace;
    }
  }
`;
