import React, { useCallback } from 'react';
import { css } from '@emotion/react';
import { Upload, FileJson } from 'lucide-react';
import { message } from 'antd';
import { loadJaegerTraces } from '../../lib/trace-loader';
import { generateEvalSetFromTraces } from '../../lib/evalset-builder';
import { useTraceContext } from '../../context/TraceContext';

export const TraceUploadZone: React.FC = () => {
  const { actions } = useTraceContext();

  const handleFileUpload = useCallback(async (file: File) => {
    try {
      const content = await file.text();
      const traces = await loadJaegerTraces(content);

      if (traces.length === 0) {
        message.error('No valid traces found in the file');
        return;
      }

      const filename = file.name.replace('.json', '');
      const evalSet = generateEvalSetFromTraces(traces, filename);

      actions.setBuilderEvalSet(evalSet);
      message.success(`Loaded ${traces.length} trace(s) and generated EvalSet!`);
    } catch (error) {
      console.error('Failed to load trace:', error);
      message.error('Failed to load trace file. Please ensure it is a valid Jaeger JSON file.');
    }
  }, [actions]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.json')) {
      handleFileUpload(file);
    } else {
      message.error('Please upload a .json file');
    }
  }, [handleFileUpload]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      handleFileUpload(file);
    }
  }, [handleFileUpload]);

  const handleBackToWelcome = () => {
    actions.setCurrentView('upload');
  };

  return (
    <div css={containerStyle}>
      <div css={contentStyle}>
        <div css={headerStyle}>
          <h1>EvalSet Builder</h1>
          <p>Upload a trace file to get started</p>
        </div>

        <div
          css={dropZoneStyle}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
        >
          <input
            type="file"
            id="trace-file-input"
            accept=".json"
            onChange={handleFileInput}
            css={fileInputStyle}
          />
          <label htmlFor="trace-file-input" css={labelStyle}>
            <div css={iconWrapperStyle}>
              <FileJson size={64} />
            </div>
            <h2>Drop your trace file here</h2>
            <p>or click to browse</p>
            <div css={supportedFormatsStyle}>
              <Upload size={16} />
              <span>Supports Jaeger JSON format (.json)</span>
            </div>
          </label>
        </div>

        <button css={backButtonStyle} onClick={handleBackToWelcome}>
          ← Back to Main Page
        </button>
      </div>
    </div>
  );
};

const containerStyle = css`
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-primary);
  padding: 24px;
`;

const contentStyle = css`
  max-width: 600px;
  width: 100%;
`;

const headerStyle = css`
  text-align: center;
  margin-bottom: 40px;

  h1 {
    font-size: 2rem;
    font-weight: 700;
    margin: 0 0 12px 0;
    color: var(--text-primary);
  }

  p {
    font-size: 1rem;
    color: var(--text-secondary);
    margin: 0;
  }
`;

const dropZoneStyle = css`
  position: relative;
  background: var(--bg-surface);
  border: 2px dashed var(--border-default);
  border-radius: 16px;
  padding: 64px 32px;
  transition: all 0.3s ease;
  cursor: pointer;

  &:hover {
    border-color: var(--accent-cyan);
    background: var(--bg-elevated);
    transform: translateY(-4px);
    box-shadow: 0 16px 48px rgba(0, 217, 255, 0.15);
  }
`;

const fileInputStyle = css`
  display: none;
`;

const labelStyle = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  cursor: pointer;

  h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  p {
    font-size: 1rem;
    color: var(--text-secondary);
    margin: 0;
  }
`;

const iconWrapperStyle = css`
  width: 120px;
  height: 120px;
  border-radius: 50%;
  background: linear-gradient(135deg, rgba(0, 217, 255, 0.1) 0%, rgba(138, 43, 226, 0.1) 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-cyan);
  transition: all 0.3s ease;

  label:hover & {
    transform: scale(1.1);
    background: linear-gradient(135deg, rgba(0, 217, 255, 0.2) 0%, rgba(138, 43, 226, 0.2) 100%);
  }
`;

const supportedFormatsStyle = css`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: var(--bg-primary);
  border-radius: 8px;
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin-top: 8px;
`;

const backButtonStyle = css`
  display: block;
  margin: 24px auto 0;
  padding: 12px 24px;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  color: var(--text-primary);
  font-size: 0.875rem;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    border-color: var(--accent-cyan);
    color: var(--accent-cyan);
    background: var(--bg-elevated);
  }
`;
