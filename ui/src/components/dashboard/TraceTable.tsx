import React from 'react';
import { Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { css } from '@emotion/react';
import { Clock, User, Cpu, MessageSquare, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import type { TraceTableRow } from '../../lib/types';
import { formatTimestamp } from '../../lib/utils';

interface TraceTableProps {
  rows: TraceTableRow[];
  selectedMetrics: string[];
  threshold: number;
  onRowClick: (traceId: string) => void;
  isEvaluating: boolean;
}

const tableStyle = css`
  .ant-table {
    background: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
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

    &:hover {
      background: var(--bg-elevated);
      box-shadow: 0 2px 8px rgba(0, 217, 255, 0.1);
    }

    &.pending-row {
      cursor: default;
      opacity: 0.7;
    }
  }

  .ant-table-tbody > tr > td {
    padding: 12px 16px;
    color: var(--text-primary);
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
`;

export const TraceTable: React.FC<TraceTableProps> = ({
  rows,
  selectedMetrics,
  threshold,
  onRowClick,
  isEvaluating,
}) => {
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
      title: 'Input',
      dataIndex: 'userInputPreview',
      key: 'userInputPreview',
      width: 200,
      ellipsis: true,
      render: (text: string | undefined) => {
        if (!text) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }
        return (
          <div className="cell-with-icon">
            <MessageSquare size={14} />
            <span className="preview-text">{text}</span>
          </div>
        );
      },
    },
    {
      title: 'Output',
      dataIndex: 'finalOutputPreview',
      key: 'finalOutputPreview',
      width: 200,
      ellipsis: true,
      render: (text: string | undefined) => {
        if (!text) {
          return (
            <div className="loading-cell">
              <Loader2 size={14} className="animate-spin" />
            </div>
          );
        }
        return <span className="preview-text">{text}</span>;
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
      width: 140,
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

  return (
    <div css={tableStyle}>
      <Table
        columns={columns}
        dataSource={rows}
        rowKey="traceId"
        pagination={false}
        onRow={(record) => ({
          onClick: () => {
            if (record.agentName || record.startTime || record.model) {
              onRowClick(record.traceId);
            }
          },
          className: (!record.agentName && !record.startTime && !record.model) ? 'pending-row' : '',
        })}
        loading={isEvaluating && rows.length === 0}
      />
    </div>
  );
};
