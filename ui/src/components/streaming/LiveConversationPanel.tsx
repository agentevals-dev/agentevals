import { useRef, useEffect, useState } from 'react';
import { UserMessage, ToolCallMessage, ToolResultMessage, AgentMessage, TurnErrorMessage } from './LiveMessage';
import { isFailed, jsonValue } from '../../lib/liveSessions';
import type { TurnView } from '../../lib/liveSessions';

interface LiveConversationPanelProps {
  turns: TurnView[];
  isActive: boolean;
}

export function TurnMessages({ turn }: { turn: TurnView }) {
  return (
    <>
      {turn.userText && <UserMessage text={turn.userText} />}
      {turn.toolCalls.map((call, i) => (
        <div key={call.spanId || call.callId || i}>
          <ToolCallMessage name={call.name} args={jsonValue(call.arguments)} />
          {call.result && <ToolResultMessage response={jsonValue(call.result)} isError={call.isError} />}
        </div>
      ))}
      {turn.answerText && <AgentMessage text={turn.answerText} />}
      {isFailed(turn) && <TurnErrorMessage errorType={turn.errorType} />}
    </>
  );
}

export function LiveConversationPanel({ turns, isActive }: LiveConversationPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [turns, autoScroll]);

  return (
    <div>
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

      <div
        ref={containerRef}
        style={{
          maxHeight: '600px',
          overflowY: 'auto',
        }}
      >
        {turns.map(turn => <TurnMessages key={`${turn.traceId}:${turn.spanId}`} turn={turn} />)}
      </div>
    </div>
  );
}
