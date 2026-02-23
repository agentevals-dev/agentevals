import React from 'react';
import { css } from '@emotion/react';
import type { PerformanceMetrics } from '../../lib/types';

interface PerformanceSectionProps {
  metrics: PerformanceMetrics;
}

export const PerformanceSection: React.FC<PerformanceSectionProps> = ({ metrics }) => {
  if (!metrics || !metrics.latency || !metrics.tokens) {
    return null;
  }

  const { latency, tokens } = metrics;

  return (
    <div css={sectionStyle}>
      <h3>Performance</h3>

      <table>
        <tbody>
          <tr>
            <td>Overall Latency (p99)</td>
            <td>{latency.overall.p99.toFixed(0)} ms</td>
          </tr>
          <tr>
            <td>LLM Call (p99)</td>
            <td>{latency.llmCalls.p99.toFixed(0)} ms</td>
          </tr>
          <tr>
            <td>Tool Execution (p99)</td>
            <td>{latency.toolExecutions.p99.toFixed(0)} ms</td>
          </tr>
          <tr className="separator">
            <td>Total Tokens</td>
            <td>{tokens.total.toLocaleString()} ({tokens.totalPrompt.toLocaleString()} prompt + {tokens.totalOutput.toLocaleString()} output)</td>
          </tr>
          <tr>
            <td>Tokens per LLM Call (p99)</td>
            <td>{tokens.perLlmCall.p99.toFixed(0)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
};

const sectionStyle = css`
  padding: 16px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 8px;

  h3 {
    margin: 0 0 12px 0;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;

    td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border-light);
    }

    td:first-of-type {
      font-family: system-ui, -apple-system, sans-serif;
      color: var(--text-secondary);
      font-weight: 500;
      width: 50%;
    }

    td:last-of-type {
      color: var(--text-primary);
      font-family: 'Courier New', monospace;
      text-align: right;
    }

    tr.separator td {
      padding-top: 16px;
      font-weight: 600;
      border-top: 2px solid var(--border-default);
    }

    tr:last-child td {
      border-bottom: none;
    }
  }
`;
