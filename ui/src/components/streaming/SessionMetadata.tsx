interface SessionMetadataProps {
  session: {
    sessionId: string;
    traceId: string;
    metadata: Record<string, any>;
    startedAt: string;
    status: 'active' | 'complete';
  };
  liveStats: {
    totalInputTokens: number;
    totalOutputTokens: number;
  };
}

export function SessionMetadata({ session, liveStats }: SessionMetadataProps) {
  const totalTokens = liveStats.totalInputTokens + liveStats.totalOutputTokens;

  return (
    <div style={{
      padding: '12px 0',
      borderBottom: '1px solid var(--border)',
      marginBottom: '12px',
      display: 'flex',
      gap: '24px',
      alignItems: 'center',
      flexWrap: 'wrap',
    }}>
      {totalTokens > 0 && (
        <div>
          <div style={{
            fontSize: '10px',
            color: 'var(--text-tertiary)',
            marginBottom: '4px',
            fontWeight: 600,
            textTransform: 'uppercase' as const,
          }}>
            Tokens
          </div>
          <div style={{
            fontSize: '14px',
            fontWeight: 600,
            color: '#10b981',
          }}>
            {totalTokens.toLocaleString()}
            <span style={{
              fontSize: '11px',
              color: 'var(--text-tertiary)',
              marginLeft: '6px',
            }}>
              (↓{liveStats.totalInputTokens.toLocaleString()} ↑{liveStats.totalOutputTokens.toLocaleString()})
            </span>
          </div>
        </div>
      )}

      {Object.keys(session.metadata).length > 0 && Object.entries(session.metadata).map(([key, value]) => (
        <div key={key}>
          <div style={{
            fontSize: '10px',
            color: 'var(--text-tertiary)',
            marginBottom: '4px',
            fontWeight: 600,
            textTransform: 'uppercase' as const,
          }}>
            {key}
          </div>
          <div style={{
            fontSize: '14px',
            fontWeight: 600,
            color: 'var(--text-primary)',
          }}>
            {String(value)}
          </div>
        </div>
      ))}
    </div>
  );
}
