import { useEffect, useRef, useState } from 'react';
import { useTraceContext } from '../../context/TraceContext';
import { SessionCard } from './SessionCard';
import { config } from '../../config';
import { getConfig } from '../../api/client';
import { decodeFrame, isComplete, primaryTraceId, sessionId, startedAtMs } from '../../lib/liveSessions';
import type { Session } from '../../lib/liveSessions';

export function LiveStreamingView() {
  const { state, actions } = useTraceContext();
  const activeSessions = state.streamingSessions;
  const setActiveSessions = actions.setStreamingSessions;
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const [selectedGoldenId, setSelectedGoldenId] = useState<string | null>(null);
  const [isPreparingEvaluation, setIsPreparingEvaluation] = useState(false);
  const [otlpPorts, setOtlpPorts] = useState<{ httpPort: number; grpcPort: number } | null>(null);

  useEffect(() => {
    getConfig().then(cfg => setOtlpPorts(cfg.otlp ?? null)).catch(() => {});
  }, []);

  const totalQueuedSessions = state.annotationQueues.reduce((sum, q) => sum + q.items.length, 0);

  const handleAddToQueue = (session: Session, queueId: string) => {
    actions.addToAnnotationQueue(queueId, session);
    actions.setCurrentAnnotationQueueId(queueId);
  };

  const handleCreateAndAddToQueue = (session: Session, name: string) => {
    const id = actions.createAnnotationQueue(name);
    actions.addToAnnotationQueue(id, session);
    actions.setCurrentAnnotationQueueId(id);
  };

  const getQueueNamesForSession = (id: string): string[] =>
    state.annotationQueues
      .filter(q => q.items.some(item => item.sessionId === id))
      .map(q => q.name);

  const eventSourceRef = useRef<EventSource | null>(null);
  const retryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryDelayRef = useRef(1000);
  const mountedRef = useRef(true);
  // The context's actions change identity with unrelated state; the stream must not reconnect then.
  const setSessionsRef = useRef(setActiveSessions);
  useEffect(() => {
    setSessionsRef.current = setActiveSessions;
  }, [setActiveSessions]);

  useEffect(() => {
    mountedRef.current = true;

    const connectSSE = () => {
      if (!mountedRef.current) return;

      const es = new EventSource(config.api.endpoints.uiUpdatesStream);
      eventSourceRef.current = es;
      // Every connection starts with the server's current sessions, then ``synced``. Until then the
      // snapshot is collected here and replaces the shown sessions at once, so sessions that went
      // away while disconnected do not linger.
      let snapshot: Map<string, Session> | null = new Map();

      es.onopen = () => {
        retryDelayRef.current = 1000;
        setConnectionStatus('connected');
      };

      es.onerror = () => {
        if (!mountedRef.current) return;
        setConnectionStatus('disconnected');
        if (es.readyState === EventSource.CLOSED) {
          scheduleReconnect();
        }
      };

      es.onmessage = (message) => {
        const { event } = decodeFrame(message.data);
        switch (event.case) {
          case 'upsert': {
            const session = event.value;
            if (snapshot) {
              snapshot.set(sessionId(session), session);
            } else {
              setSessionsRef.current(prev => new Map(prev).set(sessionId(session), session));
            }
            break;
          }
          case 'removed': {
            const id = event.value.id;
            if (snapshot) {
              snapshot.delete(id);
            } else {
              setSessionsRef.current(prev => {
                const next = new Map(prev);
                next.delete(id);
                return next;
              });
            }
            break;
          }
          case 'synced': {
            const sessions = snapshot ?? new Map<string, Session>();
            snapshot = null;
            setSessionsRef.current(() => sessions);
            break;
          }
        }
      };
    };

    const scheduleReconnect = () => {
      if (!mountedRef.current) return;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;

      retryTimeoutRef.current = setTimeout(() => {
        retryTimeoutRef.current = null;
        retryDelayRef.current = Math.min(retryDelayRef.current * 2, 30000);
        connectSSE();
      }, retryDelayRef.current);
    };

    connectSSE();

    return () => {
      mountedRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
        retryTimeoutRef.current = null;
      }
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

      const evalSetEnvelope = await evalSetResponse.json();
      const evalSetBlob = new Blob([JSON.stringify(evalSetEnvelope.data.evalSet, null, 2)], { type: 'application/json' });
      const evalSetFile = new File([evalSetBlob], `eval_set_${selectedGoldenId}.json`, { type: 'application/json' });

      if (import.meta.env.DEV) {
        console.log('Fetching traces for all sessions');
      }
      // Include the golden alongside the other sessions so its performance is
      // plotted as a reference in the charts. It's tagged via goldenTraceId so
      // the results view keeps it out of pass/fail scoring (scoring it against
      // the eval set derived from itself is a trivial pass).
      const sessionIds = Array.from(activeSessions.keys());
      const goldenTraceId = primaryTraceId(goldenSession) || null;

      const traceFiles = await Promise.all(
        sessionIds.map(async (id) => {
          const session = activeSessions.get(id);
          if (!session || session.turns.length === 0) {
            return null;
          }

          const traceResponse = await fetch(config.api.endpoints.streamingGetTrace, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: id }),
          });

          if (!traceResponse.ok) {
            console.warn(`Failed to get trace for session ${id}`);
            return null;
          }

          const traceEnvelope = await traceResponse.json();
          const traceBlob = new Blob([traceEnvelope.data.traceContent], { type: 'application/json' });
          return new File([traceBlob], `trace_${id}.jsonl`, { type: 'application/json' });
        })
      );

      const validTraceFiles = traceFiles.filter((f): f is File => f !== null);

      if (import.meta.env.DEV) {
        console.log(`Loaded ${validTraceFiles.length} trace files and 1 eval set`);
      }

      actions.setEvaluationOrigin('streaming');
      actions.setTraceFiles(validTraceFiles);
      actions.setEvalSet(evalSetFile);
      actions.setGoldenTraceId(goldenTraceId);
      actions.setCurrentView('upload');

    } catch (error) {
      console.error('Failed to prepare evaluation:', error);
      alert(`Failed to prepare evaluation: ${error instanceof Error ? error.message : 'Unknown error'}`);
    } finally {
      setIsPreparingEvaluation(false);
    }
  };

  const newestFirst = (a: Session, b: Session) =>
    startedAtMs(b) - startedAtMs(a) || sessionId(b).localeCompare(sessionId(a));
  const sessions = Array.from(activeSessions.values());
  const activeLiveSessions = sessions
    .filter(s => !isComplete(s))
    .sort(newestFirst);
  const completedSessions = sessions
    .filter(isComplete)
    .sort(newestFirst);
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

          </div>
        </div>

        {allSessions.length > 0 && (
          <div style={{
            padding: '12px 16px',
            background: 'rgba(124, 58, 237, 0.05)',
            borderRadius: '8px',
            border: '1px solid rgba(124, 58, 237, 0.2)',
            display: 'flex',
            gap: '16px',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}>
            <span style={{
              fontSize: '13px',
              color: 'var(--text-secondary)',
              fontWeight: 600,
            }}>
              {activeLiveSessions.length} active, {completedSessions.length} completed
            </span>
            {completedSessions.length > 0 && (
              <button
                onClick={() => {
                  actions.clearAllSessions();
                  if (selectedGoldenId && completedSessions.some(s => sessionId(s) === selectedGoldenId)) {
                    setSelectedGoldenId(null);
                  }
                }}
                style={{
                  padding: '5px 12px',
                  borderRadius: '6px',
                  background: 'transparent',
                  border: '1.5px solid rgba(239, 68, 68, 0.4)',
                  color: '#ef4444',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
              >
                Clear completed
              </button>
            )}
          </div>
        )}
      </div>

      {totalQueuedSessions > 0 && (
        <div style={{
          padding: '16px 20px',
          background: 'linear-gradient(135deg, rgba(168, 85, 247, 0.08) 0%, rgba(124, 58, 237, 0.08) 100%)',
          borderRadius: '12px',
          marginBottom: '16px',
          border: '2px solid rgba(168, 85, 247, 0.2)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <div style={{ fontSize: '12px', fontWeight: 700, color: '#A855F7', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
              Annotation Queues
            </div>
            <p style={{ fontSize: '14px', color: 'var(--text-secondary)', margin: 0 }}>
              {totalQueuedSessions} session{totalQueuedSessions !== 1 ? 's' : ''} across {state.annotationQueues.length} queue{state.annotationQueues.length !== 1 ? 's' : ''}
            </p>
          </div>
          <button
            onClick={() => actions.setCurrentView('annotation-queue')}
            style={{
              padding: '8px 20px', borderRadius: '8px',
              background: 'rgba(168, 85, 247, 0.15)',
              border: '1.5px solid rgba(168, 85, 247, 0.4)',
              color: '#A855F7', fontSize: '13px', fontWeight: 600,
              cursor: 'pointer', transition: 'all 0.2s', whiteSpace: 'nowrap',
            }}
          >
            Open Annotation Queues →
          </button>
        </div>
      )}

      {selectedGoldenId && (
        <div style={{
          padding: '24px',
          background: 'linear-gradient(135deg, rgba(124, 58, 237, 0.08) 0%, rgba(168, 85, 247, 0.08) 100%)',
          borderRadius: '12px',
          marginBottom: '24px',
          border: '2px solid rgba(124, 58, 237, 0.2)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <div style={{
              fontSize: '12px',
              fontWeight: 700,
              color: '#7C3AED',
              marginBottom: '8px',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}>
              {completedSessions.length > 1 ? 'Golden Run Selected' : 'EvalSet Selected'}
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
              {completedSessions.length > 1
                ? `Ready to evaluate ${completedSessions.length - 1} session${completedSessions.length - 1 !== 1 ? 's' : ''} against this baseline`
                : 'Run more agent sessions to evaluate them against this baseline'}
            </p>
          </div>

          {completedSessions.length > 1 && (
            <button
              onClick={handleContinueToEvaluation}
              disabled={isPreparingEvaluation}
              style={{
                height: '44px',
                padding: '0 32px',
                borderRadius: '8px',
                background: isPreparingEvaluation ? 'var(--bg-surface)' : 'var(--accent-primary)',
                border: isPreparingEvaluation ? '1px solid var(--border-default)' : 'none',
                color: isPreparingEvaluation ? 'var(--text-secondary)' : '#fff',
                fontSize: '15px',
                fontWeight: 600,
                cursor: isPreparingEvaluation ? 'not-allowed' : 'pointer',
                opacity: isPreparingEvaluation ? 0.4 : 1,
                transition: 'all 0.3s ease',
                boxShadow: isPreparingEvaluation ? 'none' : '0 0 20px rgba(168, 85, 247, 0.3)',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
              }}
              onMouseEnter={(e) => {
                if (!isPreparingEvaluation) {
                  e.currentTarget.style.transform = 'translateY(-2px)';
                  e.currentTarget.style.boxShadow = '0 0 30px rgba(168, 85, 247, 0.5)';
                }
              }}
              onMouseLeave={(e) => {
                if (!isPreparingEvaluation) {
                  e.currentTarget.style.transform = 'translateY(0)';
                  e.currentTarget.style.boxShadow = '0 0 20px rgba(168, 85, 247, 0.3)';
                }
              }}
            >
              {isPreparingEvaluation ? 'Preparing...' : 'Continue to Evaluation →'}
            </button>
          )}
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
            maxWidth: '480px',
            margin: '0 auto 20px',
            lineHeight: '1.6',
          }}>
            Send OpenTelemetry traces and logs from your agent, or from your Collector, to agentevals.
          </p>
          <div style={{
            display: 'inline-grid',
            gridTemplateColumns: 'auto auto',
            gap: '6px 16px',
            textAlign: 'left',
            fontSize: '13px',
            marginBottom: '20px',
          }}>
            <span style={{ color: 'var(--text-tertiary)', fontWeight: 600 }}>OTLP HTTP</span>
            <code style={{ color: 'var(--text-primary)' }}>
              http://{window.location.hostname}:{otlpPorts?.httpPort ?? 4318}
            </code>
            <span style={{ color: 'var(--text-tertiary)', fontWeight: 600 }}>OTLP gRPC</span>
            <code style={{ color: 'var(--text-primary)' }}>
              {window.location.hostname}:{otlpPorts?.grpcPort ?? 4317}
            </code>
          </div>
          <ol style={{
            fontSize: '13px',
            color: 'var(--text-secondary)',
            maxWidth: '480px',
            margin: '0 auto',
            paddingLeft: '20px',
            textAlign: 'left',
            lineHeight: '1.7',
          }}>
            <li>
              Set <code>OTEL_EXPORTER_OTLP_ENDPOINT</code> (or your Collector's exporter) to one of the endpoints above.
            </li>
            <li>
              Turn on message content capture in your instrumentation. Without it, turns arrive with no text and cannot become an EvalSet.
            </li>
          </ol>
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
                    key={sessionId(session)}
                    session={session}
                    isSelected={selectedGoldenId === sessionId(session)}
                    onSelect={() => setSelectedGoldenId(
                      selectedGoldenId === sessionId(session) ? null : sessionId(session)
                    )}
                    annotationQueues={state.annotationQueues}
                    onAddToQueue={(queueId) => handleAddToQueue(session, queueId)}
                    onCreateAndAddToQueue={(name) => handleCreateAndAddToQueue(session, name)}
                    queueNames={getQueueNamesForSession(sessionId(session))}
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
                    key={sessionId(session)}
                    session={session}
                    isSelected={selectedGoldenId === sessionId(session)}
                    onSelect={() => setSelectedGoldenId(
                      selectedGoldenId === sessionId(session) ? null : sessionId(session)
                    )}
                    onRemove={() => {
                      actions.removeSession(sessionId(session));
                      if (selectedGoldenId === sessionId(session)) setSelectedGoldenId(null);
                    }}
                    annotationQueues={state.annotationQueues}
                    onAddToQueue={(queueId) => handleAddToQueue(session, queueId)}
                    onCreateAndAddToQueue={(name) => handleCreateAndAddToQueue(session, name)}
                    queueNames={getQueueNamesForSession(sessionId(session))}
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
