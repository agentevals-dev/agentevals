import { useRef, useEffect, useState, useMemo } from 'react';
import { UserMessage, ToolCallMessage, AgentMessage } from './LiveMessage';
import type { ConversationElement } from '../../lib/types';

export type { ConversationElement };

interface LiveConversationPanelProps {
  elements: ConversationElement[];
  isActive: boolean;
}

export function LiveConversationPanel({ elements, isActive }: LiveConversationPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const sortedElements = useMemo(
    () => [...elements].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0)),
    [elements],
  );

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [sortedElements, autoScroll]);

  return (
    <div>
      {sortedElements.length > 0 && (
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '8px',
        }}>
          <div style={{
            fontSize: '11px',
            fontWeight: 600,
            color: 'var(--text-tertiary)',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}>
            {isActive && <span style={{
              display: 'inline-block',
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: '#10b981',
              animation: 'pulse 2s ease-in-out infinite',
            }} />}
            CONVERSATION
          </div>

          <label style={{
            fontSize: '11px',
            color: 'var(--text-tertiary)',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            cursor: 'pointer',
          }}>
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
            />
            Auto-scroll
          </label>
        </div>
      )}

      <div
        ref={containerRef}
        style={{
          maxHeight: '600px',
          overflowY: 'auto',
        }}
      >
        {sortedElements.length === 0 && (
          <div style={{
            fontSize: '13px',
            color: 'var(--text-tertiary)',
            textAlign: 'center',
            padding: '24px',
          }}>
            Waiting for agent activity...
          </div>
        )}

        {sortedElements.map((element, idx) => {
          switch (element.type) {
            case 'user_input':
              return <UserMessage key={idx} text={element.data.text} timestamp={element.timestamp} />;
            case 'tool_call':
              return <ToolCallMessage
                key={idx}
                name={element.data.toolCall.name}
                args={element.data.toolCall.args}
                timestamp={element.timestamp}
              />;
            case 'agent_response':
              return <AgentMessage
                key={idx}
                text={element.data.text}
                timestamp={element.timestamp}
                isStreaming={false}
              />;
            default:
              return null;
          }
        })}
      </div>
    </div>
  );
}
