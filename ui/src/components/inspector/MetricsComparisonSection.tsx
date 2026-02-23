import React from 'react';
import { css } from '@emotion/react';
import { CheckCircle, XCircle, AlertCircle, MinusCircle, Loader2 } from 'lucide-react';
import type { MetricResult, Invocation } from '../../lib/types';
import { getStatusColor } from '../../lib/utils';

interface MetricsComparisonSectionProps {
  metricResults: MetricResult[];
  expectedInvocation: Invocation | null;
  actualInvocation: Invocation | null;
  threshold: number;
  selectedMetrics: string[];
  isEvaluating: boolean;
}

export const MetricsComparisonSection: React.FC<MetricsComparisonSectionProps> = ({
  metricResults,
  expectedInvocation,
  actualInvocation,
  threshold,
  selectedMetrics,
  isEvaluating,
}) => {
  // Helper to extract text from content parts
  const getTextFromParts = (parts: any[]) => {
    return parts.filter(p => p.text).map(p => p.text).join('\n');
  };

  // Helper to truncate long text
  const truncateText = (text: string, maxLength: number = 150) => {
    if (!text || text.length === 0) return 'N/A';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
  };

  // Extract expected and actual values for a given metric
  const getMetricComparison = (
    metric: MetricResult
  ): { expected: React.ReactNode; actual: React.ReactNode } => {
    if (!expectedInvocation || !actualInvocation) {
      return {
        expected: 'No eval set',
        actual: metric.score !== null ? metric.score.toFixed(2) : 'N/A',
      };
    }

    const metricName = metric.metricName;

    // Tool trajectory metrics
    if (metricName === 'tool_trajectory_avg_score') {
      const expectedTools = (expectedInvocation.intermediateData?.toolUses || []).map(t => t.name);
      const actualTools = (actualInvocation.intermediateData?.toolUses || []).map(t => t.name);

      return {
        expected: (
          <div css={toolListStyles}>
            {expectedTools.length > 0 ? (
              expectedTools.map((tool, idx) => (
                <div key={idx} css={toolItemStyles}>
                  {idx + 1}. {tool}
                </div>
              ))
            ) : (
              <span css={emptyTextStyles}>No tools</span>
            )}
          </div>
        ),
        actual: (
          <div css={toolListStyles}>
            {actualTools.length > 0 ? (
              actualTools.map((tool, idx) => (
                <div key={idx} css={toolItemStyles}>
                  {idx + 1}. {tool}
                </div>
              ))
            ) : (
              <span css={emptyTextStyles}>No tools</span>
            )}
            {metric.score !== null && (
              <div css={scoreDisplayStyles}>Score: {metric.score.toFixed(2)}</div>
            )}
          </div>
        ),
      };
    }

    // Response matching metrics
    if (
      metricName === 'response_match_score' ||
      metricName === 'final_response_match_v2'
    ) {
      const expectedResponse = getTextFromParts(expectedInvocation.finalResponse.parts);
      const actualResponse = getTextFromParts(actualInvocation.finalResponse.parts);

      return {
        expected: (
          <div css={textContentStyles}>
            {truncateText(expectedResponse, 200)}
          </div>
        ),
        actual: (
          <div css={textContentStyles}>
            {truncateText(actualResponse, 200)}
            {metric.score !== null && (
              <div css={scoreDisplayStyles}>Score: {metric.score.toFixed(2)}</div>
            )}
          </div>
        ),
      };
    }

    // Hallucination metrics
    if (metricName === 'hallucinations_v1') {
      return {
        expected: 'No hallucinations',
        actual: (
          <div>
            {metric.score !== null && metric.score < 0.5 ? (
              <span css={failureTextStyles}>Hallucinations detected</span>
            ) : (
              <span css={successTextStyles}>No hallucinations</span>
            )}
            {metric.score !== null && (
              <div css={scoreDisplayStyles}>Score: {metric.score.toFixed(2)}</div>
            )}
          </div>
        ),
      };
    }

    // Default: show score-based comparison
    return {
      expected: expectedInvocation ? 'See eval set' : 'N/A',
      actual: metric.score !== null ? `Score: ${metric.score.toFixed(2)}` : 'N/A',
    };
  };

  const metricResultsMap = new Map(metricResults.map(mr => [mr.metricName, mr]));
  const metricsToShow = selectedMetrics.length > 0 ? selectedMetrics : metricResults.map(mr => mr.metricName);

  const getStatusIcon = (status: string) => {
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
  };

  return (
    <div css={sectionContainerStyles}>
      <div css={sectionHeaderStyles}>
        <h3>Metrics Overview</h3>
        <span css={thresholdBadgeStyles}>Threshold: {threshold}</span>
      </div>

      <div css={metricsListStyles}>
        {metricsToShow.map((metricName) => {
          const result = metricResultsMap.get(metricName);

          // Show loading state for metrics being evaluated
          if (!result && isEvaluating) {
            return (
              <div key={metricName} css={metricCardStyles('var(--border-default)')}>
                <div css={metricCardHeaderStyles}>
                  <div css={metricNameContainerStyles}>
                    <Loader2 size={16} css={spinnerStyles} />
                    <span css={metricNameStyles}>{metricName}</span>
                  </div>
                  <div css={statusBadgeStyles('var(--text-secondary)')}>
                    Evaluating...
                  </div>
                </div>
              </div>
            );
          }

          if (!result) {
            return null;
          }

          const statusColor = getStatusColor(result.evalStatus);
          const StatusIcon = getStatusIcon(result.evalStatus);
          const comparison = getMetricComparison(result);

          return (
            <div key={metricName} css={metricCardStyles(statusColor)}>
              <div css={metricCardHeaderStyles}>
                <div css={metricNameContainerStyles}>
                  <StatusIcon size={16} css={statusIconStyles(statusColor)} />
                  <span css={metricNameStyles}>{result.metricName}</span>
                </div>
                <div css={statusBadgeStyles(statusColor)}>
                  {result.evalStatus}
                </div>
              </div>

              <div css={comparisonGridStyles}>
                <div css={comparisonColumnStyles}>
                  <div css={columnLabelStyles}>Expected</div>
                  <div css={columnContentStyles}>
                    {comparison.expected}
                  </div>
                </div>
                <div css={comparisonColumnStyles}>
                  <div css={columnLabelStyles}>Actual</div>
                  <div css={columnContentStyles}>
                    {comparison.actual}
                  </div>
                </div>
              </div>

              {result.error && (
                <div css={errorMessageStyles}>
                  <AlertCircle size={14} />
                  <span>{result.error}</span>
                </div>
              )}

              {result.perInvocationScores.length > 1 && (
                <div css={perInvocationScoresStyles}>
                  <div css={perInvocationLabelStyles}>Per-invocation scores:</div>
                  <div css={scoresListStyles}>
                    {result.perInvocationScores.map((score, idx) => (
                      <span key={idx} css={invocationScoreStyles}>
                        {score !== null ? score.toFixed(2) : 'N/A'}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

const sectionContainerStyles = css`
  margin-bottom: 24px;
`;

const sectionHeaderStyles = css`
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

const thresholdBadgeStyles = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  background: var(--bg-elevated);
  padding: 4px 12px;
  border-radius: 12px;
  border: 1px solid var(--border-default);
`;

const metricsListStyles = css`
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

const metricCardStyles = (borderColor: string) => css`
  background: var(--bg-elevated);
  border-radius: 8px;
  border: 1px solid var(--border-default);
  overflow: hidden;
  transition: all 0.2s ease;

  &:hover {
    border-color: ${borderColor};
    box-shadow: 0 0 20px ${borderColor}33;
    transform: translateY(-2px);
  }
`;

const metricCardHeaderStyles = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
`;

const metricNameContainerStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
`;

const metricNameStyles = css`
  font-family: var(--font-mono);
  font-size: 0.938rem;
  font-weight: 600;
  color: var(--text-primary);
`;

const statusIconStyles = (color: string) => css`
  color: ${color};
  flex-shrink: 0;
`;

const spinnerStyles = css`
  color: var(--text-secondary);
  animation: spin 1s linear infinite;

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }
`;

const statusBadgeStyles = (color: string) => css`
  padding: 4px 12px;
  background: ${color}22;
  border: 1px solid ${color};
  border-radius: 12px;
  color: ${color};
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.5px;
`;

const comparisonGridStyles = css`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border-default);
`;

const comparisonColumnStyles = css`
  padding: 16px;
  background: var(--bg-elevated);
  display: flex;
  flex-direction: column;
  gap: 8px;
`;

const columnLabelStyles = css`
  font-size: 0.688rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
`;

const columnContentStyles = css`
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--text-primary);
`;

const toolListStyles = css`
  display: flex;
  flex-direction: column;
  gap: 6px;
`;

const toolItemStyles = css`
  font-family: var(--font-mono);
  font-size: 0.813rem;
  color: var(--text-primary);
  padding: 6px 10px;
  background: var(--bg-primary);
  border-radius: 4px;
  border-left: 3px solid var(--accent-lime);
`;

const textContentStyles = css`
  white-space: pre-wrap;
  word-wrap: break-word;
  padding: 8px;
  background: var(--bg-primary);
  border-radius: 4px;
  font-size: 0.813rem;
  line-height: 1.5;
`;

const scoreDisplayStyles = css`
  margin-top: 8px;
  font-family: var(--font-mono);
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--accent-cyan);
`;

const successTextStyles = css`
  color: var(--status-success);
  font-weight: 600;
`;

const failureTextStyles = css`
  color: var(--status-failure);
  font-weight: 600;
`;

const emptyTextStyles = css`
  color: var(--text-secondary);
  font-style: italic;
  font-size: 0.813rem;
`;

const errorMessageStyles = css`
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--status-failure);
  font-size: 0.75rem;
  margin: 12px 16px;
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
`;

const perInvocationScoresStyles = css`
  margin: 12px 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-default);
`;

const perInvocationLabelStyles = css`
  font-size: 0.688rem;
  color: var(--text-secondary);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
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
  background: var(--bg-primary);
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-default);
`;
