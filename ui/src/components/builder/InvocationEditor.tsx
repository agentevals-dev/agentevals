import React, { useState } from 'react';
import { css } from '@emotion/react';
import { Input, Tag } from 'antd';
import type { Invocation, ToolCall, ToolResponse } from '../../lib/types';

interface InvocationEditorProps {
  invocation: Invocation;
  onChange: (invocation: Invocation) => void;
}

interface JsonParseResult {
  value: Record<string, unknown> | null;
  error: string | null;
}

interface ToolCallEditorProps {
  index: number;
  toolUse: ToolCall;
  toolResponse?: ToolResponse;
  onIdentityChange: (index: number, updates: Partial<ToolCall>) => void;
  onToolArgsChange: (index: number, args: Record<string, unknown>) => void;
  onToolResponseChange: (index: number, response: Record<string, unknown>) => void;
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

  const updateIntermediateData = (nextToolUses: ToolCall[], nextToolResponses: ToolResponse[]) => {
    const current = invocation.intermediateData || { toolUses: [], toolResponses: [] };
    onChange({
      ...invocation,
      intermediateData: {
        ...current,
        toolUses: nextToolUses,
        toolResponses: nextToolResponses,
      },
    });
  };

  const handleToolIdentityChange = (index: number, updates: Partial<ToolCall>) => {
    const nextToolUses = [...toolUses];
    const currentToolUse = nextToolUses[index];
    if (!currentToolUse) return;

    nextToolUses[index] = { ...currentToolUse, ...updates };

    const nextToolResponses = [...toolResponses];
    if (nextToolResponses[index]) {
      nextToolResponses[index] = {
        ...nextToolResponses[index],
        ...(updates.name !== undefined ? { name: updates.name } : {}),
        ...(updates.id !== undefined ? { id: updates.id } : {}),
      };
    }

    updateIntermediateData(nextToolUses, nextToolResponses);
  };

  const handleToolArgsChange = (index: number, args: Record<string, unknown>) => {
    const nextToolUses = [...toolUses];
    const currentToolUse = nextToolUses[index];
    if (!currentToolUse) return;

    nextToolUses[index] = { ...currentToolUse, args };
    updateIntermediateData(nextToolUses, [...toolResponses]);
  };

  const handleToolResponseChange = (index: number, response: Record<string, unknown>) => {
    const nextToolResponses = [...toolResponses];
    const currentToolResponse = nextToolResponses[index];
    if (!currentToolResponse) return;

    nextToolResponses[index] = { ...currentToolResponse, response };
    updateIntermediateData([...toolUses], nextToolResponses);
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
          <div css={toolListStyle}>
            {toolUses.map((toolUse, index) => (
              <ToolCallEditor
                key={`${toolUse.id || toolUse.name}-${index}`}
                index={index}
                toolUse={toolUse}
                toolResponse={toolResponses[index]}
                onIdentityChange={handleToolIdentityChange}
                onToolArgsChange={handleToolArgsChange}
                onToolResponseChange={handleToolResponseChange}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

const ToolCallEditor: React.FC<ToolCallEditorProps> = ({
  index,
  toolUse,
  toolResponse,
  onIdentityChange,
  onToolArgsChange,
  onToolResponseChange,
}) => {
  const [argsText, setArgsText] = useState(formatJsonObject(toolUse.args));
  const [responseText, setResponseText] = useState(formatJsonObject(toolResponse?.response));
  const [argsError, setArgsError] = useState<string | null>(null);
  const [responseError, setResponseError] = useState<string | null>(null);

  const handleArgsTextChange = (text: string) => {
    setArgsText(text);
    const parsed = parseJsonObject(text);
    setArgsError(parsed.error);
    if (parsed.value) {
      onToolArgsChange(index, parsed.value);
    }
  };

  const handleResponseTextChange = (text: string) => {
    setResponseText(text);
    const parsed = parseJsonObject(text);
    setResponseError(parsed.error);
    if (parsed.value) {
      onToolResponseChange(index, parsed.value);
    }
  };

  return (
    <div css={toolEditorStyle}>
      <div css={toolHeaderStyle}>
        <Tag color="green">Tool Call {index + 1}</Tag>
      </div>

      <div css={identityGridStyle}>
        <label css={fieldLabelStyle}>
          <span>Tool Name</span>
          <Input
            value={toolUse.name}
            onChange={(e) => onIdentityChange(index, { name: e.target.value })}
            placeholder="Tool name"
          />
        </label>
        <label css={fieldLabelStyle}>
          <span>Call ID</span>
          <Input
            value={toolUse.id || ''}
            onChange={(e) => onIdentityChange(index, { id: e.target.value || undefined })}
            placeholder="Optional call ID"
          />
        </label>
      </div>

      <label css={fieldLabelStyle}>
        <span>Arguments</span>
        <Input.TextArea
          value={argsText}
          onChange={(e) => handleArgsTextChange(e.target.value)}
          rows={4}
          status={argsError ? 'error' : undefined}
        />
      </label>
      {argsError && <div css={validationTextStyle}>{argsError}</div>}

      {toolResponse && (
        <label css={fieldLabelStyle}>
          <span>Response</span>
          <Input.TextArea
            value={responseText}
            onChange={(e) => handleResponseTextChange(e.target.value)}
            rows={4}
            status={responseError ? 'error' : undefined}
          />
        </label>
      )}
      {responseError && <div css={validationTextStyle}>{responseError}</div>}
    </div>
  );
};

function formatJsonObject(value: Record<string, unknown> | undefined): string {
  return JSON.stringify(value || {}, null, 2);
}

function parseJsonObject(text: string): JsonParseResult {
  try {
    const parsed = JSON.parse(text.trim() || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { value: null, error: 'Enter a JSON object.' };
    }
    return { value: parsed, error: null };
  } catch {
    return { value: null, error: 'Enter valid JSON.' };
  }
}

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

const toolListStyle = css`
  display: flex;
  flex-direction: column;
  gap: 12px;
`;

const toolEditorStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 4px;
  padding: 12px;
`;

const toolHeaderStyle = css`
  display: flex;
  align-items: center;
  margin-bottom: 12px;
`;

const identityGridStyle = css`
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  margin-bottom: 12px;
`;

const fieldLabelStyle = css`
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 0.75rem;
  color: var(--text-secondary);

  &:last-child {
    margin-bottom: 0;
  }
`;

const validationTextStyle = css`
  margin-top: -6px;
  margin-bottom: 12px;
  font-size: 0.75rem;
  color: var(--status-failure);
`;
