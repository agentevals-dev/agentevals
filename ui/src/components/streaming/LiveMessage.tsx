interface UserMessageProps {
  text: string;
  timestamp: number;
}

export function UserMessage({ text }: UserMessageProps) {
  return (
    <div style={{
      marginBottom: '16px',
    }}>
      <div style={{
        fontSize: '10px',
        color: '#3b82f6',
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
  args: Record<string, any>;
  timestamp: number;
}

export function ToolCallMessage({ name, args }: ToolCallMessageProps) {
  const argsStr = Object.keys(args).length > 0
    ? Object.keys(args).map(k => `${k}=${JSON.stringify(args[k])}`).join(', ')
    : '';

  return (
    <div style={{
      marginBottom: '12px',
      paddingLeft: '12px',
    }}>
      <div style={{
        fontSize: '12px',
        color: '#8b5cf6',
        fontFamily: 'monospace',
        fontWeight: 500,
      }}>
        → {name}({argsStr})
      </div>
    </div>
  );
}

interface AgentMessageProps {
  text: string;
  timestamp: number;
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
