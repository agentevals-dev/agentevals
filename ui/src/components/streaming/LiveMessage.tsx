import { useState } from 'react';

interface UserMessageProps {
  text: string;
}

export function UserMessage({ text }: UserMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
    }}>
      <div style={{
        fontSize: '10px',
        color: '#7C3AED',
        marginBottom: '6px',
        fontWeight: 700,
        textTransform: 'uppercase' as const,
        letterSpacing: '0.5px',
      }}>
        User
      </div>
      <div style={{
        fontSize: '14px',
        color: 'var(--text-primary)',
        lineHeight: '1.6',
      }}>
        {text}
      </div>
    </div>
  );
}

interface ToolCallMessageProps {
  name: string;
  /** The arguments as JSON; ``undefined`` when not recorded. */
  args: unknown;
}

function formatArgs(args: unknown): string {
  if (args === undefined || args === null) return '';
  if (typeof args === 'object' && !Array.isArray(args)) {
    return Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ');
  }
  return JSON.stringify(args);
}

export function ToolCallMessage({ name, args }: ToolCallMessageProps) {
  const argsStr = formatArgs(args);

  return (
    <div style={{
      marginBottom: '2px',
      paddingLeft: '12px',
    }}>
      <div style={{
        fontSize: '12px',
        color: '#A855F7',
        fontFamily: 'monospace',
        fontWeight: 500,
      }}>
        → {name}({argsStr})
      </div>
    </div>
  );
}

const TRUNCATE_LENGTH = 120;

interface ToolResultMessageProps {
  response: unknown;
  isError?: boolean;
}

export function ToolResultMessage({ response, isError }: ToolResultMessageProps) {
  const [expanded, setExpanded] = useState(false);
  const jsonStr = JSON.stringify(response);
  const needsTruncation = jsonStr.length > TRUNCATE_LENGTH;
  const displayStr = !expanded && needsTruncation
    ? jsonStr.slice(0, TRUNCATE_LENGTH) + '\u2026'
    : JSON.stringify(response, null, expanded ? 2 : undefined);

  const color = isError ? '#ef4444' : '#10b981';

  return (
    <div style={{
      marginBottom: '12px',
      paddingLeft: '12px',
    }}>
      <div
        style={{
          fontSize: '11px',
          color,
          fontFamily: 'monospace',
          fontWeight: 400,
          cursor: needsTruncation ? 'pointer' : 'default',
          whiteSpace: expanded ? 'pre-wrap' : 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
        onClick={needsTruncation ? () => setExpanded(e => !e) : undefined}
        title={needsTruncation ? (expanded ? 'Click to collapse' : 'Click to expand') : undefined}
      >
        ← {displayStr}
      </div>
    </div>
  );
}

interface AgentMessageProps {
  text: string;
  isStreaming?: boolean;
}

export function AgentMessage({ text, isStreaming }: AgentMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
    }}>
      <div style={{
        fontSize: '10px',
        color: '#10b981',
        marginBottom: '6px',
        fontWeight: 700,
        textTransform: 'uppercase' as const,
        letterSpacing: '0.5px',
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
      }}>
        Agent
        {isStreaming && <span style={{
          display: 'inline-block',
          width: '6px',
          height: '6px',
          borderRadius: '50%',
          background: '#10b981',
          animation: 'pulse 1.5s ease-in-out infinite',
        }} />}
      </div>
      <div style={{
        fontSize: '14px',
        color: 'var(--text-primary)',
        lineHeight: '1.6',
      }}>
        {text || <em style={{ color: 'var(--text-tertiary)' }}>Thinking...</em>}
      </div>
    </div>
  );
}

interface TurnErrorMessageProps {
  errorType?: string | null;
}

export function TurnErrorMessage({ errorType }: TurnErrorMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
      fontSize: '12px',
      fontWeight: 600,
      color: 'var(--status-failure)',
    }}>
      Turn failed{errorType ? `: ${errorType}` : ''}
    </div>
  );
}
