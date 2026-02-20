import React from 'react';
import { css } from '@emotion/react';
import { FileJson, Play } from 'lucide-react';
import { useTraceContext } from '../../context/TraceContext';

export const WelcomeView: React.FC = () => {
  const { actions } = useTraceContext();

  const handleGetStarted = () => {
    actions.setCurrentView('builder');
  };

  const handleExpertMode = () => {
    actions.setCurrentView('upload');
  };

  return (
    <div css={containerStyle}>
      <div css={contentStyle}>
        <div css={headerStyle}>
          <h1>trace-eval</h1>
          <p>Evaluate agent behavior from pre-recorded OpenTelemetry traces</p>
        </div>

        <div css={questionStyle}>
          <h2>What would you like to do?</h2>
        </div>

        <div css={buttonsContainerStyle}>
          <button css={optionButtonStyle} onClick={handleGetStarted}>
            <div css={iconWrapperStyle}>
              <FileJson size={48} />
            </div>
            <h3>I am just getting started</h3>
            <p>Use Builder to turn your traces into EvalSets</p>
          </button>

          <button css={optionButtonStyle} onClick={handleExpertMode}>
            <div css={iconWrapperStyle}>
              <Play size={48} />
            </div>
            <h3>I know what I am doing</h3>
            <p>Upload your traces and EvalSets, then evaluate them</p>
          </button>
        </div>
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
  max-width: 1000px;
  width: 100%;
`;

const headerStyle = css`
  text-align: center;
  margin-bottom: 48px;

  h1 {
    font-size: 3rem;
    font-weight: 700;
    margin: 0 0 16px 0;
    background: linear-gradient(135deg, var(--accent-cyan) 0%, var(--accent-purple) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  p {
    font-size: 1.125rem;
    color: var(--text-secondary);
    margin: 0;
  }
`;

const questionStyle = css`
  text-align: center;
  margin-bottom: 40px;

  h2 {
    font-size: 1.5rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }
`;

const buttonsContainerStyle = css`
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 24px;

  @media (max-width: 768px) {
    grid-template-columns: 1fr;
  }
`;

const optionButtonStyle = css`
  background: var(--bg-surface);
  border: 2px solid var(--border-default);
  border-radius: 16px;
  padding: 40px 32px;
  cursor: pointer;
  transition: all 0.3s ease;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;

  &:hover {
    transform: translateY(-8px);
    border-color: var(--accent-cyan);
    box-shadow: 0 16px 48px rgba(0, 217, 255, 0.2);
    background: var(--bg-elevated);
  }

  h3 {
    font-size: 1.5rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  p {
    font-size: 1rem;
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }
`;

const iconWrapperStyle = css`
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background: linear-gradient(135deg, rgba(0, 217, 255, 0.1) 0%, rgba(138, 43, 226, 0.1) 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-cyan);
  transition: all 0.3s ease;

  button:hover & {
    transform: scale(1.1);
    background: linear-gradient(135deg, rgba(0, 217, 255, 0.2) 0%, rgba(138, 43, 226, 0.2) 100%);
  }
`;
