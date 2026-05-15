import React, { useRef, useEffect, useState } from 'react';
import { css } from '@emotion/react';
import { AlertTriangle, Download, X, Loader2 } from 'lucide-react';
import { useTraceContext } from '../../context/TraceContext';
import type { TraceState } from '../../context/TraceContext';
import { generateBugReport } from '../../api/client';
import { buildEvalConfig } from '../../lib/eval-config';
import { getConsoleLogs } from '../../lib/console-capture';
import { getNetworkErrors } from '../../lib/network-capture';

interface BugReportModalProps {
  onClose: () => void;
}

function serializeAppState(state: TraceState): Record<string, unknown> {
  const evalConfig = buildEvalConfig({
    selectedEvaluatorNames: state.selectedEvaluatorNames,
    judgeModel: state.judgeModel,
    threshold: state.threshold,
    trajectoryMatchType: state.trajectoryMatchType,
  });

  return {
    currentView: state.currentView,
    evaluationOrigin: state.evaluationOrigin,
    selectedTraceId: state.selectedTraceId,
    version: state.version,
    isEvaluating: state.isEvaluating,
    progressMessage: state.progressMessage,
    selectedEvaluatorNames: state.selectedEvaluatorNames,
    judgeModel: state.judgeModel,
    threshold: state.threshold,
    trajectoryMatchType: state.trajectoryMatchType,
    evalConfig,
    errors: state.errors,
    traceFileCount: state.traceFiles.length,
    hasEvalSetFile: state.evalSetFile !== null,
    apiKeyStatus: state.apiKeyStatus,
    streamingSessionCount: state.streamingSessions.size,
    streamingSessionIds: Array.from(state.streamingSessions.keys()),
    streamingSessionStatuses: Object.fromEntries(
      Array.from(state.streamingSessions.entries()).map(([id, s]) => [
        id,
        {
          status: s.status,
          spanCount: s.spans.length,
          invocationCount: s.invocations?.length ?? 0,
        },
      ]),
    ),
    tableRowCount: state.tableRows.size,
    resultCount: state.results.length,
    annotationQueueCount: state.annotationQueues.length,
    annotationQueues: state.annotationQueues.map((q) => ({
      id: q.id,
      name: q.name,
      itemCount: q.items.length,
    })),
  };
}

export const BugReportModal: React.FC<BugReportModalProps> = ({ onClose }) => {
  const { state } = useTraceContext();
  const modalRef = useRef<HTMLDivElement>(null);
  const [description, setDescription] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (modalRef.current && !modalRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [onClose]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  const handleGenerate = async () => {
    setIsGenerating(true);
    setError(null);

    try {
      const blob = await generateBugReport({
        user_description: description,
        browser_info: {
          userAgent: navigator.userAgent,
          url: location.href,
          screenWidth: screen.width,
          screenHeight: screen.height,
          windowWidth: window.innerWidth,
          windowHeight: window.innerHeight,
          language: navigator.language,
        },
        console_logs: getConsoleLogs(),
        app_state: serializeAppState(state),
        network_errors: getNetworkErrors(),
      });

      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `bug-report-${new Date().toISOString().replace(/[:.]/g, '-')}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate bug report');
    } finally {
      setIsGenerating(false);
    }
  };

  const sessionCount = state.streamingSessions.size;

  return (
    <div css={overlayStyle}>
      <div ref={modalRef} css={modalStyle}>
        <div css={headerStyle}>
          <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600 }}>
            Generate Bug Report
          </h3>
          <button onClick={onClose} css={closeButtonStyle}>
            <X size={16} />
          </button>
        </div>

        <div css={warningStyle}>
          <AlertTriangle size={16} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            This report may contain trace data including LLM prompts and
            responses. Review the downloaded ZIP before sharing.
          </span>
        </div>

        <div css={bodyStyle}>
          <label css={labelStyle}>
            Describe the issue
            <textarea
              css={textareaStyle}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What happened? What did you expect?"
              rows={3}
            />
          </label>

          <div css={includedStyle}>
            <div css={labelStyle}>What's included</div>
            <ul css={listStyle}>
              <li>Environment info (Python/OS/package versions)</li>
              <li>Backend logs (last 1000 entries)</li>
              <li>Browser console logs (last 500 entries)</li>
              <li>Network error log</li>
              <li>App state snapshot</li>
              {sessionCount > 0 && (
                <li>
                  Session data ({sessionCount} session{sessionCount !== 1 ? 's' : ''} — raw spans & logs)
                </li>
              )}
              <li>Temporary processing files</li>
            </ul>
          </div>

          {error && <div css={errorStyle}>{error}</div>}
        </div>

        <div css={footerStyle}>
          <button onClick={onClose} css={cancelButtonStyle}>
            Cancel
          </button>
          <button
            onClick={handleGenerate}
            css={generateButtonStyle}
            disabled={isGenerating}
          >
            {isGenerating ? (
              <>
                <Loader2 size={14} css={spinnerStyle} />
                Generating...
              </>
            ) : (
              <>
                <Download size={14} />
                Generate Report
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};

const overlayStyle = css`
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
`;

const modalStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  width: 480px;
  max-width: 90vw;
  max-height: 80vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
`;

const headerStyle = css`
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-default);
`;

const closeButtonStyle = css`
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
  display: flex;
  align-items: center;

  &:hover {
    color: var(--text-primary);
    background: var(--bg-elevated);
  }
`;

const warningStyle = css`
  display: flex;
  gap: 10px;
  padding: 12px 20px;
  background: rgba(245, 158, 11, 0.08);
  border-bottom: 1px solid rgba(245, 158, 11, 0.15);
  color: rgb(245, 158, 11);
  font-size: 0.8rem;
  line-height: 1.4;
`;

const bodyStyle = css`
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

const labelStyle = css`
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 6px;
`;

const textareaStyle = css`
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 0.8rem;
  padding: 10px 12px;
  resize: vertical;
  outline: none;

  &:focus {
    border-color: var(--accent-primary);
  }

  &::placeholder {
    color: var(--text-tertiary);
  }
`;

const includedStyle = css`
  background: var(--bg-elevated);
  border-radius: 8px;
  padding: 12px 16px;
`;

const listStyle = css`
  margin: 0;
  padding-left: 18px;
  font-size: 0.75rem;
  color: var(--text-tertiary);
  line-height: 1.8;
`;

const errorStyle = css`
  padding: 10px 12px;
  background: rgba(255, 87, 87, 0.1);
  border: 1px solid rgba(255, 87, 87, 0.2);
  border-radius: 8px;
  color: var(--status-failure);
  font-size: 0.8rem;
`;

const footerStyle = css`
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 16px 20px;
  border-top: 1px solid var(--border-default);
`;

const cancelButtonStyle = css`
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-display);

  &:hover {
    background: var(--bg-elevated);
    color: var(--text-primary);
  }
`;

const generateButtonStyle = css`
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 8px;
  border: none;
  background: var(--accent-primary);
  color: #000;
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-display);

  &:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
`;

const spinnerStyle = css`
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  animation: spin 1s linear infinite;
`;
