import type { SessionSummary } from '../../generated/agentevals/api/v1alpha1/views_pb';
import { tokens } from '../../lib/liveSessions';

interface SessionMetadataProps {
  summary: SessionSummary;
  provider?: string;
}

function MetadataItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{
        fontSize: '10px',
        color: 'var(--text-tertiary)',
        marginBottom: '4px',
        fontWeight: 600,
        textTransform: 'uppercase' as const,
      }}>
        {label}
      </div>
      <div style={{
        fontSize: '14px',
        fontWeight: 600,
        color: 'var(--text-primary)',
      }}>
        {children}
      </div>
    </div>
  );
}

export function SessionMetadata({ summary, provider }: SessionMetadataProps) {
  const { input, output, cacheRead = 0, cacheWrite = 0 } = tokens(summary.totals?.usage);
  const total = (input ?? 0) + (output ?? 0);
  const totals = summary.totals;

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
      {summary.agent && <MetadataItem label="Agent">{summary.agent}</MetadataItem>}

      {summary.agentVersion && <MetadataItem label="Version">{summary.agentVersion}</MetadataItem>}

      {total > 0 && (
        <MetadataItem label="Tokens">
          <span style={{ color: '#10b981' }}>
            {total.toLocaleString()}
          </span>
          <span style={{
            fontSize: '11px',
            color: 'var(--text-tertiary)',
            marginLeft: '6px',
          }}>
            (↓{(input ?? 0).toLocaleString()} ↑{(output ?? 0).toLocaleString()})
          </span>
          {totals && totals.modelCallsWithUsage < totals.modelCalls && (
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginLeft: '6px' }}>
              measured in {totals.modelCallsWithUsage} of {totals.modelCalls} calls
            </span>
          )}
        </MetadataItem>
      )}

      {provider && <MetadataItem label="Provider">{provider}</MetadataItem>}

      {(cacheWrite > 0 || cacheRead > 0) && (
        <MetadataItem label="Cache Tokens">
          <span style={{ color: '#f59e0b' }}>
            {cacheRead > 0 && `${cacheRead.toLocaleString()} read`}
            {cacheWrite > 0 && cacheRead > 0 && ' / '}
            {cacheWrite > 0 && `${cacheWrite.toLocaleString()} created`}
          </span>
        </MetadataItem>
      )}

      {summary.conversationId && <MetadataItem label="Conversation">{summary.conversationId}</MetadataItem>}

      <MetadataItem label="Spans">{summary.spanCount.toLocaleString()}</MetadataItem>
    </div>
  );
}
