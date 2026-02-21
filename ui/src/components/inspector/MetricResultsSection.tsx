import React from 'react';
import { css } from '@emotion/react';
import { CheckCircle, XCircle, AlertCircle, MinusCircle, Loader2 } from 'lucide-react';
import type { MetricResult } from '../../lib/types';
import { getStatusColor } from '../../lib/utils';

interface MetricResultsSectionProps {
  metricResults: MetricResult[];
  threshold: number;
  selectedMetrics?: string[];
  isEvaluating?: boolean;
}

export const MetricResultsSection: React.FC<MetricResultsSectionProps> = ({
  metricResults,
  threshold,
  selectedMetrics = [],
  isEvaluating = false,
}) => {
  const metricResultsMap = new Map(metricResults.map(mr => [mr.metricName, mr]));
  const metricsToShow = selectedMetrics.length > 0 ? selectedMetrics : metricResults.map(mr => mr.metricName);

  return (
    <div css={containerStyles}>
      <div css={headerStyles}>
        <h3>Evaluation Results</h3>
        <span css={thresholdStyles}>Threshold: {threshold}</span>
      </div>

      <div css={metricsListStyles}>
        {metricsToShow.map((metricName) => {
          const result = metricResultsMap.get(metricName);

          if (!result && isEvaluating) {
            return (
              <div key={metricName} css={metricItemStyles('var(--text-secondary)')}>
                <div css={metricHeaderStyles}>
                  <div css={iconStatusStyles('var(--text-secondary)')}>
                    <Loader2 size={16} className="animate-spin" />
                  </div>
                  <span css={metricNameStyles}>{metricName}</span>
                </div>
                <div css={metricBodyStyles}>
                  <div css={scoreDisplayStyles}>
                    <span css={statusTextStyles('var(--text-secondary)')}>
                      Evaluating...
                    </span>
                  </div>
                </div>
              </div>
            );
          }

          if (!result) {
            return null;
          }

          const index = metricsToShow.indexOf(metricName);
          const statusColor = getStatusColor(result.evalStatus);
          const StatusIcon = getStatusIcon(result.evalStatus);

          return (
            <div key={index} css={metricItemStyles(statusColor)}>
              <div css={metricHeaderStyles}>
                <div css={iconStatusStyles(statusColor)}>
                  <StatusIcon size={16} />
                </div>
                <span css={metricNameStyles}>{result.metricName}</span>
              </div>

              <div css={metricBodyStyles}>
                <div css={scoreDisplayStyles}>
                  {result.score !== null ? (
                    <>
                      <span css={scoreValueStyles(statusColor)}>
                        {result.score.toFixed(2)}
                      </span>
                      <span css={statusTextStyles(statusColor)}>
                        {result.evalStatus}
                      </span>
                    </>
                  ) : (
                    <span css={statusTextStyles(statusColor)}>
                      {result.evalStatus}
                    </span>
                  )}
                </div>

                {result.perInvocationScores.length > 1 && (
                  <div css={perInvocationScoresStyles}>
                    <div css={perInvocationLabelStyles}>Per-invocation:</div>
                    <div css={scoresListStyles}>
                      {result.perInvocationScores.map((score, idx) => (
                        <span key={idx} css={invocationScoreStyles}>
                          {score !== null ? score.toFixed(2) : 'N/A'}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {result.error && (
                  <div css={errorMessageStyles}>
                    <AlertCircle size={14} />
                    <span>{result.error}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

function getStatusIcon(status: string) {
  switch (status) {
    case 'PASSED':
      return CheckCircle;
    case 'FAILED':
      return XCircle;
    case 'ERROR':
      return AlertCircle;
    default:
      return MinusCircle;
  }
}

const containerStyles = css`
  background: var(--bg-elevated);
  border-radius: 6px;
  border: 1px solid var(--border-default);
  padding: 16px;
  margin-top: 16px;
`;

const headerStyles = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;

  h3 {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }
`;

const thresholdStyles = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-family: var(--font-mono);
`;

const metricsListStyles = css`
  display: flex;
  flex-direction: column;
  gap: 12px;
`;

const metricItemStyles = (statusColor: string) => css`
  background: var(--bg-primary);
  border-radius: 4px;
  border-left: 3px solid ${statusColor};
  padding: 12px;
  transition: all 0.2s ease;

  &:hover {
    background: var(--bg-surface);
    box-shadow: 0 0 12px ${statusColor}33;
  }
`;

const metricHeaderStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
`;

const iconStatusStyles = (color: string) => css`
  display: flex;
  align-items: center;
  color: ${color};
`;

const metricNameStyles = css`
  font-family: var(--font-mono);
  font-size: 0.813rem;
  font-weight: 500;
  color: var(--text-primary);
`;

const metricBodyStyles = css`
  display: flex;
  flex-direction: column;
  gap: 8px;
`;

const scoreDisplayStyles = css`
  display: flex;
  align-items: center;
  gap: 12px;
`;

const scoreValueStyles = (color: string) => css`
  font-size: 1.25rem;
  font-weight: 700;
  font-family: var(--font-mono);
  color: ${color};
`;

const statusTextStyles = (color: string) => css`
  font-size: 0.75rem;
  font-weight: 600;
  color: ${color};
  letter-spacing: 0.5px;
`;

const perInvocationScoresStyles = css`
  margin-top: 4px;
`;

const perInvocationLabelStyles = css`
  font-size: 0.688rem;
  color: var(--text-secondary);
  margin-bottom: 4px;
`;

const scoresListStyles = css`
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
`;

const invocationScoreStyles = css`
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--text-primary);
  background: var(--bg-elevated);
  padding: 4px 8px;
  border-radius: 3px;
`;

const errorMessageStyles = css`
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--status-failure);
  font-size: 0.75rem;
  margin-top: 8px;
  padding: 8px;
  background: rgba(255, 87, 87, 0.1);
  border-radius: 4px;

  svg {
    flex-shrink: 0;
    margin-top: 2px;
  }

  span {
    flex: 1;
    line-height: 1.4;
  }

  .animate-spin {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }
`;
