import React from 'react';
import { css } from '@emotion/react';
import { motion } from 'framer-motion';
import type { TraceResult } from '../../lib/types';
import { calculatePassRate, calculateAvgScore } from '../../lib/utils';

interface SummaryStatsProps {
  traceResults: TraceResult[];
}

const statsStyle = css`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 20px;
  margin-bottom: 32px;

  .stat-card {
    background-color: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: 12px;
    padding: 24px;
    text-align: center;
  }

  .stat-value {
    font-size: 2.5rem;
    font-weight: 700;
    color: var(--accent-cyan);
    margin-bottom: 8px;
    font-family: var(--font-mono);
  }

  .stat-label {
    font-size: 14px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .pass-rate-card {
    position: relative;
  }

  .pass-rate-circle {
    width: 120px;
    height: 120px;
    margin: 0 auto 12px;
    position: relative;
  }

  .circle-bg {
    fill: none;
    stroke: var(--bg-elevated);
    stroke-width: 12;
  }

  .circle-progress {
    fill: none;
    stroke: var(--status-success);
    stroke-width: 12;
    stroke-linecap: round;
    transform: rotate(-90deg);
    transform-origin: center;
    transition: stroke-dashoffset 0.8s ease-out;
  }

  .circle-text {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-size: 2rem;
    font-weight: 700;
    color: var(--status-success);
    font-family: var(--font-mono);
  }
`;

export const SummaryStats: React.FC<SummaryStatsProps> = ({ traceResults }) => {
  const allMetricResults = traceResults.flatMap((tr) => tr.metricResults);

  const totalTraces = traceResults.length;
  const passRate = calculatePassRate(allMetricResults);
  const avgScore = calculateAvgScore(allMetricResults);

  const passedTraces = traceResults.filter((tr) =>
    tr.metricResults.every((m) => m.evalStatus === 'PASSED')
  ).length;

  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  const passOffset = circumference - (passRate * circumference);

  return (
    <div css={statsStyle}>
      <motion.div
        className="stat-card"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
      >
        <div className="stat-value">{totalTraces}</div>
        <div className="stat-label">Total Traces</div>
      </motion.div>

      <motion.div
        className="stat-card pass-rate-card"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
      >
        <div className="pass-rate-circle">
          <svg width="120" height="120" viewBox="0 0 120 120">
            <circle
              className="circle-bg"
              cx="60"
              cy="60"
              r={radius}
            />
            <circle
              className="circle-progress"
              cx="60"
              cy="60"
              r={radius}
              strokeDasharray={circumference}
              strokeDashoffset={passOffset}
            />
          </svg>
          <div className="circle-text">{Math.round(passRate * 100)}%</div>
        </div>
        <div className="stat-label">
          Pass Rate ({passedTraces}/{totalTraces} traces)
        </div>
      </motion.div>

      <motion.div
        className="stat-card"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
      >
        <div className="stat-value">
          {avgScore !== null ? avgScore.toFixed(3) : 'N/A'}
        </div>
        <div className="stat-label">Average Score</div>
      </motion.div>
    </div>
  );
};
