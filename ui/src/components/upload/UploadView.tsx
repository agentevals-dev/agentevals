import React from 'react';
import { Button, Select, Slider } from 'antd';
import { css } from '@emotion/react';
import { Play, FileJson, ArrowLeft } from 'lucide-react';
import { FileDropZone } from './FileDropZone';
import { MetricSelector } from './MetricSelector';
import { useTraceContext } from '../../context/TraceContext';

const uploadViewStyle = css`
  max-width: 1600px;
  margin: 0 auto;
  padding: 20px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;

  .header {
    margin-bottom: 16px;
    position: relative;
  }

  .header-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .nav-button {
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid var(--border-default);
    background: var(--bg-surface);
    color: var(--text-primary);
    font-size: 0.875rem;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 8px;

    &:hover {
      border-color: var(--accent-cyan);
      color: var(--accent-cyan);
      background: var(--bg-elevated);
    }
  }

  .title {
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--text-primary);
    margin-bottom: 4px;
    text-align: center;
  }

  .subtitle {
    font-size: 0.9rem;
    color: var(--text-secondary);
    text-align: center;
  }

  .upload-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 12px;
    margin-bottom: 12px;
  }

  .section {
    background-color: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .section-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 8px;
  }

  .metrics-section {
    grid-column: 1 / -1;
    flex: 1;
    min-height: 0;
  }

  .settings-section {
    grid-column: 1 / -1;
  }

  .setting-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .setting-label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-primary);
  }

  .setting-hint {
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 0px;
  }

  .actions {
    display: flex;
    justify-content: center;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-default);
  }

  .run-button {
    height: 44px;
    font-size: 15px;
    font-weight: 600;
    padding: 0 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    gap: 10px;
    background-color: var(--accent-cyan) !important;
    border-color: var(--accent-cyan) !important;
    color: #000 !important;
    box-shadow: 0 0 20px rgba(0, 217, 255, 0.3);
    transition: all 0.3s ease;

    &:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 0 30px rgba(0, 217, 255, 0.5);
      background-color: var(--accent-cyan) !important;
      border-color: var(--accent-cyan) !important;
    }

    &:disabled {
      opacity: 0.4;
      box-shadow: none;
      transform: none;
      background-color: var(--bg-surface) !important;
      border-color: var(--border-default) !important;
      color: var(--text-secondary) !important;
    }
  }

  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid var(--bg-elevated);
    border-top-color: var(--accent-cyan);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 1024px) {
    .upload-grid {
      grid-template-columns: 1fr;
    }
  }
`;

const JUDGE_MODELS = [
  'gemini-2.5-flash',
  'gemini-2.0-flash',
  'claude-3.5-sonnet',
  'gpt-4o',
];

export const UploadView: React.FC = () => {
  const { state, actions } = useTraceContext();

  const canRunEvaluation =
    state.traceFiles.length > 0 &&
    state.selectedMetrics.length > 0 &&
    !state.isLoadingMetadata &&
    state.traceMetadata.size > 0;

  return (
    <div css={uploadViewStyle}>
      <div className="header">
        <div className="header-nav">
          <button
            className="nav-button"
            onClick={() => actions.setCurrentView(state.evaluationOrigin === 'streaming' ? 'streaming' : 'welcome')}
          >
            <ArrowLeft size={16} />
            {state.evaluationOrigin === 'streaming' ? 'Back to Live View' : 'Back to Welcome'}
          </button>
          <div style={{ display: 'flex', gap: '8px' }}>
            {state.annotationQueues.some(q => q.items.length > 0) && (
              <button
                className="nav-button"
                onClick={() => actions.setCurrentView('annotation-queue')}
                style={{ borderColor: 'rgba(139, 92, 246, 0.4)', color: '#8b5cf6' }}
              >
                Annotation Queues
              </button>
            )}
            <button
              className="nav-button"
              onClick={() => actions.setCurrentView('builder')}
            >
              <FileJson size={16} />
              Open EvalSet Builder
            </button>
          </div>
        </div>
        <h1 className="title">agentevals</h1>
        <p className="subtitle">
          Evaluate agent behavior from OpenTelemetry traces without re-running agents
        </p>
      </div>

      <div className="upload-grid">
        {state.evaluationOrigin === 'streaming' ? (
          <>
            <div className="section">
              <div className="section-title">Trace Files</div>
              <div style={{
                padding: '10px',
                backgroundColor: 'var(--bg-elevated)',
                borderRadius: '4px',
                border: '1px solid var(--border-default)'
              }}>
                <div style={{ color: 'var(--text-primary)', fontSize: '12px', fontWeight: 600, marginBottom: '6px' }}>
                  {state.traceFiles.length} file(s) loaded from live session:
                </div>
                {state.traceFiles.map((file, idx) => (
                  <div
                    key={idx}
                    style={{
                      color: 'var(--text-secondary)',
                      fontSize: '11px',
                      padding: '3px 6px',
                      backgroundColor: 'var(--bg-surface)',
                      borderRadius: '3px',
                      marginTop: idx > 0 ? '3px' : '0',
                      fontFamily: 'monospace'
                    }}
                  >
                    {file.name}
                  </div>
                ))}
                {state.isLoadingMetadata && (
                  <div style={{
                    marginTop: '8px',
                    padding: '6px',
                    backgroundColor: 'var(--bg-surface)',
                    borderRadius: '3px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    color: 'var(--accent-cyan)',
                    fontSize: '11px'
                  }}>
                    <div className="spinner" />
                    Extracting trace metadata...
                  </div>
                )}
                {!state.isLoadingMetadata && state.traceMetadata.size > 0 && (
                  <div style={{
                    marginTop: '8px',
                    padding: '6px',
                    backgroundColor: 'var(--bg-surface)',
                    borderRadius: '3px',
                    color: 'var(--status-success)',
                    fontSize: '11px'
                  }}>
                    ✓ {state.traceMetadata.size} trace(s) ready
                  </div>
                )}
              </div>
            </div>

            <div className="section">
              <div className="section-title">Eval Set (Optional)</div>
              {state.evalSetFile ? (
                <div style={{
                  padding: '10px',
                  backgroundColor: 'var(--bg-elevated)',
                  borderRadius: '4px',
                  border: '1px solid var(--border-default)'
                }}>
                  <div style={{ color: 'var(--text-primary)', fontSize: '12px', fontWeight: 600, marginBottom: '6px' }}>
                    Eval set file:
                  </div>
                  <div style={{
                    color: 'var(--text-secondary)',
                    fontSize: '11px',
                    padding: '3px 6px',
                    backgroundColor: 'var(--bg-surface)',
                    borderRadius: '3px',
                    fontFamily: 'monospace'
                  }}>
                    {state.evalSetFile.name}
                  </div>
                </div>
              ) : (
                <div style={{ color: 'var(--text-secondary)', fontSize: '12px', padding: '10px 0' }}>
                  No golden eval set loaded.
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="section">
              <div className="section-title">Trace Files</div>
              <FileDropZone
                title="Drop trace files here"
                description="Upload one or more Jaeger JSON trace files"
                multiple
                onChange={actions.setTraceFiles}
              />
              {state.traceFiles.length > 0 && (
                <div style={{
                  marginTop: 12,
                  padding: '10px',
                  backgroundColor: 'var(--bg-elevated)',
                  borderRadius: '4px',
                  border: '1px solid var(--border-default)'
                }}>
                  <div style={{
                    color: 'var(--text-primary)',
                    fontSize: '12px',
                    fontWeight: 600,
                    marginBottom: '6px'
                  }}>
                    {state.traceFiles.length} file(s) selected:
                  </div>
                  {state.traceFiles.map((file, idx) => (
                    <div
                      key={idx}
                      style={{
                        color: 'var(--text-secondary)',
                        fontSize: '11px',
                        padding: '3px 6px',
                        backgroundColor: 'var(--bg-surface)',
                        borderRadius: '3px',
                        marginTop: idx > 0 ? '3px' : '0',
                        fontFamily: 'monospace'
                      }}
                    >
                      {file.name}
                    </div>
                  ))}
                  {state.isLoadingMetadata && (
                    <div style={{
                      marginTop: '8px',
                      padding: '6px',
                      backgroundColor: 'var(--bg-surface)',
                      borderRadius: '3px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '8px',
                      color: 'var(--accent-cyan)',
                      fontSize: '11px'
                    }}>
                      <div className="spinner" />
                      Extracting trace metadata...
                    </div>
                  )}
                  {!state.isLoadingMetadata && state.traceMetadata.size > 0 && (
                    <div style={{
                      marginTop: '8px',
                      padding: '6px',
                      backgroundColor: 'var(--bg-surface)',
                      borderRadius: '3px',
                      color: 'var(--status-success)',
                      fontSize: '11px'
                    }}>
                      ✓ {state.traceMetadata.size} trace(s) ready
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="section">
              <div className="section-title">Eval Set (Optional)</div>
              <FileDropZone
                title="Drop eval set file here"
                description="Upload golden eval set JSON for comparison"
                onChange={(files) => actions.setEvalSet(files[0] || null)}
              />
              {state.evalSetFile && (
                <div style={{
                  marginTop: 12,
                  padding: '10px',
                  backgroundColor: 'var(--bg-elevated)',
                  borderRadius: '4px',
                  border: '1px solid var(--border-default)'
                }}>
                  <div style={{
                    color: 'var(--text-primary)',
                    fontSize: '12px',
                    fontWeight: 600,
                    marginBottom: '6px'
                  }}>
                    Eval set file:
                  </div>
                  <div style={{
                    color: 'var(--text-secondary)',
                    fontSize: '11px',
                    padding: '3px 6px',
                    backgroundColor: 'var(--bg-surface)',
                    borderRadius: '3px',
                    fontFamily: 'monospace'
                  }}>
                    {state.evalSetFile.name}
                  </div>
                </div>
              )}
            </div>
          </>
        )}

        <div className="section metrics-section">
          <div className="section-title">Metrics Configuration</div>
          <MetricSelector
            selectedMetrics={state.selectedMetrics}
            onToggleMetric={actions.toggleMetric}
            loadFromAPI={true}
          />
        </div>

        <div className="section settings-section">
          <div className="section-title">Evaluation Settings</div>

          <div className="setting-item">
            <label className="setting-label">Judge Model</label>
            <Select
              value={state.judgeModel}
              onChange={actions.setJudgeModel}
              options={JUDGE_MODELS.map((model) => ({ label: model, value: model }))}
              style={{ width: '100%' }}
              size="small"
            />
            <span className="setting-hint">
              LLM for judge-based metrics
            </span>
          </div>

          <div className="setting-item" style={{ marginTop: 10 }}>
            <label className="setting-label">Pass Threshold: {state.threshold}</label>
            <Slider
              min={0}
              max={1}
              step={0.05}
              value={state.threshold}
              onChange={actions.setThreshold}
            />
            <span className="setting-hint">
              Minimum score to pass
            </span>
          </div>
        </div>
      </div>

      <div className="actions">
        <Button
          type="primary"
          size="large"
          className="run-button"
          onClick={actions.runEvaluation}
          disabled={!canRunEvaluation}
          loading={state.isEvaluating || state.isLoadingMetadata}
        >
          <Play size={20} />
          {state.isEvaluating
            ? (state.progressMessage || 'Running Evaluation...')
            : state.isLoadingMetadata
            ? 'Loading Traces...'
            : 'Run Evaluation'}
        </Button>
      </div>
    </div>
  );
};
