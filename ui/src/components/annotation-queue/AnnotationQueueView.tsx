import { useState } from 'react';
import { Select } from 'antd';
import { useTraceContext } from '../../context/TraceContext';
import { AnnotationTable } from './AnnotationTable';
import { AnnotationDetailPanel } from './AnnotationDetailPanel';
import type { Annotation, AnnotationQueueItem } from '../../lib/types';
import { config } from '../../config';

export function AnnotationQueueView() {
  const { state, actions } = useTraceContext();
  const { annotationQueues, currentAnnotationQueueId } = state;

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedGoldenId, setSelectedGoldenId] = useState<string | null>(null);
  const [isPreparingEvaluation, setIsPreparingEvaluation] = useState(false);
  const [newQueueName, setNewQueueName] = useState('');
  const [showNewQueueInput, setShowNewQueueInput] = useState(false);

  const currentQueue = annotationQueues.find(q => q.id === currentAnnotationQueueId)
    || annotationQueues[0]
    || null;

  const selectedItem: AnnotationQueueItem | null = currentQueue
    ? currentQueue.items.find(item => item.sessionId === selectedSessionId) ?? null
    : null;

  const handleSaveAnnotation = (annotation: Annotation) => {
    if (!currentQueue || !selectedSessionId) return;
    actions.annotateQueueItem(currentQueue.id, selectedSessionId, annotation);
  };

  const handleCreateQueue = () => {
    const name = newQueueName.trim();
    if (!name) return;
    const id = actions.createAnnotationQueue(name);
    actions.setCurrentAnnotationQueueId(id);
    setNewQueueName('');
    setShowNewQueueInput(false);
  };

  const handleContinueToEvaluation = async () => {
    if (!selectedGoldenId || !currentQueue) return;
    setIsPreparingEvaluation(true);

    try {
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

      const traceFiles = await Promise.all(
        currentQueue.items.map(async (item) => {
          if (item.session.turns.length === 0) return null;

          const traceResponse = await fetch(config.api.endpoints.streamingGetTrace, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: item.sessionId }),
          });

          if (!traceResponse.ok) return null;

          const traceEnvelope = await traceResponse.json();
          const traceBlob = new Blob([traceEnvelope.data.traceContent], { type: 'application/json' });
          return new File([traceBlob], `trace_${item.sessionId}.jsonl`, { type: 'application/json' });
        })
      );

      const validTraceFiles = traceFiles.filter((f): f is File => f !== null);

      const pendingAnnotations = new Map<string, Annotation>();
      for (const item of currentQueue.items) {
        if (item.annotation) {
          pendingAnnotations.set(item.traceId, item.annotation);
        }
      }

      actions.setPendingAnnotations(pendingAnnotations);
      actions.setEvaluationOrigin('annotation-queue');
      await actions.setTraceFiles(validTraceFiles);
      actions.setEvalSet(evalSetFile);
      actions.setCurrentView('upload');
    } catch (error) {
      console.error('Failed to prepare evaluation:', error);
      alert(`Failed to prepare evaluation: ${error instanceof Error ? error.message : 'Unknown error'}`);
    } finally {
      setIsPreparingEvaluation(false);
    }
  };

  const completedItems = currentQueue?.items.filter(item => item.session.turns.length > 0) || [];

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      overflow: 'hidden',
    }}>
      <div style={{
        padding: '24px 48px 0',
        flexShrink: 0,
      }}>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'start',
          marginBottom: '20px',
        }}>
          <div>
            <h1 style={{
              fontSize: '28px', fontWeight: 700, marginBottom: '6px',
              color: 'var(--text-primary)', letterSpacing: '-0.5px',
            }}>
              Annotation Queues
            </h1>
            <p style={{ fontSize: '14px', color: 'var(--text-secondary)', margin: 0 }}>
              Review and annotate agent sessions before evaluation
            </p>
          </div>

        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
          <div style={{ minWidth: '220px' }}>
            <Select
              value={currentQueue?.id ?? undefined}
              onChange={(val) => {
                actions.setCurrentAnnotationQueueId(val);
                setSelectedSessionId(null);
                setSelectedGoldenId(null);
              }}
              placeholder="Select a queue"
              style={{ width: '100%' }}
              options={annotationQueues.map(q => ({
                label: `${q.name} (${q.items.length})`,
                value: q.id,
              }))}
            />
          </div>

          {showNewQueueInput ? (
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <input
                autoFocus
                value={newQueueName}
                onChange={(e) => setNewQueueName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleCreateQueue(); if (e.key === 'Escape') setShowNewQueueInput(false); }}
                placeholder="Queue name"
                style={{
                  padding: '7px 12px', borderRadius: '8px',
                  border: '1.5px solid var(--border-default)',
                  background: 'var(--bg-primary)', color: 'var(--text-primary)',
                  fontSize: '13px', outline: 'none', width: '180px',
                }}
              />
              <button
                onClick={handleCreateQueue}
                style={{
                  padding: '7px 14px', borderRadius: '8px',
                  background: 'var(--accent-primary)', border: 'none',
                  color: '#fff', fontSize: '13px', fontWeight: 600, cursor: 'pointer',
                }}
              >
                Create
              </button>
              <button
                onClick={() => setShowNewQueueInput(false)}
                style={{
                  padding: '7px 10px', borderRadius: '8px', background: 'transparent',
                  border: '1.5px solid var(--border)', color: 'var(--text-secondary)',
                  fontSize: '13px', cursor: 'pointer',
                }}
              >
                ×
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowNewQueueInput(true)}
              style={{
                padding: '7px 14px', borderRadius: '8px', background: 'transparent',
                border: '1.5px solid var(--border)', color: 'var(--text-secondary)',
                fontSize: '13px', fontWeight: 600, cursor: 'pointer', transition: 'all 0.2s',
              }}
            >
              + New Queue
            </button>
          )}
        </div>

        {selectedGoldenId && (
          <div style={{
            padding: '20px 24px',
            background: 'linear-gradient(135deg, rgba(124, 58, 237, 0.08) 0%, rgba(168, 85, 247, 0.08) 100%)',
            borderRadius: '12px',
            marginBottom: '20px',
            border: '2px solid rgba(124, 58, 237, 0.2)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}>
            <div>
              <div style={{ fontSize: '12px', fontWeight: 700, color: '#7C3AED', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                {completedItems.length > 1 ? 'Golden Run Selected' : 'EvalSet Selected'}
              </div>
              <p style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '4px' }}>
                {selectedGoldenId}
              </p>
              <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: 0 }}>
                {completedItems.length > 1
                  ? `Ready to evaluate ${completedItems.length - 1} session${completedItems.length - 1 !== 1 ? 's' : ''} against this baseline`
                  : 'Add more sessions to evaluate them against this baseline'}
              </p>
            </div>

            {completedItems.length > 1 && (
              <button
                onClick={handleContinueToEvaluation}
                disabled={isPreparingEvaluation}
                style={{
                  height: '44px', padding: '0 32px', borderRadius: '8px',
                  background: isPreparingEvaluation ? 'var(--bg-surface)' : 'var(--accent-primary)',
                  border: isPreparingEvaluation ? '1px solid var(--border-default)' : 'none',
                  color: isPreparingEvaluation ? 'var(--text-secondary)' : '#fff',
                  fontSize: '15px', fontWeight: 600,
                  cursor: isPreparingEvaluation ? 'not-allowed' : 'pointer',
                  opacity: isPreparingEvaluation ? 0.4 : 1,
                  transition: 'all 0.3s ease',
                  boxShadow: isPreparingEvaluation ? 'none' : '0 0 20px rgba(168, 85, 247, 0.3)',
                  whiteSpace: 'nowrap',
                }}
              >
                {isPreparingEvaluation ? 'Preparing...' : 'Continue to Evaluation →'}
              </button>
            )}
          </div>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
        {!currentQueue ? (
          <div style={{
            padding: '80px 48px', textAlign: 'center',
          }}>
            <div style={{ fontSize: '48px', marginBottom: '16px', opacity: 0.6 }}>📋</div>
            <p style={{ fontSize: '18px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '8px' }}>
              No annotation queues yet
            </p>
            <p style={{ fontSize: '14px', color: 'var(--text-secondary)', maxWidth: '360px', margin: '0 auto 20px', lineHeight: '1.6' }}>
              Add sessions from the live view or create a queue above to get started
            </p>
            <button
              onClick={() => setShowNewQueueInput(true)}
              style={{
                padding: '10px 24px', borderRadius: '8px', background: 'var(--accent-primary)',
                border: 'none', color: '#fff', fontSize: '14px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              Create a queue
            </button>
          </div>
        ) : selectedItem ? (
          <div style={{ height: '100%', overflow: 'hidden' }}>
            <AnnotationDetailPanel
              item={selectedItem}
              onSave={handleSaveAnnotation}
              onClose={() => setSelectedSessionId(null)}
            />
          </div>
        ) : (
          <div style={{ padding: '0 48px 24px' }}>
            {currentQueue.items.length === 0 ? (
              <div style={{
                padding: '60px 40px', textAlign: 'center',
                background: 'var(--card-bg)', borderRadius: '16px',
                border: '2px dashed var(--border)',
              }}>
                <p style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '8px' }}>
                  Queue is empty
                </p>
                <p style={{ fontSize: '14px', color: 'var(--text-secondary)' }}>
                  Add sessions from the live view using the "Add to Queue" button
                </p>
              </div>
            ) : (
              <div>
                <div style={{
                  padding: '10px 14px', marginBottom: '16px',
                  background: 'rgba(124, 58, 237, 0.05)',
                  borderRadius: '8px', border: '1px solid rgba(124, 58, 237, 0.2)',
                  display: 'flex', gap: '16px', alignItems: 'center', justifyContent: 'space-between',
                }}>
                  <span style={{ fontSize: '13px', color: 'var(--text-secondary)', fontWeight: 600 }}>
                    {currentQueue.items.length} session{currentQueue.items.length !== 1 ? 's' : ''} · {currentQueue.items.filter(i => i.annotation).length} annotated
                  </span>
                  <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    Click a row to annotate · Click "Set as EvalSet" to select a golden run
                  </span>
                </div>

                <AnnotationTable
                  items={currentQueue.items}
                  selectedSessionId={selectedSessionId}
                  onSelectSession={(sessionId) => {
                    setSelectedSessionId(sessionId === selectedSessionId ? null : sessionId);
                  }}
                  selectedGoldenId={selectedGoldenId}
                  onSelectGolden={(sessionId) => setSelectedGoldenId(
                    selectedGoldenId === sessionId ? null : sessionId
                  )}
                />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
