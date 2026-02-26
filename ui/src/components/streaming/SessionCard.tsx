import { useState, useEffect } from 'react';
import { LiveConversationPanel } from './LiveConversationPanel';
import type { ConversationElement } from './LiveConversationPanel';
import { SessionMetadata } from './SessionMetadata';

interface Invocation {
  invocationId: string;
  userText: string;
  agentText: string;
  toolCalls: Array<{ name: string; args: any }>;
  modelInfo?: {
    models?: string[];
    inputTokens?: number;
    outputTokens?: number;
  };
}

interface SessionCardProps {
  session: {
    sessionId: string;
    traceId: string;
    evalSetId: string | null;
    spans: any[];
    status: 'active' | 'complete';
    metadata: Record<string, any>;
    invocations?: Invocation[];
    liveElements?: ConversationElement[];
    liveStats?: {
      totalInputTokens: number;
      totalOutputTokens: number;
    };
    startedAt?: string;
  };
  isSelected: boolean;
  onSelect: () => void;
  evaluationResult?: {
    metricResults: Array<{
      metricName: string;
      score: number;
      evalStatus: string;
    }>;
  };
}

export function SessionCard({ session, isSelected, onSelect, evaluationResult }: SessionCardProps) {
  const [expanded, setExpanded] = useState(false);

  const conversationElements = session.liveElements || [];
  const liveStats = session.liveStats || {
    totalInputTokens: 0,
    totalOutputTokens: 0,
  };

  useEffect(() => {
    if (session.status === 'active' && conversationElements.length > 0 && !expanded) {
      setExpanded(true);
    }
  }, [conversationElements.length, session.status, expanded]);

  const totalTokens = liveStats.totalInputTokens + liveStats.totalOutputTokens ||
    session.invocations?.reduce((sum, inv) => {
      const input = inv.modelInfo?.inputTokens || 0;
      const output = inv.modelInfo?.outputTokens || 0;
      return sum + input + output;
    }, 0) || 0;

  const modelName = session.metadata?.model ||
    session.invocations?.[0]?.modelInfo?.models?.[0] ||
    'Unknown';

  return (
    <div
      style={{
        background: 'var(--card-bg)',
        borderRadius: '12px',
        border: isSelected ? '2px solid #3b82f6' : '1px solid var(--border)',
        padding: '20px',
        transition: 'all 0.2s',
        boxShadow: isSelected ? '0 4px 12px rgba(59, 130, 246, 0.15)' : 'none',
      }}
    >
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'start',
        marginBottom: '16px',
      }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px', flexWrap: 'wrap' }}>
            <span style={{
              fontSize: '11px',
              fontWeight: 600,
              color: '#8b5cf6',
              background: 'rgba(139, 92, 246, 0.1)',
              padding: '4px 10px',
              borderRadius: '6px',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}>
              {modelName}
            </span>

            {session.invocations && session.invocations.length > 0 && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-tertiary)',
                background: 'var(--bg-primary)',
                padding: '4px 10px',
                borderRadius: '6px',
              }}>
                {session.invocations.length} turns
              </span>
            )}

            {totalTokens > 0 && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: '#10b981',
                background: 'rgba(16, 185, 129, 0.1)',
                padding: '4px 10px',
                borderRadius: '6px',
              }}>
                {totalTokens.toLocaleString()} tokens
              </span>
            )}
          </div>

          <h3 style={{
            fontSize: '16px',
            fontWeight: 600,
            color: 'var(--text-primary)',
            margin: '0 0 4px 0',
          }}>
            {session.sessionId}
          </h3>

          <p style={{
            fontSize: '12px',
            color: 'var(--text-tertiary)',
            fontFamily: 'monospace',
            margin: 0,
          }}>
            {session.traceId.slice(0, 16)}...
          </p>
        </div>

        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexShrink: 0 }}>
          {evaluationResult && (
            <div style={{ display: 'flex', gap: '4px' }}>
              {evaluationResult.metricResults.map((mr) => (
                <div
                  key={mr.metricName}
                  style={{
                    padding: '6px 10px',
                    borderRadius: '6px',
                    background: mr.evalStatus === 'PASSED'
                      ? 'rgba(34, 197, 94, 0.15)'
                      : 'rgba(239, 68, 68, 0.15)',
                    color: mr.evalStatus === 'PASSED' ? '#22c55e' : '#ef4444',
                    fontSize: '12px',
                    fontWeight: 700,
                  }}
                >
                  {mr.score != null ? mr.score.toFixed(2) : 'N/A'}
                </div>
              ))}
            </div>
          )}

          <button
            onClick={(e) => {
              e.stopPropagation();
              onSelect();
            }}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              background: isSelected ? '#3b82f6' : 'transparent',
              border: isSelected ? 'none' : '1.5px solid var(--border)',
              color: isSelected ? 'white' : 'var(--text-primary)',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all 0.2s',
              whiteSpace: 'nowrap',
            }}
          >
            {isSelected ? 'EvalSet' : 'Set as EvalSet'}
          </button>

          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            style={{
              padding: '8px 12px',
              borderRadius: '8px',
              background: expanded ? 'rgba(59, 130, 246, 0.1)' : 'transparent',
              border: '1.5px solid var(--border)',
              color: 'var(--text-primary)',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
          >
            {expanded ? '▲' : '▼'}
          </button>
        </div>
      </div>

      {expanded && session.startedAt && (totalTokens > 0 || Object.keys(session.metadata).length > 0) && (
        <SessionMetadata
          session={{
            sessionId: session.sessionId,
            traceId: session.traceId,
            metadata: session.metadata,
            startedAt: session.startedAt,
            status: session.status,
          }}
          liveStats={liveStats}
        />
      )}

      {expanded && conversationElements.length > 0 && (
        <LiveConversationPanel
          elements={conversationElements}
          isActive={session.status === 'active'}
        />
      )}

      {expanded && session.invocations && session.invocations.length > 0 && conversationElements.length === 0 && (
        <details style={{ marginTop: '16px' }}>
          <summary style={{
            fontSize: '11px',
            color: 'var(--text-tertiary)',
            cursor: 'pointer',
            fontWeight: 600,
            padding: '8px 0',
          }}>
            Show Invocations ({session.invocations.length})
          </summary>
        <div style={{
          borderTop: '1px solid var(--border)',
          paddingTop: '20px',
          marginTop: '4px',
        }}>
          {session.invocations.map((inv, idx) => (
            <div
              key={inv.invocationId}
              style={{
                marginBottom: '16px',
                padding: '16px',
                background: 'var(--bg-primary)',
                borderRadius: '10px',
                borderLeft: '3px solid #3b82f6',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <span style={{
                  fontSize: '11px',
                  color: '#3b82f6',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                }}>
                  Turn {idx + 1}
                </span>

                {inv.modelInfo && (inv.modelInfo.inputTokens || inv.modelInfo.outputTokens) && (
                  <div style={{ display: 'flex', gap: '6px' }}>
                    {inv.modelInfo.inputTokens && (
                      <span style={{
                        fontSize: '10px',
                        color: 'var(--text-tertiary)',
                        background: 'var(--card-bg)',
                        padding: '3px 8px',
                        borderRadius: '4px',
                        fontWeight: 600,
                      }}>
                        ↓ {inv.modelInfo.inputTokens}
                      </span>
                    )}
                    {inv.modelInfo.outputTokens && (
                      <span style={{
                        fontSize: '10px',
                        color: 'var(--text-tertiary)',
                        background: 'var(--card-bg)',
                        padding: '3px 8px',
                        borderRadius: '4px',
                        fontWeight: 600,
                      }}>
                        ↑ {inv.modelInfo.outputTokens}
                      </span>
                    )}
                  </div>
                )}
              </div>

              <div style={{ marginBottom: '12px' }}>
                <div style={{
                  fontSize: '10px',
                  color: '#6b7280',
                  marginBottom: '6px',
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  letterSpacing: '0.3px',
                }}>
                  User
                </div>
                <div style={{
                  fontSize: '14px',
                  color: 'var(--text-primary)',
                  lineHeight: '1.6',
                }}>
                  {inv.userText || '(no text)'}
                </div>
              </div>

              {inv.toolCalls && inv.toolCalls.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <div style={{
                    fontSize: '10px',
                    color: '#6b7280',
                    marginBottom: '6px',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    letterSpacing: '0.3px',
                  }}>
                    Tools
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {inv.toolCalls.map((tc, i) => (
                      <div
                        key={i}
                        style={{
                          fontSize: '12px',
                          color: '#3b82f6',
                          fontFamily: 'monospace',
                          background: 'rgba(59, 130, 246, 0.08)',
                          padding: '6px 10px',
                          borderRadius: '6px',
                          fontWeight: 500,
                        }}
                      >
                        {tc.name}({Object.keys(tc.args).length > 0 ? Object.keys(tc.args).map(k => `${k}=${JSON.stringify(tc.args[k])}`).join(', ') : ''})
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {inv.agentText && (
                <div>
                  <div style={{
                    fontSize: '10px',
                    color: '#6b7280',
                    marginBottom: '6px',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    letterSpacing: '0.3px',
                  }}>
                    Agent
                  </div>
                  <div style={{
                    fontSize: '14px',
                    color: 'var(--text-primary)',
                    lineHeight: '1.6',
                  }}>
                    {inv.agentText}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
        </details>
      )}

    </div>
  );
}
