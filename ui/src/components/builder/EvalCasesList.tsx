import React from 'react';
import { css } from '@emotion/react';
import { Button } from 'antd';
import { Plus } from 'lucide-react';
import { EvalCaseCard } from './EvalCaseCard';
import type { EvalCase } from '../../lib/types';

interface EvalCasesListProps {
  evalCases: EvalCase[];
  onUpdateCase: (index: number, evalCase: EvalCase) => void;
  onRemoveCase: (index: number) => void;
  onAddCase: () => void;
}

export const EvalCasesList: React.FC<EvalCasesListProps> = ({
  evalCases,
  onUpdateCase,
  onRemoveCase,
  onAddCase,
}) => {
  return (
    <div css={containerStyle}>
      <div css={headerStyle}>
        <h2>Eval Cases ({evalCases.length})</h2>
        <Button
          type="primary"
          icon={<Plus size={16} />}
          onClick={onAddCase}
        >
          Add Case
        </Button>
      </div>

      <div css={casesListStyle}>
        {evalCases.length === 0 ? (
          <div css={emptyStateStyle}>
            <p>No eval cases yet. Add one to get started.</p>
          </div>
        ) : (
          evalCases.map((evalCase, idx) => (
            <EvalCaseCard
              key={idx}
              evalCase={evalCase}
              index={idx}
              onChange={(updated) => onUpdateCase(idx, updated)}
              onRemove={() => onRemoveCase(idx)}
            />
          ))
        )}
      </div>
    </div>
  );
};

const containerStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;
`;

const headerStyle = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;

  h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }
`;

const casesListStyle = css`
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

const emptyStateStyle = css`
  text-align: center;
  padding: 40px 20px;
  color: var(--text-secondary);

  p {
    margin: 0;
    font-size: 0.875rem;
  }
`;
