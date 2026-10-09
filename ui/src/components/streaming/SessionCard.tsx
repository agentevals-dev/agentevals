import { useState, useEffect, useRef } from 'react';
import { LiveConversationPanel } from './LiveConversationPanel';
import type { ConversationElement } from './LiveConversationPanel';
import { SessionMetadata } from './SessionMetadata';
import { isFailedTurn, lacksContent } from './turnStatus';
import type { AnnotationQueue, StreamingInvocation } from '../../lib/types';

interface SessionCardProps {
  session: {
    sessionId: string;
    traceId: string;
    evalSetId: string | null;
    spans: any[];
    status: 'active' | 'complete';
    metadata: Record<string, any>;
    invocations?: StreamingInvocation[];
    liveElements?: ConversationElement[];
    liveStats?: {
      totalInputTokens: number;
      totalOutputTokens: number;
      model?: string;
    };
    startedAt?: string;
  };
  isSelected: boolean;
  onSelect: () => void;
  onRemove?: () => void;
  evaluationResult?: {
    metricResults: Array<{
      metricName: string;
      score: number;
      evalStatus: string;
    }>;
  };
  annotationQueues?: AnnotationQueue[];
  onAddToQueue?: (queueId: string) => void;
  onCreateAndAddToQueue?: (name: string) => void;
  queueNames?: string[];
}

export function SessionCard({ session, isSelected, onSelect, onRemove, evaluationResult, annotationQueues, onAddToQueue, onCreateAndAddToQueue, queueNames }: SessionCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [showQueuePicker, setShowQueuePicker] = useState(false);
  const [newQueueName, setNewQueueName] = useState('');
  const [showNewQueueInput, setShowNewQueueInput] = useState(false);
  const queuePickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (queuePickerRef.current && !queuePickerRef.current.contains(e.target as Node)) {
        setShowQueuePicker(false);
        setShowNewQueueInput(false);
        setNewQueueName('');
      }
    };
    if (showQueuePicker) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showQueuePicker]);

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
    session.liveStats?.model ||
    session.invocations?.[0]?.modelInfo?.models?.[0] ||
    'Unknown';

  const providerName = session.invocations?.[0]?.modelInfo?.provider || null;

  const totalCacheCreation = session.invocations?.reduce((sum, inv) => sum + (inv.modelInfo?.cacheCreationTokens || 0), 0) || 0;
  const totalCacheRead = session.invocations?.reduce((sum, inv) => sum + (inv.modelInfo?.cacheReadTokens || 0), 0) || 0;

  const failedTurns = session.invocations?.filter(isFailedTurn).length ?? 0;
  const noContent = lacksContent(session.invocations);
  const warnings = Array.from(new Set(session.invocations?.flatMap(inv => inv.warnings ?? []) ?? []));
  const evalSetBlocked = noContent && !isSelected;

  return (
    <div
      style={{
        background: 'var(--card-bg)',
        borderRadius: '12px',
        border: isSelected ? '2px solid #7C3AED' : '1px solid var(--border)',
        padding: '20px',
        transition: 'all 0.2s',
        boxShadow: isSelected ? '0 4px 12px rgba(124, 58, 237, 0.15)' : 'none',
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
              color: '#A855F7',
              background: 'rgba(168, 85, 247, 0.1)',
              padding: '4px 10px',
              borderRadius: '6px',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}>
              {modelName}
            </span>

            {providerName && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: '#3b82f6',
                background: 'rgba(59, 130, 246, 0.1)',
                padding: '4px 10px',
                borderRadius: '6px',
              }}>
                {providerName}
              </span>
            )}

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

            {(totalCacheCreation > 0 || totalCacheRead > 0) && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: '#f59e0b',
                background: 'rgba(245, 158, 11, 0.1)',
                padding: '4px 10px',
                borderRadius: '6px',
              }}>
                cache {totalCacheRead > 0 ? `${totalCacheRead.toLocaleString()} read` : ''}
                {totalCacheCreation > 0 && totalCacheRead > 0 ? ' / ' : ''}
                {totalCacheCreation > 0 ? `${totalCacheCreation.toLocaleString()} created` : ''}
              </span>
            )}

            {failedTurns > 0 && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--status-failure)',
                background: 'rgba(239, 68, 68, 0.1)',
                padding: '4px 10px',
                borderRadius: '6px',
              }}>
                {failedTurns} failed turn{failedTurns !== 1 ? 's' : ''}
              </span>
            )}

            {noContent && (
              <span
                title="No prompts or answers were captured. Turn on message content capture in your instrumentation."
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  color: '#f59e0b',
                  background: 'rgba(245, 158, 11, 0.1)',
                  padding: '4px 10px',
                  borderRadius: '6px',
                }}
              >
                content off
              </span>
            )}

            {warnings.length > 0 && (
              <span
                title={warnings.join('\n')}
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  color: 'var(--text-tertiary)',
                  background: 'var(--bg-primary)',
                  padding: '4px 10px',
                  borderRadius: '6px',
                  cursor: 'help',
                }}
              >
                {warnings.length} warning{warnings.length !== 1 ? 's' : ''}
              </span>
            )}

            {queueNames && queueNames.length > 0 && queueNames.map(name => (
              <span key={name} style={{
                fontSize: '11px',
                fontWeight: 600,
                color: '#A855F7',
                background: 'rgba(168, 85, 247, 0.12)',
                border: '1px solid rgba(168, 85, 247, 0.3)',
                padding: '3px 8px',
                borderRadius: '6px',
              }}>
                📋 {name}
              </span>
            ))}
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
          {session.status === 'complete' && onAddToQueue && (
            <div style={{ position: 'relative' }} ref={queuePickerRef}>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setShowQueuePicker(!showQueuePicker);
                }}
                style={{
                  padding: '8px 14px', borderRadius: '8px',
                  background: showQueuePicker ? 'rgba(168, 85, 247, 0.15)' : 'transparent',
                  border: '1.5px solid rgba(168, 85, 247, 0.4)',
                  color: '#A855F7', fontSize: '13px', fontWeight: 600,
                  cursor: 'pointer', transition: 'all 0.2s', whiteSpace: 'nowrap',
                }}
              >
                + Queue
              </button>

              {showQueuePicker && (
                <div style={{
                  position: 'absolute', right: 0, top: 'calc(100% + 6px)', zIndex: 100,
                  background: 'var(--bg-elevated)', border: '1px solid var(--border-default)',
                  borderRadius: '10px', padding: '8px', minWidth: '200px',
                  boxShadow: '0 8px 24px rgba(0,0,0,0.3)',
                }}>
                  {(annotationQueues || []).length > 0 && (
                    <div>
                      {(annotationQueues || []).map(q => (
                        <button
                          key={q.id}
                          onClick={(e) => {
                            e.stopPropagation();
                            onAddToQueue(q.id);
                            setShowQueuePicker(false);
                          }}
                          style={{
                            display: 'block', width: '100%', padding: '8px 10px',
                            borderRadius: '6px', background: 'transparent', border: 'none',
                            color: 'var(--text-primary)', fontSize: '13px', fontWeight: 500,
                            cursor: 'pointer', textAlign: 'left', transition: 'background 0.15s',
                          }}
                          onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-primary)')}
                          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                        >
                          {q.name} <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>({q.items.length})</span>
                        </button>
                      ))}
                      <div style={{ borderTop: '1px solid var(--border-subtle)', margin: '6px 0' }} />
                    </div>
                  )}

                  {showNewQueueInput ? (
                    <div style={{ padding: '4px 2px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      <input
                        autoFocus
                        value={newQueueName}
                        onChange={(e) => setNewQueueName(e.target.value)}
                        onKeyDown={(e) => {
                          e.stopPropagation();
                          if (e.key === 'Enter' && newQueueName.trim() && onCreateAndAddToQueue) {
                            onCreateAndAddToQueue(newQueueName.trim());
                            setNewQueueName('');
                            setShowNewQueueInput(false);
                            setShowQueuePicker(false);
                          }
                          if (e.key === 'Escape') setShowNewQueueInput(false);
                        }}
                        placeholder="Queue name"
                        style={{
                          padding: '6px 10px', borderRadius: '6px',
                          border: '1.5px solid var(--border-default)',
                          background: 'var(--bg-primary)', color: 'var(--text-primary)',
                          fontSize: '12px', outline: 'none', width: '100%', boxSizing: 'border-box',
                        }}
                      />
                      <div style={{ display: 'flex', gap: '4px' }}>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            if (newQueueName.trim() && onCreateAndAddToQueue) {
                              onCreateAndAddToQueue(newQueueName.trim());
                              setNewQueueName('');
                              setShowNewQueueInput(false);
                              setShowQueuePicker(false);
                            }
                          }}
                          style={{
                            flex: 1, padding: '6px', borderRadius: '6px',
                            background: 'var(--accent-primary)', border: 'none',
                            color: '#fff', fontSize: '12px', fontWeight: 600, cursor: 'pointer',
                          }}
                        >
                          Create
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); setShowNewQueueInput(false); setNewQueueName(''); }}
                          style={{
                            padding: '6px 8px', borderRadius: '6px', background: 'transparent',
                            border: '1px solid var(--border)', color: 'var(--text-secondary)',
                            fontSize: '12px', cursor: 'pointer',
                          }}
                        >
                          ×
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); setShowNewQueueInput(true); }}
                      style={{
                        display: 'block', width: '100%', padding: '8px 10px',
                        borderRadius: '6px', background: 'transparent', border: 'none',
                        color: '#A855F7', fontSize: '13px', fontWeight: 600,
                        cursor: 'pointer', textAlign: 'left', transition: 'background 0.15s',
                      }}
                      onMouseEnter={e => (e.currentTarget.style.background = 'rgba(168, 85, 247, 0.08)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                    >
                      + New queue
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

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
            disabled={evalSetBlocked}
            title={evalSetBlocked
              ? 'This session has no message content, so it cannot be a golden run. Turn on content capture and run the agent again.'
              : undefined}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              background: isSelected ? '#7C3AED' : 'transparent',
              border: isSelected ? 'none' : '1.5px solid var(--border)',
              color: isSelected ? 'white' : 'var(--text-primary)',
              fontSize: '13px',
              fontWeight: 600,
              cursor: evalSetBlocked ? 'not-allowed' : 'pointer',
              opacity: evalSetBlocked ? 0.4 : 1,
              transition: 'all 0.2s',
              whiteSpace: 'nowrap',
            }}
          >
            {isSelected ? 'EvalSet' : 'Set as EvalSet'}
          </button>

          {session.status === 'complete' && onRemove && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRemove();
              }}
              title="Remove session"
              style={{
                padding: '8px 10px',
                borderRadius: '8px',
                background: 'transparent',
                border: '1.5px solid rgba(239, 68, 68, 0.3)',
                color: '#ef4444',
                fontSize: '13px',
                fontWeight: 600,
                cursor: 'pointer',
                transition: 'all 0.2s',
                lineHeight: 1,
              }}
            >
              ×
            </button>
          )}

          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            style={{
              padding: '8px 12px',
              borderRadius: '8px',
              background: expanded ? 'rgba(124, 58, 237, 0.1)' : 'transparent',
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

      {evalSetBlocked && (
        <p style={{
          fontSize: '12px',
          color: 'var(--text-secondary)',
          margin: '-8px 0 16px 0',
        }}>
          This session has no message content, so it cannot be set as the EvalSet. Turn on content capture in your instrumentation and run the agent again.
        </p>
      )}

      {expanded && session.startedAt && (totalTokens > 0 || Object.keys(session.metadata).length > 0) && (
        <SessionMetadata
          session={{
            sessionId: session.sessionId,
            traceId: session.traceId,
            metadata: session.metadata,
            startedAt: session.startedAt,
            status: session.status,
            invocations: session.invocations,
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
                borderLeft: '3px solid #7C3AED',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <span style={{
                  fontSize: '11px',
                  color: '#7C3AED',
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
                          color: '#7C3AED',
                          fontFamily: 'monospace',
                          background: 'rgba(124, 58, 237, 0.08)',
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
