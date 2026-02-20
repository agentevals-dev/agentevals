import React from 'react';
import { css } from '@emotion/react';
import { Input, Tag } from 'antd';
import type { Invocation } from '../../lib/types';

interface InvocationEditorProps {
  invocation: Invocation;
  onChange: (invocation: Invocation) => void;
}

export const InvocationEditor: React.FC<InvocationEditorProps> = ({
  invocation,
  onChange,
}) => {
  const userText = invocation.userContent?.parts?.[0]?.text || '';
  const responseText = invocation.finalResponse?.parts?.[0]?.text || '';
  const toolUses = invocation.intermediateData?.toolUses || [];
  const toolResponses = invocation.intermediateData?.toolResponses || [];

  const handleUserContentChange = (text: string) => {
    const updated = { ...invocation };
    updated.userContent = {
      role: 'user',
      parts: [{ text }],
    };
    onChange(updated);
  };

  const handleFinalResponseChange = (text: string) => {
    const updated = { ...invocation };
    updated.finalResponse = {
      role: 'model',
      parts: [{ text }],
    };
    onChange(updated);
  };

  return (
    <div css={containerStyle}>
      <div css={sectionStyle}>
        <div css={labelStyle}>
          <Tag color="purple">User Input</Tag>
          <span>Extracted from first call_llm span</span>
        </div>
        <Input.TextArea
          value={userText}
          onChange={(e) => handleUserContentChange(e.target.value)}
          rows={2}
          placeholder="User input text"
        />
      </div>

      <div css={sectionStyle}>
        <div css={labelStyle}>
          <Tag color="cyan">Final Response</Tag>
          <span>Extracted from last call_llm span</span>
        </div>
        <Input.TextArea
          value={responseText}
          onChange={(e) => handleFinalResponseChange(e.target.value)}
          rows={3}
          placeholder="Model response text"
        />
      </div>

      {toolUses.length > 0 && (
        <div css={sectionStyle}>
          <div css={labelStyle}>
            <Tag color="lime">Tool Trajectory</Tag>
            <span>{toolUses.length} tool call{toolUses.length !== 1 ? 's' : ''}</span>
          </div>
          <div css={jsonDisplayStyle}>
            <pre>{JSON.stringify({ toolUses, toolResponses }, null, 2)}</pre>
          </div>
        </div>
      )}
    </div>
  );
};

const containerStyle = css`
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: 4px;
  padding: 12px;
  margin-bottom: 12px;

  &:last-child {
    margin-bottom: 0;
  }
`;

const sectionStyle = css`
  margin-bottom: 12px;

  &:last-child {
    margin-bottom: 0;
  }
`;

const labelStyle = css`
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 0.75rem;
  color: var(--text-secondary);
`;

const jsonDisplayStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 4px;
  padding: 8px;

  pre {
    margin: 0;
    font-size: 0.75rem;
    font-family: monospace;
    color: var(--text-secondary);
    overflow-x: auto;
  }
`;
