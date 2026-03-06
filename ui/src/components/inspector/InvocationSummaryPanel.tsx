import React from 'react';
import { css } from '@emotion/react';
import { MessageCircle, Wrench, MessageSquare } from 'lucide-react';
import type { Invocation } from '../../lib/types';

interface InvocationSummaryPanelProps {
  invocations: Invocation[];
  selectedInvocationId: string | null;
  onSelectInvocation: (id: string) => void;
}

export const InvocationSummaryPanel: React.FC<InvocationSummaryPanelProps> = ({
  invocations,
  selectedInvocationId,
  onSelectInvocation,
}) => {
  // Helper to extract text from content parts
  const getTextFromParts = (parts: any[]) => {
    return parts.filter(p => p.text).map(p => p.text).join(' ');
  };

  // Helper to truncate text
  const truncateText = (text: string, maxLength: number = 200) => {
    if (!text || text.length === 0) return 'No content';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
  };

  if (invocations.length === 0) {
    return (
      <div css={panelContainerStyles}>
        <div css={panelHeaderStyles}>
          <h2>Invocation Summary</h2>
        </div>
        <div css={emptyStateStyles}>
          <MessageCircle size={32} />
          <p>No invocations found</p>
        </div>
      </div>
    );
  }

  return (
    <div css={panelContainerStyles}>
      <div css={panelHeaderStyles}>
        <h2>Invocation Summary</h2>
        <span css={countBadgeStyles}>{invocations.length}</span>
      </div>

      <div css={panelContentStyles}>
        {invocations.map((invocation, idx) => {
          const isSelected = invocation.invocationId === selectedInvocationId;
          const userText = getTextFromParts(invocation.userContent.parts);
          const responseText = getTextFromParts(invocation.finalResponse.parts);
          const toolCount = invocation.intermediateData?.toolUses?.length || 0;

          return (
            <div
              key={invocation.invocationId || idx}
              css={invocationCardStyles(isSelected)}
              onClick={() => onSelectInvocation(invocation.invocationId)}
            >
              <div css={cardHeaderStyles}>
                <span css={invocationNumberStyles}>Invocation #{idx + 1}</span>
                {invocation.invocationId && (
                  <span css={invocationIdStyles}>{invocation.invocationId.substring(0, 8)}</span>
                )}
              </div>

              <div css={cardSectionStyles}>
                <div css={sectionLabelStyles}>
                  <MessageCircle size={14} css={iconStyles('var(--accent-purple)')} />
                  <span>User Input</span>
                </div>
                <div css={previewTextStyles}>
                  {truncateText(userText)}
                </div>
              </div>

              {toolCount > 0 && (
                <div css={cardSectionStyles}>
                  <div css={sectionLabelStyles}>
                    <Wrench size={14} css={iconStyles('var(--accent-lime)')} />
                    <span>Tools</span>
                  </div>
                  <div css={toolBadgeStyles}>
                    {toolCount} tool{toolCount !== 1 ? 's' : ''} used
                  </div>
                </div>
              )}

              <div css={cardSectionStyles}>
                <div css={sectionLabelStyles}>
                  <MessageSquare size={14} css={iconStyles('var(--accent-cyan)')} />
                  <span>Response</span>
                </div>
                <div css={previewTextStyles}>
                  {truncateText(responseText)}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const panelContainerStyles = css`
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-surface);
`;

const panelHeaderStyles = css`
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-elevated);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;

  h2 {
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }
`;

const countBadgeStyles = css`
  display: flex;
  align-items: center;
  padding: 4px 12px;
  background: rgba(0, 217, 255, 0.1);
  border: 1px solid var(--accent-cyan);
  border-radius: 12px;
  color: var(--accent-cyan);
  font-size: 0.75rem;
  font-weight: 600;
`;

const panelContentStyles = css`
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;

  &::-webkit-scrollbar {
    width: 8px;
  }

  &::-webkit-scrollbar-track {
    background: var(--bg-primary);
  }

  &::-webkit-scrollbar-thumb {
    background: var(--border-default);
    border-radius: 4px;
  }

  &::-webkit-scrollbar-thumb:hover {
    background: var(--accent-cyan);
  }
`;

const emptyStateStyles = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 32px;
  text-align: center;
  color: var(--text-secondary);
  gap: 12px;

  svg {
    opacity: 0.3;
  }

  p {
    font-size: 0.875rem;
    margin: 0;
    color: var(--text-primary);
  }
`;

const invocationCardStyles = (selected: boolean) => css`
  background: var(--bg-elevated);
  border: ${selected ? '2px' : '1px'} solid ${selected ? 'var(--accent-cyan)' : 'var(--border-default)'};
  border-radius: 8px;
  padding: 16px;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: ${selected ? 'var(--glow-info)' : 'none'};

  &:hover {
    border-color: var(--accent-cyan);
    transform: translateY(-2px);
    box-shadow: ${selected ? 'var(--glow-info)' : '0 4px 16px rgba(0, 217, 255, 0.15)'};
  }
`;

const cardHeaderStyles = css`
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-default);
`;

const invocationNumberStyles = css`
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--text-primary);
`;

const invocationIdStyles = css`
  font-family: var(--font-mono);
  font-size: 0.688rem;
  color: var(--text-secondary);
  background: var(--bg-primary);
  padding: 2px 8px;
  border-radius: 4px;
`;

const cardSectionStyles = css`
  margin-bottom: 12px;

  &:last-child {
    margin-bottom: 0;
  }
`;

const sectionLabelStyles = css`
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;

  span {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
`;

const iconStyles = (color: string) => css`
  color: ${color};
  flex-shrink: 0;
`;

const previewTextStyles = css`
  font-size: 0.813rem;
  line-height: 1.4;
  color: var(--text-primary);
  padding: 8px;
  background: var(--bg-primary);
  border-radius: 4px;
  white-space: pre-wrap;
  word-wrap: break-word;
`;

const toolBadgeStyles = css`
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  background: rgba(124, 255, 107, 0.1);
  border: 1px solid var(--accent-lime);
  border-radius: 12px;
  color: var(--accent-lime);
  font-size: 0.75rem;
  font-weight: 600;
`;
