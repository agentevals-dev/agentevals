import React from 'react';
import { css } from '@emotion/react';
import { ArrowLeft, Copy, Check, FileJson } from 'lucide-react';
import { Button, message } from 'antd';
import { truncateTraceId } from '../../lib/utils';
import type { TraceResult } from '../../lib/types';
import { useTraceContext } from '../../context/TraceContext';
import { loadJaegerTraces } from '../../lib/trace-loader';
import { generateEvalSetFromTraces } from '../../lib/evalset-builder';

interface InspectorHeaderProps {
  traceResult: TraceResult;
  onBack: () => void;
}

export const InspectorHeader: React.FC<InspectorHeaderProps> = ({
  traceResult,
  onBack,
}) => {
  const [copied, setCopied] = React.useState(false);
  const { state, actions } = useTraceContext();

  const handleCopyTraceId = () => {
    navigator.clipboard.writeText(traceResult.traceId);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleCreateEvalSet = async () => {
    try {
      let matchingTrace = null;
      let matchingFilename = '';

      for (const file of state.traceFiles) {
        const content = await file.text();
        const traces = await loadJaegerTraces(content);
        const found = traces.find(t => t.traceId === traceResult.traceId);

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

  const overallStatus = traceResult.metricResults.every(
    m => m.evalStatus === 'PASSED' || m.evalStatus === 'NOT_EVALUATED'
  ) ? 'PASSED' : traceResult.metricResults.some(
    m => m.evalStatus === 'FAILED'
  ) ? 'FAILED' : 'PARTIAL';

  return (
    <div css={headerStyles}>
      <div css={leftSectionStyles}>
        <button onClick={onBack} css={backButtonStyles}>
          <ArrowLeft size={20} />
          <span>Back to Dashboard</span>
        </button>

        <div css={dividerStyles} />

        <div css={traceInfoStyles}>
          <div css={traceIdStyles}>
            <span css={labelStyles}>TRACE:</span>
            <code css={traceIdCodeStyles}>
              {truncateTraceId(traceResult.traceId)}
            </code>
            <button
              onClick={handleCopyTraceId}
              css={copyButtonStyles}
              title="Copy full trace ID"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>

          <div css={metadataStyles}>
            <span css={metadataItemStyles}>
              <span css={metadataLabelStyles}>Invocations:</span>
              <span css={metadataValueStyles}>{traceResult.numInvocations}</span>
            </span>
            <span css={metadataItemStyles}>
              <span css={metadataLabelStyles}>Metrics:</span>
              <span css={metadataValueStyles}>{traceResult.metricResults.length}</span>
            </span>
            {traceResult.conversionWarnings.length > 0 && (
              <span css={metadataItemStyles}>
                <span css={[metadataLabelStyles, warningLabelStyles]}>Warnings:</span>
                <span css={[metadataValueStyles, warningValueStyles]}>
                  {traceResult.conversionWarnings.length}
                </span>
              </span>
            )}
          </div>
        </div>
      </div>

      <div css={rightSectionStyles}>
        <Button
          icon={<FileJson size={16} />}
          onClick={handleCreateEvalSet}
        >
          Use as EvalSet Template
        </Button>
        <div css={statusBadgeStyles(overallStatus)}>
          <div css={statusDotStyles(overallStatus)} />
          {overallStatus}
        </div>
      </div>
    </div>
  );
};

const headerStyles = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
  position: sticky;
  top: 0;
  z-index: 100;
`;

const leftSectionStyles = css`
  display: flex;
  align-items: center;
  gap: 16px;
`;

const backButtonStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: transparent;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  color: var(--text-primary);
  font-family: var(--font-display);
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--bg-elevated);
    border-color: var(--accent-cyan);
    color: var(--accent-cyan);
  }
`;

const dividerStyles = css`
  width: 1px;
  height: 32px;
  background: var(--border-default);
`;

const traceInfoStyles = css`
  display: flex;
  flex-direction: column;
  gap: 8px;
`;

const traceIdStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const labelStyles = css`
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.5px;
`;

const traceIdCodeStyles = css`
  font-family: var(--font-mono);
  font-size: 0.875rem;
  color: var(--accent-cyan);
  padding: 4px 8px;
  background: var(--bg-primary);
  border-radius: 4px;
`;

const copyButtonStyles = css`
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--bg-elevated);
    color: var(--accent-cyan);
  }
`;

const metadataStyles = css`
  display: flex;
  gap: 16px;
`;

const metadataItemStyles = css`
  display: flex;
  gap: 6px;
  align-items: center;
`;

const metadataLabelStyles = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
`;

const metadataValueStyles = css`
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-primary);
`;

const warningLabelStyles = css`
  color: var(--status-warning);
`;

const warningValueStyles = css`
  color: var(--status-warning);
`;

const rightSectionStyles = css`
  display: flex;
  align-items: center;
  gap: 12px;
`;

const statusBadgeStyles = (status: string) => css`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 6px;
  font-size: 0.875rem;
  font-weight: 600;
  letter-spacing: 0.5px;

  ${status === 'PASSED' && `
    background: rgba(124, 255, 107, 0.1);
    color: var(--status-success);
    box-shadow: 0 0 16px rgba(124, 255, 107, 0.3);
  `}

  ${status === 'FAILED' && `
    background: rgba(255, 87, 87, 0.1);
    color: var(--status-failure);
    box-shadow: 0 0 16px rgba(255, 87, 87, 0.3);
  `}

  ${status === 'PARTIAL' && `
    background: rgba(255, 179, 71, 0.1);
    color: var(--status-warning);
    box-shadow: 0 0 16px rgba(255, 179, 71, 0.3);
  `}
`;

const statusDotStyles = (status: string) => css`
  width: 8px;
  height: 8px;
  border-radius: 50%;

  ${status === 'PASSED' && `background: var(--status-success);`}
  ${status === 'FAILED' && `background: var(--status-failure);`}
  ${status === 'PARTIAL' && `background: var(--status-warning);`}
`;
