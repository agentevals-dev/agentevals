import { useState } from 'react';
import type { AnnotationQueueItem, Annotation, FirstPassLabel } from '../../lib/types';
import { TurnMessages } from '../streaming/LiveConversationPanel';

interface AnnotationDetailPanelProps {
  item: AnnotationQueueItem;
  onSave: (annotation: Annotation) => void;
  onClose: () => void;
}

export function AnnotationDetailPanel({ item, onSave, onClose }: AnnotationDetailPanelProps) {
  const [firstPass, setFirstPass] = useState<FirstPassLabel | null>(item.annotation?.firstPass ?? null);
  const [comment, setComment] = useState(item.annotation?.comment ?? '');
  const [saved, setSaved] = useState(false);

  const handleSave = () => {
    if (!firstPass) return;
    const annotation: Annotation = {
      firstPass,
      comment,
      annotatedAt: new Date().toISOString(),
    };
    onSave(annotation);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const turns = item.session.turns;
  const totalTokens = item.totalTokens ?? 0;

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0, background: 'var(--bg-surface)' }}>
      <div style={{
        flex: '1 1 60%',
        overflowY: 'auto',
        padding: '24px',
        background: 'var(--bg-surface)',
        borderRight: '1px solid var(--border-default)',
      }}>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '20px',
        }}>
          <button
            onClick={onClose}
            style={{
              padding: '6px 12px',
              borderRadius: '6px',
              background: 'transparent',
              border: '1.5px solid var(--border)',
              color: 'var(--text-secondary)',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            ← Back to table
          </button>
        </div>

        <div style={{
          display: 'flex',
          gap: '8px',
          flexWrap: 'wrap',
          marginBottom: '20px',
        }}>
          <span style={{
            fontSize: '11px', fontWeight: 600, color: '#A855F7',
            background: 'rgba(168, 85, 247, 0.1)', padding: '4px 10px',
            borderRadius: '6px', textTransform: 'uppercase', letterSpacing: '0.5px',
          }}>
            {item.model || 'Unknown model'}
          </span>
          {totalTokens > 0 && (
            <span style={{
              fontSize: '11px', fontWeight: 600, color: '#10b981',
              background: 'rgba(16, 185, 129, 0.1)', padding: '4px 10px', borderRadius: '6px',
            }}>
              {totalTokens.toLocaleString()} tokens
            </span>
          )}
          {item.startTime && (
            <span style={{
              fontSize: '11px', color: 'var(--text-tertiary)',
              background: 'var(--bg-primary)', padding: '4px 10px', borderRadius: '6px', fontWeight: 600,
            }}>
              {new Date(item.startTime).toLocaleString('en-US', {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
              })}
            </span>
          )}
        </div>

        <div style={{
          fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)',
          marginBottom: '12px', textTransform: 'uppercase', letterSpacing: '0.5px',
        }}>
          Session ID
        </div>
        <div style={{
          fontFamily: 'monospace', fontSize: '12px', color: 'var(--text-secondary)',
          marginBottom: '24px',
        }}>
          {item.sessionId}
        </div>

        <div style={{
          fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)',
          marginBottom: '16px', textTransform: 'uppercase', letterSpacing: '0.5px',
        }}>
          Conversation
        </div>

        {turns.length > 0 ? (
          <div style={{ background: 'var(--bg-elevated)', padding: '16px', borderRadius: '8px' }}>
            {turns.map((turn, idx) => (
              <div
                key={`${turn.traceId}:${turn.spanId}`}
                style={{
                  marginBottom: idx < turns.length - 1 ? '20px' : '0',
                  paddingBottom: idx < turns.length - 1 ? '20px' : '0',
                  borderBottom: idx < turns.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                }}
              >
                <div style={{
                  fontSize: '11px', fontWeight: 700, color: '#7C3AED',
                  marginBottom: '12px', textTransform: 'uppercase', letterSpacing: '0.5px',
                }}>
                  Turn {idx + 1}
                </div>
                <TurnMessages turn={turn} />
              </div>
            ))}
          </div>
        ) : (
          <div style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>
            No conversation data available.
          </div>
        )}
      </div>

      <div style={{
        flex: '0 0 340px',
        padding: '24px',
        background: 'var(--bg-surface)',
        overflowY: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: '20px',
      }}>
        <div style={{
          fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)',
          textTransform: 'uppercase', letterSpacing: '0.5px',
        }}>
          Annotation
        </div>

        <div>
          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '8px' }}>
            First Pass
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            {(['looks_correct', 'doesnt_look_correct'] as FirstPassLabel[]).map((val) => {
              const isSelected = firstPass === val;
              const isPositive = val === 'looks_correct';
              return (
                <button
                  key={val}
                  onClick={() => setFirstPass(isSelected ? null : val)}
                  style={{
                    flex: 1,
                    padding: '10px',
                    borderRadius: '8px',
                    border: `1.5px solid ${isSelected
                      ? (isPositive ? 'rgba(16, 185, 129, 0.5)' : 'rgba(255, 87, 87, 0.5)')
                      : 'var(--border-default)'}`,
                    background: isSelected
                      ? (isPositive ? 'rgba(16, 185, 129, 0.1)' : 'rgba(255, 87, 87, 0.1)')
                      : 'var(--bg-primary)',
                    fontSize: '22px',
                    cursor: 'pointer',
                    transition: 'all 0.15s',
                  }}
                >
                  {isPositive ? '👍' : '👎'}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '8px' }}>
            Notes
          </div>
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Add optional notes..."
            rows={5}
            style={{
              width: '100%',
              padding: '10px 12px',
              borderRadius: '8px',
              border: '1.5px solid var(--border-default)',
              background: 'var(--bg-primary)',
              color: 'var(--text-primary)',
              fontSize: '13px',
              lineHeight: '1.6',
              resize: 'vertical',
              outline: 'none',
              fontFamily: 'inherit',
              boxSizing: 'border-box',
            }}
          />
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <button
            onClick={handleSave}
            disabled={!firstPass}
            style={{
              padding: '10px 24px',
              borderRadius: '8px',
              background: firstPass ? 'var(--accent-primary)' : 'var(--bg-elevated)',
              border: 'none',
              color: firstPass ? '#fff' : 'var(--text-tertiary)',
              fontSize: '13px',
              fontWeight: 600,
              cursor: firstPass ? 'pointer' : 'not-allowed',
              transition: 'all 0.2s',
              boxShadow: firstPass ? '0 0 16px rgba(168, 85, 247, 0.25)' : 'none',
            }}
          >
            Save Annotation
          </button>
          {saved && (
            <span style={{ fontSize: '12px', color: 'var(--status-success)', fontWeight: 600 }}>
              ✓ Saved
            </span>
          )}
        </div>

        {item.annotation && (
          <div style={{
            padding: '12px 16px',
            background: 'rgba(16, 185, 129, 0.05)',
            border: '1px solid rgba(16, 185, 129, 0.2)',
            borderRadius: '8px',
          }}>
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#10b981', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
              Current annotation
            </div>
            <div style={{ fontSize: '20px', lineHeight: 1 }}>
              {item.annotation.firstPass === 'looks_correct' ? '👍' : '👎'}
            </div>
            {item.annotation.comment && (
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                {item.annotation.comment}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
