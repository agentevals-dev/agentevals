import React from 'react';
import { css } from '@emotion/react';
import { Wrench, CheckCircle, AlertCircle } from 'lucide-react';
import type { ToolCall, ToolResponse } from '../../lib/types';
import { pairToolResponses } from '../../lib/toolPairing';

interface ToolCallListProps {
  toolUses: ToolCall[];
  toolResponses: ToolResponse[];
  onSelectTool?: (index: number, type: 'use' | 'response') => void;
}

export const ToolCallList: React.FC<ToolCallListProps> = ({
  toolUses,
  toolResponses,
  onSelectTool,
}) => {
  if (toolUses.length === 0) {
    return (
      <div css={emptyStateStyles}>
        <Wrench size={32} />
        <p>No tools used in this invocation</p>
      </div>
    );
  }

  const paired = pairToolResponses(toolUses, toolResponses);

  return (
    <div css={listContainerStyles}>
      {toolUses.map((toolUse, index) => {
        const toolResponse = paired[index];
        const hasError = toolResponse?.response?.isError === true;

        return (
          <div key={toolUse.id || index} css={toolItemStyles}>
            <div
              css={toolUseStyles}
              onClick={() => onSelectTool?.(index, 'use')}
            >
              <div css={toolHeaderStyles}>
                <div css={iconContainerStyles('#7cff6b')}>
                  <Wrench size={14} />
                </div>
                <span css={toolNameStyles}>{toolUse.name}</span>
                {toolUse.id && (
                  <span css={toolIdStyles}>{toolUse.id}</span>
                )}
              </div>

              {Object.keys(toolUse.args).length > 0 && (
                <div css={argsContainerStyles}>
                  <div css={argsLabelStyles}>Arguments:</div>
                  <pre css={jsonCodeStyles}>
                    {JSON.stringify(toolUse.args, null, 2)}
                  </pre>
                </div>
              )}
            </div>

            {toolResponse && (
              <div
                css={toolResponseStyles(hasError)}
                onClick={() => onSelectTool?.(index, 'response')}
              >
                <div css={responseHeaderStyles}>
                  <div css={iconContainerStyles(hasError ? '#ff5757' : '#ff9f43')}>
                    {hasError ? <AlertCircle size={14} /> : <CheckCircle size={14} />}
                  </div>
                  <span css={responseNameStyles}>
                    {hasError ? 'Error Response' : 'Response'}
                  </span>
                </div>

                <div css={responseContentStyles}>
                  <pre css={jsonCodeStyles}>
                    {JSON.stringify(toolResponse.response, null, 2)}
                  </pre>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

const listContainerStyles = css`
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

const emptyStateStyles = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px;
  color: var(--text-secondary);
  gap: 12px;

  svg {
    opacity: 0.3;
  }

  p {
    font-size: 0.875rem;
    margin: 0;
  }
`;

const toolItemStyles = css`
  display: flex;
  flex-direction: column;
  gap: 8px;
`;

const toolUseStyles = css`
  background: var(--bg-primary);
  border-radius: 4px;
  border-left: 3px solid #7cff6b;
  padding: 12px;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--bg-surface);
    box-shadow: 0 0 12px rgba(124, 255, 107, 0.2);
  }
`;

const toolResponseStyles = (hasError?: boolean) => css`
  background: var(--bg-primary);
  border-radius: 4px;
  border-left: 3px solid ${hasError ? '#ff5757' : '#ff9f43'};
  padding: 12px;
  margin-left: 16px;
  cursor: pointer;
  transition: all 0.2s ease;

  &:hover {
    background: var(--bg-surface);
    box-shadow: 0 0 12px ${hasError ? 'rgba(255, 87, 87, 0.2)' : 'rgba(255, 159, 67, 0.2)'};
  }
`;

const toolHeaderStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
`;

const responseHeaderStyles = css`
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
`;

const iconContainerStyles = (color: string) => css`
  display: flex;
  align-items: center;
  color: ${color};
`;

const toolNameStyles = css`
  font-family: var(--font-mono);
  font-size: 0.813rem;
  font-weight: 600;
  color: var(--text-primary);
`;

const responseNameStyles = css`
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
`;

const toolIdStyles = css`
  font-family: var(--font-mono);
  font-size: 0.688rem;
  color: var(--text-secondary);
  background: var(--bg-surface);
  padding: 2px 6px;
  border-radius: 3px;
`;

const argsContainerStyles = css`
  margin-top: 8px;
`;

const argsLabelStyles = css`
  font-size: 0.688rem;
  color: var(--text-secondary);
  margin-bottom: 4px;
`;

const responseContentStyles = css`
  margin-top: 8px;
`;

const jsonCodeStyles = css`
  font-family: var(--font-mono);
  font-size: 0.75rem;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--bg-elevated);
  padding: 8px;
  border-radius: 4px;
  margin: 0;
  overflow-x: auto;

  &::-webkit-scrollbar {
    height: 6px;
  }

  &::-webkit-scrollbar-track {
    background: var(--bg-primary);
  }

  &::-webkit-scrollbar-thumb {
    background: var(--border-default);
    border-radius: 3px;
  }

  &::-webkit-scrollbar-thumb:hover {
    background: var(--accent-primary);
  }
`;
