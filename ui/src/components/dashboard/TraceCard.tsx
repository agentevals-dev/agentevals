import React from 'react';
import { css } from '@emotion/react';
import { motion } from 'framer-motion';
import { ChevronRight, AlertTriangle, FileJson } from 'lucide-react';
import { message, Button } from 'antd';
import type { TraceResult } from '../../lib/types';
import { truncateTraceId, getStatusColor, getStatusGlow, copyToClipboard } from '../../lib/utils';
import { MetricScoreCard } from './MetricScoreCard';
import { useTraceContext } from '../../context/TraceContext';
import { loadJaegerTraces } from '../../lib/trace-loader';
import { generateEvalSetFromTraces } from '../../lib/evalset-builder';

interface TraceCardProps {
  traceResult: TraceResult;
  threshold: number;
  onClick: () => void;
}

const cardStyle = css`
  background-color: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  padding: 20px;
  cursor: pointer;
  transition: all 0.2s ease;
  position: relative;
  overflow: hidden;

  &:hover {
    transform: translateY(-4px);
    box-shadow: 0 8px 32px rgba(0, 217, 255, 0.15);
    border-color: var(--accent-cyan);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 16px;
  }

  .trace-id {
    font-family: var(--font-mono);
    font-size: 14px;
    color: var(--text-secondary);
    cursor: pointer;

    &:hover {
      color: var(--accent-cyan);
    }
  }

  .status-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    position: relative;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.6;
    }
  }

  .metrics-section {
    margin: 16px 0;
  }

  .metrics-title {
    font-size: 12px;
    color: var(--text-muted);
    text-transform: uppercase;
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }

  .metrics-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .metadata-section {
    display: flex;
    gap: 16px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border-default);
  }

  .metadata-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .view-details {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--accent-cyan);
    font-size: 14px;
    font-weight: 500;
  }

  .actions-section {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 16px;
  }

  .warnings {
    margin-top: 12px;
    padding: 12px;
    background-color: rgba(255, 179, 71, 0.1);
    border-left: 3px solid var(--status-warning);
    border-radius: 4px;
  }

  .warning-text {
    font-size: 12px;
    color: var(--status-warning);
  }
`;

export const TraceCard: React.FC<TraceCardProps> = ({ traceResult, threshold, onClick }) => {
  const { traceId, numInvocations, metricResults, conversionWarnings } = traceResult;
  const { state, actions } = useTraceContext();

  const passedMetrics = metricResults.filter((m) => m.evalStatus === 'PASSED').length;
  const failedMetrics = metricResults.filter((m) => m.evalStatus === 'FAILED').length;
  const totalMetrics = metricResults.length;

  const overallStatus =
    failedMetrics === 0 ? 'PASSED' :
    passedMetrics === 0 ? 'FAILED' :
    'PARTIAL';

  const statusColor = getStatusColor(overallStatus as any);
  const statusGlow = getStatusGlow(overallStatus as any);

  const handleCopyTraceId = (e: React.MouseEvent) => {
    e.stopPropagation();
    copyToClipboard(traceId);
  };

  const handleCreateEvalSet = async (e: React.MouseEvent) => {
    e.stopPropagation();

    try {
      let matchingTrace = null;
      let matchingFilename = '';

      for (const file of state.traceFiles) {
        const content = await file.text();
        const traces = await loadJaegerTraces(content);
        const found = traces.find(t => t.traceId === traceId);

        if (found) {
          matchingTrace = found;
          matchingFilename = file.name.replace('.json', '');
          break;
        }
      }

      if (!matchingTrace) {
        message.error('Could not find trace in uploaded files');
        return;
      }

      const evalSet = generateEvalSetFromTraces([matchingTrace], matchingFilename);
      actions.setBuilderEvalSet(evalSet);
      actions.setCurrentView('builder');
      message.success('EvalSet created! Edit and save when ready.');
    } catch (error) {
      console.error('Failed to create evalset:', error);
      message.error('Failed to create evalset from trace');
    }
  };

  return (
    <motion.div
      css={cardStyle}
      onClick={onClick}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="card-header">
        <div className="trace-id" onClick={handleCopyTraceId} title="Click to copy">
          TRACE: {truncateTraceId(traceId)}
        </div>
        <div
          className="status-badge"
          style={{
            backgroundColor: `${statusColor}20`,
            border: `1px solid ${statusColor}60`,
            boxShadow: statusGlow
          }}
        >
          <div
            className="status-dot"
            style={{ backgroundColor: statusColor }}
          />
          <span style={{ color: statusColor }}>
            {overallStatus} ({passedMetrics}/{totalMetrics})
          </span>
        </div>
      </div>

      <div className="metrics-section">
        <div className="metrics-title">Metrics</div>
        <div className="metrics-list">
          {metricResults.map((metric) => (
            <MetricScoreCard
              key={metric.metricName}
              metricResult={metric}
              threshold={threshold}
              compact
            />
          ))}
        </div>
      </div>

      <div className="metadata-section">
        <div className="metadata-item">
          ⏱️ {numInvocations} invocation{numInvocations !== 1 ? 's' : ''}
        </div>
        {conversionWarnings.length > 0 && (
          <div className="metadata-item">
            <AlertTriangle size={14} />
            {conversionWarnings.length} warning{conversionWarnings.length !== 1 ? 's' : ''}
          </div>
        )}
      </div>

      {conversionWarnings.length > 0 && (
        <div className="warnings">
          <div className="warning-text">
            {conversionWarnings[0]}
            {conversionWarnings.length > 1 && ` (+${conversionWarnings.length - 1} more)`}
          </div>
        </div>
      )}

      <div className="actions-section">
        <Button
          icon={<FileJson size={14} />}
          onClick={handleCreateEvalSet}
          size="small"
        >
          Create EvalSet
        </Button>
        <div className="view-details">
          View Details <ChevronRight size={16} />
        </div>
      </div>
    </motion.div>
  );
};
