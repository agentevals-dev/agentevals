import React, { useState } from 'react';
import { Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { css } from '@emotion/react';
import { Clock, User, Cpu, MessageSquare, CheckCircle2, XCircle, Loader2, ChevronRight, ChevronDown } from 'lucide-react';
import type { TraceTableRow } from '../../lib/types';
import { formatTimestamp } from '../../lib/utils';

interface TraceTableProps {
  rows: TraceTableRow[];
  selectedMetrics: string[];
  threshold: number;
  onRowClick: (traceId: string) => void;
  onRowHover?: (traceId: string | null) => void;
  isEvaluating: boolean;
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
      border-left-color: var(--accent-cyan);
      outline-color: var(--accent-cyan);
      background: transparent !important;
    }

    &.pending-row {
      cursor: default;
      opacity: 0.7;
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

  .loading-cell {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .metric-cell {
    display: flex;
    align-items: center;
    gap: 6px;
    justify-content: center;
  }

  .score-badge {
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
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

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  .animate-spin {
    animation: spin 1s linear infinite;
  }

  .ant-table-expanded-row > td {
    padding: 16px 16px 16px 16px !important;
    background: var(--bg-surface) !important;
  }

  .ant-table-expanded-row:hover > td {
    background: var(--bg-surface) !important;
  }
`;

const extractText = (parts: any[]): string => {
  if (!parts) return '';
  return parts
    .filter(p => p.text)
    .map(p => p.text)
    .join(' ');
};

export const TraceTable: React.FC<TraceTableProps> = ({
  rows,
  selectedMetrics,
  threshold,
  onRowClick,
  onRowHover,
  isEvaluating,
}) => {
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([]);
  const columns: ColumnsType<TraceTableRow> = [
    {
      title: 'Name',
      dataIndex: 'agentName',
      key: 'agentName',
      width: 150,
      render: (name: string | undefined) => {
        if (!name) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
              Loading...
            </div>
          );
        }
        return (
          <div className="cell-with-icon">
            <User size={14} />
            {name}
          </div>
        );
      },
    },
    {
      title: 'Session ID',
      dataIndex: 'sessionId',
      key: 'sessionId',
      width: 120,
      render: (sessionId: string | undefined, record: TraceTableRow) => {
        const displayId = sessionId || record.traceId.substring(0, 12);
        return (
          <span style={{ fontFamily: 'monospace', fontSize: 12, color: 'var(--text-primary)', fontWeight: 500 }}>
            {displayId}
          </span>
        );
      },
    },
    {
      title: 'Start Time',
      dataIndex: 'startTime',
      key: 'startTime',
      width: 180,
      render: (time: number | undefined) => {
        if (!time) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }
        return (
          <div className="cell-with-icon">
            <Clock size={14} />
            {formatTimestamp(time)}
          </div>
        );
      },
    },
    {
      title: 'Conversation',
      key: 'conversation',
      width: 300,
      ellipsis: true,
      render: (_: any, record: TraceTableRow) => {
        if (!record.userInputPreview) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }

        const numTurns = record.invocations?.length || record.numInvocations || 1;
        const isExpanded = expandedRowKeys.includes(record.traceId);

        if (numTurns > 1) {
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span
                onClick={(e) => {
                  e.stopPropagation();
                  if (isExpanded) {
                    setExpandedRowKeys(expandedRowKeys.filter(k => k !== record.traceId));
                  } else {
                    setExpandedRowKeys([...expandedRowKeys, record.traceId]);
                  }
                }}
                style={{ cursor: 'pointer', display: 'flex', alignItems: 'center' }}
              >
                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </span>
              <MessageSquare size={14} />
              <span className="preview-text" style={{ fontWeight: 600 }}>
                {numTurns} turns
              </span>
            </div>
          );
        }

        return (
          <div className="cell-with-icon">
            <MessageSquare size={14} />
            <span className="preview-text">{record.userInputPreview}</span>
          </div>
        );
      },
    },
    {
      title: 'Model',
      dataIndex: 'model',
      key: 'model',
      width: 150,
      render: (model: string | undefined) => {
        if (!model) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }
        return (
          <div className="cell-with-icon">
            <Cpu size={14} />
            <span style={{ fontSize: 12 }}>{model}</span>
          </div>
        );
      },
    },
    ...selectedMetrics.map((metricName) => ({
      title: metricName.replace(/_/g, ' ').toUpperCase(),
      key: metricName,
      width: 160,
      render: (_: any, record: TraceTableRow) => {
        const metricResult = record.metricResults.get(metricName);

        if (!metricResult) {
          return (
            <div className="metric-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }

        if (metricResult.error) {
          return (
            <div className="metric-cell">
              <XCircle size={14} color="var(--status-failure)" />
              <span style={{ fontSize: 11, color: 'var(--status-failure)' }}>Error</span>
            </div>
          );
        }

        const score = metricResult.score;
        const passed = score !== null && score >= threshold;

        return (
          <div className="metric-cell">
            {passed ? (
              <CheckCircle2 size={14} color="var(--status-success)" />
            ) : (
              <XCircle size={14} color="var(--status-failure)" />
            )}
            <span
              className="score-badge"
              style={{
                backgroundColor: passed
                  ? 'rgba(46, 213, 115, 0.2)'
                  : 'rgba(255, 87, 87, 0.2)',
                color: passed ? 'var(--status-success)' : 'var(--status-failure)',
              }}
            >
              {score !== null ? score.toFixed(2) : 'N/A'}
            </span>
          </div>
        );
      },
    })),
  ];

  const expandedRowRender = (record: TraceTableRow) => {
    const invocations = record.invocations || [];
    if (invocations.length === 0) return null;

    return (
      <div style={{
        background: 'var(--bg-elevated)',
        padding: '16px',
        borderRadius: '8px',
        marginLeft: '40px',
      }}>
        {invocations.map((inv, idx) => {
          const userText = extractText(inv.userContent?.parts || []);
          const agentText = extractText(inv.finalResponse?.parts || []);
          const toolCalls = inv.intermediateData?.toolUses || [];

          return (
            <div
              key={inv.invocationId}
              style={{
                marginBottom: idx < invocations.length - 1 ? '20px' : '0',
                paddingBottom: idx < invocations.length - 1 ? '20px' : '0',
                borderBottom: idx < invocations.length - 1 ? '1px solid var(--border-subtle)' : 'none',
              }}
            >
              <div style={{
                fontSize: '11px',
                fontWeight: 700,
                color: '#3b82f6',
                marginBottom: '12px',
                textTransform: 'uppercase',
                letterSpacing: '0.5px',
              }}>
                Turn {idx + 1}
              </div>

              <div style={{ marginBottom: '12px' }}>
                <div style={{
                  fontSize: '10px',
                  fontWeight: 600,
                  color: 'var(--text-tertiary)',
                  marginBottom: '6px',
                  textTransform: 'uppercase',
                  letterSpacing: '0.3px',
                }}>
                  User Input
                </div>
                <div style={{
                  fontSize: '13px',
                  color: 'var(--text-primary)',
                  lineHeight: '1.6',
                  padding: '8px 12px',
                  background: 'var(--bg-surface)',
                  borderRadius: '6px',
                  borderLeft: '3px solid #3b82f6',
                }}>
                  {userText || '(no text)'}
                </div>
              </div>

              {toolCalls.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <div style={{
                    fontSize: '10px',
                    fontWeight: 600,
                    color: 'var(--text-tertiary)',
                    marginBottom: '6px',
                    textTransform: 'uppercase',
                    letterSpacing: '0.3px',
                  }}>
                    Tool Calls
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {toolCalls.map((tc, i) => (
                      <div
                        key={i}
                        style={{
                          fontSize: '12px',
                          color: '#8b5cf6',
                          fontFamily: 'monospace',
                          background: 'rgba(139, 92, 246, 0.08)',
                          padding: '6px 10px',
                          borderRadius: '6px',
                          fontWeight: 500,
                        }}
                      >
                        {tc.name}({Object.keys(tc.args || {}).length > 0 ? Object.keys(tc.args).map(k => `${k}=${JSON.stringify(tc.args[k])}`).join(', ') : ''})
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <div style={{
                  fontSize: '10px',
                  fontWeight: 600,
                  color: 'var(--text-tertiary)',
                  marginBottom: '6px',
                  textTransform: 'uppercase',
                  letterSpacing: '0.3px',
                }}>
                  Agent Response
                </div>
                <div style={{
                  fontSize: '13px',
                  color: 'var(--text-primary)',
                  lineHeight: '1.6',
                  padding: '8px 12px',
                  background: 'var(--bg-surface)',
                  borderRadius: '6px',
                  borderLeft: '3px solid #10b981',
                }}>
                  {agentText || '(no text)'}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div css={tableStyle}>
      <Table
        columns={columns}
        dataSource={rows}
        rowKey="traceId"
        pagination={false}
        scroll={{ x: 'max-content' }}
        expandable={{
          expandedRowKeys,
          onExpand: (expanded, record) => {
            if (expanded) {
              setExpandedRowKeys([...expandedRowKeys, record.traceId]);
            } else {
              setExpandedRowKeys(expandedRowKeys.filter(k => k !== record.traceId));
            }
          },
          expandedRowRender,
          rowExpandable: (record) => (record.invocations?.length || 0) > 1,
          expandIcon: () => null, // Hide default expand icon since we show it in the Conversation column
        }}
        onRow={(record) => ({
          onClick: () => {
            if (record.agentName || record.startTime || record.model) {
              onRowClick(record.traceId);
            }
          },
          onMouseEnter: () => {
            if (onRowHover && (record.agentName || record.startTime || record.model)) {
              onRowHover(record.traceId);
            }
          },
          onMouseLeave: () => {
            if (onRowHover) {
              onRowHover(null);
            }
          },
          className: (!record.agentName && !record.startTime && !record.model) ? 'pending-row' : '',
        })}
        loading={isEvaluating && rows.length === 0}
      />
    </div>
  );
};
