import React, { useState } from 'react';
import { css } from '@emotion/react';
import { Input, Button } from 'antd';
import { Trash2, ChevronDown, ChevronRight } from 'lucide-react';
import { InvocationEditor } from './InvocationEditor';
import type { EvalCase } from '../../lib/types';

interface EvalCaseCardProps {
  evalCase: EvalCase;
  index: number;
  onChange: (evalCase: EvalCase) => void;
  onRemove: () => void;
}

export const EvalCaseCard: React.FC<EvalCaseCardProps> = ({
  evalCase,
  index,
  onChange,
  onRemove,
}) => {
  const [expanded, setExpanded] = useState(true);

  return (
    <div css={cardStyle}>
      <div css={headerStyle} onClick={() => setExpanded(!expanded)}>
        <div css={titleStyle}>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <span>Case {index + 1}: {evalCase.eval_id}</span>
          <span css={invocationCountStyle}>
            {evalCase.conversation.length} invocation{evalCase.conversation.length !== 1 ? 's' : ''}
          </span>
        </div>
        <Button
          danger
          size="small"
          icon={<Trash2 size={14} />}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          Remove
        </Button>
      </div>

      {expanded && (
        <div css={contentStyle}>
          <div css={fieldStyle}>
            <label>Eval ID</label>
            <Input
              value={evalCase.eval_id}
              onChange={(e) =>
                onChange({ ...evalCase, eval_id: e.target.value })
              }
            />
          </div>

          <div css={invocationsSection}>
            <h4>Conversation</h4>
            {evalCase.conversation.length === 0 ? (
              <div css={emptyConversationStyle}>
                <p>No invocations in this eval case</p>
              </div>
            ) : (
              evalCase.conversation.map((inv, invIdx) => (
                <InvocationEditor
                  key={invIdx}
                  invocation={inv}
                  onChange={(updated) => {
                    const newConversation = [...evalCase.conversation];
                    newConversation[invIdx] = updated;
                    onChange({ ...evalCase, conversation: newConversation });
                  }}
                />
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
};

const cardStyle = css`
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  overflow: hidden;
`;

const headerStyle = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);

  &:hover {
    background: var(--bg-elevated);
  }
`;

const titleStyle = css`
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
  color: var(--text-primary);
`;

const invocationCountStyle = css`
  font-size: 0.75rem;
  color: var(--text-secondary);
  font-weight: normal;
`;

const contentStyle = css`
  padding: 16px;
`;

const fieldStyle = css`
  margin-bottom: 16px;

  label {
    display: block;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 8px;
  }
`;

const invocationsSection = css`
  h4 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 0 0 12px 0;
  }
`;

const emptyConversationStyle = css`
  text-align: center;
  padding: 20px;
  color: var(--text-secondary);

  p {
    margin: 0;
    font-size: 0.875rem;
  }
`;
