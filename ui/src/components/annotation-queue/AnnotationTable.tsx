import React, { useState } from 'react';
import { Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { css } from '@emotion/react';
import { Clock, User, Cpu, MessageSquare, ChevronRight, ChevronDown } from 'lucide-react';
import type { AnnotationQueueItem } from '../../lib/types';
import { TurnMessages } from '../streaming/LiveConversationPanel';

interface AnnotationTableProps {
  items: AnnotationQueueItem[];
  selectedSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  selectedGoldenId: string | null;
  onSelectGolden: (sessionId: string) => void;
}

const tableStyle = css`
  width: 100%;

  .ant-table {
    background: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  }

  .ant-table-container {
    width: 100%;
  }

  .ant-table-content {
    overflow-x: auto;
  }

  .ant-table-thead > tr > th {
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-weight: 600;
    border-bottom: 2px solid var(--border-default);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 12px 16px;
  }

  .ant-table-tbody > tr {
    cursor: pointer;
    transition: all 0.2s ease;
    border-bottom: 1px solid var(--border-subtle);
    border-left: 4px solid transparent;
    background: transparent !important;
    outline: 2px solid transparent;
    outline-offset: -2px;

    &:hover {
      border-left-color: var(--accent-primary);
      outline-color: var(--accent-primary);
      background: transparent !important;
    }

    &.selected-row {
      border-left-color: #7C3AED;
      outline-color: #7C3AED;
    }
  }

  .ant-table-tbody > tr > td {
    padding: 12px 16px;
    color: var(--text-primary);
    background: transparent !important;
  }

  .ant-table-tbody > tr:hover > td {
    background: transparent !important;
  }

  .ant-table-thead > tr > th,
  .ant-table-tbody > tr > td {
    border-color: var(--border-default);
  }

  .ant-table-tbody > tr:last-child > td {
    border-bottom: none;
  }

  .cell-with-icon {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .preview-text {
    font-size: 12px;
    color: var(--text-secondary);
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ant-table-expanded-row > td {
    padding: 16px !important;
    background: var(--bg-surface) !important;
  }

  .ant-table-expanded-row:hover > td {
    background: var(--bg-surface) !important;
  }
`;

export const AnnotationTable: React.FC<AnnotationTableProps> = ({
  items,
  selectedSessionId,
  onSelectSession,
  selectedGoldenId,
  onSelectGolden,
}) => {
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([]);

  const columns: ColumnsType<AnnotationQueueItem> = [
    {
      title: 'Start Time',
      dataIndex: 'startTime',
      key: 'startTime',
      width: 180,
      render: (startTime: string | undefined) => {
        if (!startTime) return <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>—</span>;
        return (
          <div className="cell-with-icon">
            <Clock size={14} />
            <span style={{ fontSize: 12 }}>{new Date(startTime).toLocaleString('en-US', {
              month: 'short', day: 'numeric',
              hour: '2-digit', minute: '2-digit', second: '2-digit',
            })}</span>
          </div>
        );
      },
    },
    {
      title: 'Agent Name',
      dataIndex: 'agentName',
      key: 'agentName',
      width: 150,
      render: (name: string | undefined) => (
        <div className="cell-with-icon">
          <User size={14} />
          <span style={{ fontSize: 13 }}>{name || '—'}</span>
        </div>
      ),
    },
    {
      title: 'Session ID',
      dataIndex: 'sessionId',
      key: 'sessionId',
      width: 140,
      render: (sessionId: string) => (
        <span style={{ fontFamily: 'monospace', fontSize: 12, color: 'var(--text-primary)', fontWeight: 500 }}>
          {sessionId}
        </span>
      ),
    },
    {
      title: 'Conversation',
      key: 'conversation',
      width: 280,
      render: (_: unknown, record: AnnotationQueueItem) => {
        const numTurns = record.session.turns.length;
        const isExpanded = expandedRowKeys.includes(record.sessionId);
        const firstUserInput = record.session.summary?.firstUserText ?? '';

        if (numTurns > 1) {
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span
                onClick={(e) => {
                  e.stopPropagation();
                  setExpandedRowKeys(isExpanded
                    ? expandedRowKeys.filter(k => k !== record.sessionId)
                    : [...expandedRowKeys, record.sessionId]);
                }}
                style={{ cursor: 'pointer', display: 'flex', alignItems: 'center' }}
              >
                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </span>
              <MessageSquare size={14} />
              <span style={{ fontWeight: 600, fontSize: 12 }}>{numTurns} turns</span>
            </div>
          );
        }

        return (
          <div className="cell-with-icon">
            <MessageSquare size={14} />
            <span className="preview-text">{firstUserInput || '—'}</span>
          </div>
        );
      },
    },
    {
      title: 'Model',
      dataIndex: 'model',
      key: 'model',
      width: 150,
      render: (model: string | undefined) => (
        <div className="cell-with-icon">
          <Cpu size={14} />
          <span style={{ fontSize: 12 }}>{model || '—'}</span>
        </div>
      ),
    },
    {
      title: 'Tokens',
      dataIndex: 'totalTokens',
      key: 'totalTokens',
      width: 100,
      render: (tokens: number | undefined) => (
        <span style={{ fontSize: 12, color: tokens ? '#10b981' : 'var(--text-tertiary)' }}>
          {tokens ? tokens.toLocaleString() : '—'}
        </span>
      ),
    },
    {
      title: 'Annotated',
      key: 'annotated',
      width: 110,
      render: (_: unknown, record: AnnotationQueueItem) => {
        if (!record.annotation) {
          return (
            <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>—</span>
          );
        }
        return (
          <span style={{ fontSize: '18px', lineHeight: 1 }}>
            {record.annotation.firstPass === 'looks_correct' ? '👍' : '👎'}
          </span>
        );
      },
    },
    {
      title: 'EvalSet',
      key: 'evalset',
      width: 130,
      render: (_: unknown, record: AnnotationQueueItem) => {
        const isGolden = selectedGoldenId === record.sessionId;
        if (record.session.turns.length === 0) return <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>—</span>;
        return (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onSelectGolden(record.sessionId);
            }}
            style={{
              padding: '5px 12px',
              borderRadius: '6px',
              fontSize: '12px',
              fontWeight: 600,
              background: isGolden ? '#7C3AED' : 'transparent',
              border: isGolden ? 'none' : '1.5px solid var(--border)',
              color: isGolden ? 'white' : 'var(--text-primary)',
              cursor: 'pointer',
              transition: 'all 0.2s',
              whiteSpace: 'nowrap',
            }}
          >
            {isGolden ? 'EvalSet ✓' : 'Set as EvalSet'}
          </button>
        );
      },
    },
  ];

  const expandedRowRender = (record: AnnotationQueueItem) => {
    const turns = record.session.turns;
    if (turns.length === 0) return null;

    return (
      <div style={{
        background: 'var(--bg-elevated)',
        padding: '16px',
        borderRadius: '8px',
        marginLeft: '40px',
      }}>
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
    );
  };

  return (
    <div css={tableStyle}>
      <Table
        columns={columns}
        dataSource={items}
        rowKey="sessionId"
        pagination={false}
        scroll={{ x: 'max-content' }}
        expandable={{
          expandedRowKeys,
          onExpand: (expanded, record) => {
            setExpandedRowKeys(expanded
              ? [...expandedRowKeys, record.sessionId]
              : expandedRowKeys.filter(k => k !== record.sessionId));
          },
          expandedRowRender,
          rowExpandable: (record) => record.session.turns.length > 1,
          expandIcon: () => null,
        }}
        onRow={(record) => ({
          onClick: () => onSelectSession(record.sessionId),
          className: selectedSessionId === record.sessionId ? 'selected-row' : '',
        })}
      />
    </div>
  );
};
