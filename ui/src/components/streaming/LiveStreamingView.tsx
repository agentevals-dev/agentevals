import { useEffect, useState } from 'react';
import { useTraceContext } from '../../context/TraceContext';
import { SessionCard } from './SessionCard';
import type { ConversationElement } from './LiveConversationPanel';
import { config } from '../../config';

interface Invocation {
  invocationId: string;
  userText: string;
  agentText: string;
  toolCalls: Array<{ name: string; args: any }>;
}

interface LiveSession {
  sessionId: string;
  traceId: string;
  evalSetId: string | null;
  spans: any[];
  status: 'active' | 'complete';
  metadata: Record<string, any>;
  invocations?: Invocation[];
  liveElements: ConversationElement[];
  liveStats: {
    totalInputTokens: number;
    totalOutputTokens: number;
  };
  startedAt: string;
}

export function LiveStreamingView() {
  const { actions } = useTraceContext();
  const [activeSessions, setActiveSessions] = useState<Map<string, LiveSession>>(new Map());
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const [selectedGoldenId, setSelectedGoldenId] = useState<string | null>(null);
  const [isPreparingEvaluation, setIsPreparingEvaluation] = useState(false);

  useEffect(() => {
    if (import.meta.env.DEV) {
      console.log('[Streaming] Setting up SSE connection');
    }
    const eventSource = new EventSource(config.api.endpoints.uiUpdatesStream);

    eventSource.onopen = () => {
      if (import.meta.env.DEV) {
        console.log('[Streaming] SSE connected');
      }
      setConnectionStatus('connected');
    };

    eventSource.onerror = (error) => {
      console.error('[Streaming] SSE error:', error);
      setConnectionStatus('disconnected');
    };

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (import.meta.env.DEV) {
        console.log('[Streaming] Received event:', data.type, data);
      }

      switch (data.type) {
        case 'session_started':
          if (import.meta.env.DEV) {
            console.log('[Streaming] New session started:', data.session.sessionId);
          }
          setActiveSessions(prev => {
            const newMap = new Map(prev);
            newMap.set(data.session.sessionId, {
              sessionId: data.session.sessionId,
              traceId: data.session.traceId,
              evalSetId: data.session.evalSetId,
              spans: [],
              status: 'active',
              metadata: data.session.metadata || {},
              liveElements: [],
              liveStats: {
                totalInputTokens: 0,
                totalOutputTokens: 0,
              },
              startedAt: data.session.startedAt,
            });
            return newMap;
          });
          break;

        case 'span_received':
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);
            if (!session) return prev;

            const newMap = new Map(prev);
            newMap.set(data.sessionId, {
              ...session,
              spans: [...session.spans, data.span],
            });
            return newMap;
          });
          break;

        case 'user_input':
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);
            if (!session) return prev;

            const newMap = new Map(prev);
            newMap.set(data.sessionId, {
              ...session,
              liveElements: [
                ...session.liveElements,
                {
                  type: 'user_input',
                  timestamp: data.timestamp,
                  invocationId: data.invocationId,
                  data: { text: data.text },
                },
              ],
            });
            return newMap;
          });
          break;

        case 'tool_call':
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);
            if (!session) return prev;

            const newMap = new Map(prev);
            newMap.set(data.sessionId, {
              ...session,
              liveElements: [
                ...session.liveElements,
                {
                  type: 'tool_call',
                  timestamp: data.timestamp,
                  invocationId: data.invocationId,
                  data: { toolCall: data.toolCall },
                },
              ],
            });
            return newMap;
          });
          break;

        case 'agent_response':
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);
            if (!session) return prev;

            const newMap = new Map(prev);
            newMap.set(data.sessionId, {
              ...session,
              liveElements: [
                ...session.liveElements,
                {
                  type: 'agent_response',
                  timestamp: data.timestamp,
                  invocationId: data.invocationId,
                  data: { text: data.text },
                },
              ],
            });
            return newMap;
          });
          break;

        case 'token_update':
          if (import.meta.env.DEV) {
            console.log('[Streaming] Token update:', data);
          }
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);
            if (!session) {
              console.warn('[Streaming] Token update for unknown session:', data.sessionId);
              return prev;
            }

            const newStats = {
              ...session.liveStats,
              totalInputTokens: session.liveStats.totalInputTokens + (data.inputTokens || 0),
              totalOutputTokens: session.liveStats.totalOutputTokens + (data.outputTokens || 0),
            };

            if (import.meta.env.DEV) {
              console.log('[Streaming] New stats:', newStats);
            }

            const newMap = new Map(prev);
            newMap.set(data.sessionId, {
              ...session,
              liveStats: newStats,
            });
            return newMap;
          });
          break;

        case 'session_complete':
          if (import.meta.env.DEV) {
            console.log('[Streaming] Session complete with invocations:', data.invocations?.length);
          }
          setActiveSessions(prev => {
            const session = prev.get(data.sessionId);

            const newMap = new Map(prev);

            if (!session) {
              if (import.meta.env.DEV) {
                console.warn('[Streaming] Session not found, creating it now:', data.sessionId);
              }
              newMap.set(data.sessionId, {
                sessionId: data.sessionId,
                traceId: 'unknown',
                evalSetId: null,
                spans: [],
                status: 'complete',
                metadata: {},
                invocations: data.invocations,
                liveElements: [],
                liveStats: {
                  totalInputTokens: 0,
                  totalOutputTokens: 0,
                },
                startedAt: new Date().toISOString(),
              });
            } else {
              newMap.set(data.sessionId, {
                ...session,
                status: 'complete',
                invocations: data.invocations,
              });
            }

            return newMap;
          });
          break;

      }
    };

    return () => {
      if (import.meta.env.DEV) {
        console.log('[Streaming] Closing SSE connection');
      }
      eventSource.close();
    };
  }, []);

  const handleContinueToEvaluation = async () => {
    if (!selectedGoldenId) return;

    setIsPreparingEvaluation(true);

    try {
      const goldenSession = activeSessions.get(selectedGoldenId);
      if (!goldenSession) {
        throw new Error('Golden session not found');
      }

      if (import.meta.env.DEV) {
        console.log('Creating eval set from golden session:', selectedGoldenId);
      }

      const evalSetResponse = await fetch(config.api.endpoints.streamingCreateEvalSet, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: selectedGoldenId,
          eval_set_id: `golden_${selectedGoldenId}`,
        }),
      });

      if (!evalSetResponse.ok) {
        throw new Error('Failed to create eval set from golden session');
      }

      const evalSetData = await evalSetResponse.json();
      const evalSetBlob = new Blob([JSON.stringify(evalSetData.eval_set, null, 2)], { type: 'application/json' });
      const evalSetFile = new File([evalSetBlob], `eval_set_${selectedGoldenId}.json`, { type: 'application/json' });

      if (import.meta.env.DEV) {
        console.log('Fetching traces for all sessions');
      }
      const sessionIds = Array.from(activeSessions.keys());

      const traceFiles = await Promise.all(
        sessionIds.map(async (sessionId) => {
          const session = activeSessions.get(sessionId);
          if (!session || !session.invocations || session.invocations.length === 0) {
            return null;
          }

          if (import.meta.env.DEV) {
            console.log(`Fetching trace for session: ${sessionId}`);
          }

          const traceResponse = await fetch(config.api.endpoints.streamingGetTrace, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId }),
          });

          if (!traceResponse.ok) {
            console.warn(`Failed to get trace for session ${sessionId}`);
            return null;
          }

          const traceData = await traceResponse.json();
          const traceBlob = new Blob([traceData.trace_content], { type: 'application/json' });
          return new File([traceBlob], `trace_${sessionId}.jsonl`, { type: 'application/json' });
        })
      );

      const validTraceFiles = traceFiles.filter((f): f is File => f !== null);

      if (import.meta.env.DEV) {
        console.log(`Loaded ${validTraceFiles.length} trace files and 1 eval set`);
      }

      actions.setTraceFiles(validTraceFiles);
      actions.setEvalSet(evalSetFile);
      actions.setCurrentView('upload');

    } catch (error) {
      console.error('Failed to prepare evaluation:', error);
      alert(`Failed to prepare evaluation: ${error instanceof Error ? error.message : 'Unknown error'}`);
    } finally {
      setIsPreparingEvaluation(false);
    }
  };

  const sessions = Array.from(activeSessions.values());
  const activeLiveSessions = sessions
    .filter(s => s.status === 'active')
    .sort((a, b) => b.sessionId.localeCompare(a.sessionId));
  const completedSessions = sessions
    .filter(s => s.status === 'complete')
    .sort((a, b) => b.sessionId.localeCompare(a.sessionId));
  const allSessions = [...activeLiveSessions, ...completedSessions];

  return (
    <div style={{
      padding: '48px',
      maxWidth: '1400px',
      margin: '0 auto',
    }}>
      <div style={{
        marginBottom: '32px',
      }}>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'start',
          marginBottom: '16px',
        }}>
          <div>
            <h1 style={{
              fontSize: '28px',
              fontWeight: 700,
              marginBottom: '6px',
              color: 'var(--text-primary)',
              letterSpacing: '-0.5px',
            }}>
              Live Agent Sessions
            </h1>
            <p style={{
              fontSize: '14px',
              color: 'var(--text-secondary)',
              margin: 0,
            }}>
              Watch agent traces stream in real-time, select a golden run, and evaluate all sessions
            </p>
          </div>

          <div style={{
            display: 'flex',
            gap: '10px',
            alignItems: 'center',
          }}>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px 14px',
              borderRadius: '8px',
              background: connectionStatus === 'connected'
                ? 'rgba(34, 197, 94, 0.1)'
                : 'rgba(239, 68, 68, 0.1)',
              fontSize: '13px',
              fontWeight: 600,
            }}>
              <div style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                background: connectionStatus === 'connected' ? '#22c55e' : '#ef4444',
                animation: connectionStatus === 'connected' ? 'pulse 2s ease-in-out infinite' : 'none',
              }} />
              {connectionStatus === 'connected' ? 'Connected' : 'Disconnected'}
            </div>

            <button
              onClick={() => actions.setCurrentView('upload')}
              style={{
                padding: '8px 14px',
                borderRadius: '8px',
                background: 'transparent',
                border: '1.5px solid var(--border)',
                color: 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 600,
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
            >
              ← Upload View
            </button>
          </div>
        </div>

        {allSessions.length > 0 && (
          <div style={{
            padding: '12px 16px',
            background: 'rgba(59, 130, 246, 0.05)',
            borderRadius: '8px',
            border: '1px solid rgba(59, 130, 246, 0.2)',
            display: 'flex',
            gap: '16px',
            alignItems: 'center',
          }}>
            <span style={{
              fontSize: '13px',
              color: 'var(--text-secondary)',
              fontWeight: 600,
            }}>
              {activeLiveSessions.length} active, {completedSessions.length} completed
            </span>
          </div>
        )}
      </div>

      {selectedGoldenId && completedSessions.length > 1 && (
        <div style={{
          padding: '24px',
          background: 'linear-gradient(135deg, rgba(59, 130, 246, 0.08) 0%, rgba(139, 92, 246, 0.08) 100%)',
          borderRadius: '12px',
          marginBottom: '24px',
          border: '2px solid rgba(59, 130, 246, 0.2)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <div style={{
              fontSize: '12px',
              fontWeight: 700,
              color: '#3b82f6',
              marginBottom: '8px',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}>
              Golden Run Selected
            </div>
            <p style={{
              fontSize: '16px',
              fontWeight: 600,
              color: 'var(--text-primary)',
              marginBottom: '6px',
            }}>
              {selectedGoldenId}
            </p>
            <p style={{
              fontSize: '14px',
              color: 'var(--text-secondary)',
              margin: 0,
            }}>
              Ready to evaluate {completedSessions.length - 1} session{completedSessions.length - 1 !== 1 ? 's' : ''} against this baseline
            </p>
          </div>

          <button
            onClick={handleContinueToEvaluation}
            disabled={isPreparingEvaluation}
            style={{
              height: '44px',
              padding: '0 32px',
              borderRadius: '8px',
              background: isPreparingEvaluation ? 'var(--bg-surface)' : 'var(--accent-cyan)',
              border: isPreparingEvaluation ? '1px solid var(--border-default)' : 'none',
              color: isPreparingEvaluation ? 'var(--text-secondary)' : '#000',
              fontSize: '15px',
              fontWeight: 600,
              cursor: isPreparingEvaluation ? 'not-allowed' : 'pointer',
              opacity: isPreparingEvaluation ? 0.4 : 1,
              transition: 'all 0.3s ease',
              boxShadow: isPreparingEvaluation ? 'none' : '0 0 20px rgba(0, 217, 255, 0.3)',
              whiteSpace: 'nowrap',
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
            }}
            onMouseEnter={(e) => {
              if (!isPreparingEvaluation) {
                e.currentTarget.style.transform = 'translateY(-2px)';
                e.currentTarget.style.boxShadow = '0 0 30px rgba(0, 217, 255, 0.5)';
              }
            }}
            onMouseLeave={(e) => {
              if (!isPreparingEvaluation) {
                e.currentTarget.style.transform = 'translateY(0)';
                e.currentTarget.style.boxShadow = '0 0 20px rgba(0, 217, 255, 0.3)';
              }
            }}
          >
            {isPreparingEvaluation ? 'Preparing...' : 'Continue to Evaluation →'}
          </button>
        </div>
      )}


      {allSessions.length === 0 ? (
        <div style={{
          padding: '80px 40px',
          textAlign: 'center',
          background: 'var(--card-bg)',
          borderRadius: '16px',
          border: '2px dashed var(--border)',
        }}>
          <div style={{
            fontSize: '48px',
            marginBottom: '16px',
            opacity: 0.6,
          }}>
            📡
          </div>
          <p style={{
            fontSize: '18px',
            fontWeight: 600,
            color: 'var(--text-primary)',
            marginBottom: '8px',
          }}>
            Waiting for agent sessions
          </p>
          <p style={{
            fontSize: '14px',
            color: 'var(--text-secondary)',
            maxWidth: '400px',
            margin: '0 auto',
            lineHeight: '1.6',
          }}>
            Run your agent with streaming enabled to see traces appear here in real-time
          </p>
        </div>
      ) : (
        <div>
          {activeLiveSessions.length > 0 && (
            <div>
              <h2 style={{
                fontSize: '16px',
                fontWeight: 700,
                color: 'var(--text-primary)',
                marginBottom: '12px',
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
              }}>
                Active Sessions ({activeLiveSessions.length})
              </h2>
              <div style={{ display: 'grid', gap: '16px', marginBottom: '32px' }}>
                {activeLiveSessions.map(session => (
                  <SessionCard
                    key={session.sessionId}
                    session={session}
                    isSelected={selectedGoldenId === session.sessionId}
                    onSelect={() => setSelectedGoldenId(
                      selectedGoldenId === session.sessionId ? null : session.sessionId
                    )}
                  />
                ))}
              </div>
            </div>
          )}

          {completedSessions.length > 0 && (
            <div>
              <h2 style={{
                fontSize: '16px',
                fontWeight: 700,
                color: 'var(--text-primary)',
                marginBottom: '12px',
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
              }}>
                Completed Sessions ({completedSessions.length})
              </h2>
              <div style={{ display: 'grid', gap: '16px' }}>
                {completedSessions.map(session => (
                  <SessionCard
                    key={session.sessionId}
                    session={session}
                    isSelected={selectedGoldenId === session.sessionId}
                    onSelect={() => setSelectedGoldenId(
                      selectedGoldenId === session.sessionId ? null : session.sessionId
                    )}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
