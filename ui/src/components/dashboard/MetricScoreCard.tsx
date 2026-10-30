import React from 'react';
import { css } from '@emotion/react';
import { motion } from 'framer-motion';
import { CheckCircle, XCircle, AlertCircle, Info, Minus } from 'lucide-react';
import type { MetricResult } from '../../lib/types';
import { getScoreColor } from '../../lib/utils';

interface MetricScoreCardProps {
  metricResult: MetricResult;
  threshold: number;
  compact?: boolean;
}

const cardStyle = css`
  &.full {
    background-color: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 16px;
  }

  &.compact {
    padding: 8px 0;
  }

  .metric-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }

  .metric-name {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-primary);
    font-weight: 500;
  }

  .metric-status {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 600;
  }

  .score-section {
    margin-top: 8px;
  }

  .score-value {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 6px;
  }

  .score-bar-container {
    width: 100%;
    height: 8px;
    background-color: var(--bg-surface);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
  }

  .score-bar {
    height: 100%;
    border-radius: 4px;
    transition: width 0.6s ease-out;
  }

  .per-invocation {
    margin-top: 6px;
    font-size: 11px;
    color: var(--text-muted);
  }

  .error-message {
    margin-top: 8px;
    padding: 8px;
    background-color: rgba(255, 87, 87, 0.1);
    border-left: 2px solid var(--status-failure);
    font-size: 12px;
    color: var(--status-failure);
    border-radius: 4px;
  }
`;

export const MetricScoreCard: React.FC<MetricScoreCardProps> = ({
  metricResult,
  threshold,
  compact = false,
}) => {
  const { metricName, score, evalStatus, perInvocationScores, error } = metricResult;
  const informational = evalStatus === 'INFORMATIONAL';

  const StatusIcon =
    evalStatus === 'PASSED' ? CheckCircle :
    evalStatus === 'FAILED' ? XCircle :
    evalStatus === 'ERROR' ? AlertCircle :
    informational ? Info :
    Minus;

  const statusColor =
    evalStatus === 'PASSED' ? 'var(--status-success)' :
    evalStatus === 'FAILED' ? 'var(--status-failure)' :
    evalStatus === 'ERROR' ? 'var(--status-warning)' :
    informational ? 'var(--text-secondary)' :
    'var(--text-muted)';

  const scoreColor = informational
    ? 'var(--text-secondary)'
    : score !== null ? getScoreColor(score, threshold) : 'var(--text-muted)';
  const scorePercentage = score !== null ? score * 100 : 0;

  return (
    <div css={cardStyle} className={compact ? 'compact' : 'full'}>
      <div className="metric-header">
        <div className="metric-name">{metricName}</div>
        <div className="metric-status" style={{ color: statusColor }}>
          <StatusIcon size={14} />
          <span>{evalStatus}</span>
        </div>
      </div>

      {score !== null && (
        <div className="score-section">
          <div className="score-value" style={{ color: scoreColor }}>
            {informational ? 'Value' : 'Score'}: {score.toFixed(3)}
          </div>
          {!informational && (
            <div className="score-bar-container">
              <motion.div
                className="score-bar"
                style={{ backgroundColor: scoreColor }}
                initial={{ width: 0 }}
                animate={{ width: `${scorePercentage}%` }}
                transition={{ duration: 0.8, ease: 'easeOut' }}
              />
            </div>
          )}
          {perInvocationScores.length > 0 && !compact && (
            <div className="per-invocation">
              Per-invocation: [{perInvocationScores.map(s => s?.toFixed(2) ?? 'null').join(', ')}]
            </div>
          )}
        </div>
      )}

      {error && !compact && (
        <div className="error-message">{error}</div>
      )}
    </div>
  );
};
