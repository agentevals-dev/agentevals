import React from 'react';
import { css } from '@emotion/react';
import { Eye, EyeOff } from 'lucide-react';

interface ExecutionToggleButtonProps {
  showExecution: boolean;
  onToggle: (show: boolean) => void;
  spanCount?: number;
}

export const ExecutionToggleButton: React.FC<ExecutionToggleButtonProps> = ({
  showExecution,
  onToggle,
  spanCount,
}) => {
  return (
    <button
      css={buttonStyles(showExecution)}
      onClick={() => onToggle(!showExecution)}
      title={showExecution ? 'Hide span execution details' : 'Show span execution details'}
    >
      {showExecution ? <EyeOff size={16} /> : <Eye size={16} />}
      <span>{showExecution ? 'Hide Execution' : 'Show Execution'}</span>
      {!showExecution && spanCount !== undefined && spanCount > 0 && (
        <span css={badgeStyles}>{spanCount}</span>
      )}
    </button>
  );
};

const buttonStyles = (active: boolean) => css`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: ${active ? 'var(--accent-cyan)' : 'var(--bg-elevated)'};
  color: ${active ? 'var(--bg-primary)' : 'var(--text-primary)'};
  border: 1px solid ${active ? 'var(--accent-cyan)' : 'var(--border-default)'};
  border-radius: 6px;
  font-family: var(--font-display);
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--accent-cyan);
    color: var(--bg-primary);
    border-color: var(--accent-cyan);
    box-shadow: var(--glow-info);
  }

  svg {
    flex-shrink: 0;
  }
`;

const badgeStyles = css`
  background: var(--accent-purple);
  color: var(--bg-primary);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 0.688rem;
  font-weight: 700;
  margin-left: 4px;
`;
