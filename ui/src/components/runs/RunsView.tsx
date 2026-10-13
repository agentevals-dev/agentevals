import React, { useEffect, useMemo, useState } from 'react';
import { css } from '@emotion/react';
import { Segmented, Select } from 'antd';
import { RefreshCw, History, AlertCircle } from 'lucide-react';
import type { Run } from '../../lib/types';
import { listRuns, StorageUnavailableError } from '../../api/client';
import {
  groupRuns,
  metricNamesAcross,
  passRate,
  sortByCreatedAsc,
  type GroupBy,
} from './runHistory';
import { PassRateTrendChart } from './PassRateTrendChart';
import { PerMetricTrendChart } from './PerMetricTrendChart';
import { RunsHistoryTable } from './RunsHistoryTable';
import { RunDetailView } from './RunDetailView';

const ALL_GROUPS = '__all__';

export const RunsView: React.FC = () => {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const [groupBy, setGroupBy] = useState<GroupBy>('evalSet');
  const [selectedGroup, setSelectedGroup] = useState<string>(ALL_GROUPS);
  const [detailRunId, setDetailRunId] = useState<string | null>(null);

  // Group keys differ per dimension, so reset the picker to "all" when the
  // grouping dimension changes to avoid pointing at a now-nonexistent group.
  const changeGroupBy = (next: GroupBy) => {
    setGroupBy(next);
    setSelectedGroup(ALL_GROUPS);
  };

  const fetchRuns = () =>
    listRuns({ limit: 200 })
      .then(setRuns)
      .catch((err) => {
        if (err instanceof StorageUnavailableError) {
          setStorageUnavailable(true);
        } else {
          setError(err instanceof Error ? err.message : 'Failed to load runs');
        }
      })
      .finally(() => setLoading(false));

  const load = () => {
    setLoading(true);
    setError(null);
    setStorageUnavailable(false);
    void fetchRuns();
  };

  useEffect(() => {
    void fetchRuns();
  }, []);

  const groups = useMemo(() => groupRuns(runs, groupBy), [runs, groupBy]);

  const selectedRuns = useMemo(() => {
    if (selectedGroup === ALL_GROUPS) return runs;
    return groups.find(g => g.group.key === selectedGroup)?.runs ?? [];
  }, [runs, groups, selectedGroup]);

  const trendRuns = useMemo(() => sortByCreatedAsc(selectedRuns), [selectedRuns]);

  const stats = useMemo(() => {
    const rates = trendRuns.map(passRate).filter((r): r is number => r !== null);
    const latest = rates.length ? rates[rates.length - 1] : null;
    const avg = rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : null;
    return {
      total: selectedRuns.length,
      latest,
      avg,
      metrics: metricNamesAcross(selectedRuns).length,
    };
  }, [selectedRuns, trendRuns]);

  if (detailRunId) {
    return <RunDetailView runId={detailRunId} onBack={() => setDetailRunId(null)} />;
  }

  return (
    <div css={pageStyle}>
      <header css={headerStyle}>
        <div css={titleStyle}>
          <History size={22} />
          <h1>Run history</h1>
        </div>
        <div css={controlsStyle}>
          {runs.length > 0 && (
            <Segmented<GroupBy>
              value={groupBy}
              onChange={changeGroupBy}
              options={[
                { value: 'evalSet', label: 'Eval set' },
                { value: 'agent', label: 'Agent' },
              ]}
            />
          )}
          {groups.length > 0 && (
            <Select
              value={selectedGroup}
              onChange={setSelectedGroup}
              css={selectStyle}
              popupMatchSelectWidth={false}
              options={[
                {
                  value: ALL_GROUPS,
                  label: `All ${groupBy === 'agent' ? 'agents' : 'eval sets'} (${runs.length})`,
                },
                ...groups.map(g => ({
                  value: g.group.key,
                  label: `${g.group.label} (${g.runs.length})`,
                })),
              ]}
            />
          )}
          <button css={refreshButtonStyle} onClick={load} disabled={loading}>
            <RefreshCw size={14} className={loading ? 'spinning' : undefined} />
            Refresh
          </button>
        </div>
      </header>

      {storageUnavailable && (
        <div css={noticeStyle}>
          <AlertCircle size={18} />
          <span>
            Run history requires the durable storage backend. Start the server with{' '}
            <code>AGENTEVALS_STORAGE_BACKEND=postgres</code> and <code>AGENTEVALS_DATABASE_URL</code>{' '}
            set, then run an evaluation to populate it.
          </span>
        </div>
      )}

      {error && (
        <div css={[noticeStyle, errorNoticeStyle]}>
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      )}

      {loading && <p css={mutedTextStyle}>Loading runs...</p>}

      {!loading && !storageUnavailable && !error && runs.length === 0 && (
        <div css={emptyStateStyle}>
          <History size={32} />
          <p>No evaluation runs recorded yet.</p>
          <span>Run an evaluation and it will appear here, tracked over time.</span>
        </div>
      )}

      {!loading && !storageUnavailable && runs.length > 0 && (
        <>
          <div css={statsGridStyle}>
            <StatCard label="Runs" value={String(stats.total)} />
            <StatCard
              label="Latest pass rate"
              value={stats.latest === null ? '-' : `${Math.round(stats.latest * 100)}%`}
              accent={stats.latest !== null && stats.latest < 0.5 ? 'failure' : 'success'}
            />
            <StatCard
              label="Avg pass rate"
              value={stats.avg === null ? '-' : `${Math.round(stats.avg * 100)}%`}
            />
            <StatCard label="Metrics tracked" value={String(stats.metrics)} />
          </div>

          <div css={chartsGridStyle}>
            <PassRateTrendChart runs={trendRuns} />
            <PerMetricTrendChart runs={trendRuns} />
          </div>

          <RunsHistoryTable runs={selectedRuns} onSelectRun={setDetailRunId} />
        </>
      )}
    </div>
  );
};

interface StatCardProps {
  label: string;
  value: string;
  accent?: 'success' | 'failure';
}

const StatCard: React.FC<StatCardProps> = ({ label, value, accent }) => {
  const color =
    accent === 'failure'
      ? 'var(--status-failure)'
      : accent === 'success'
        ? 'var(--status-success)'
        : 'var(--accent-primary)';
  return (
    <div css={statCardStyle}>
      <div css={statValueStyle} style={{ color }}>
        {value}
      </div>
      <div css={statLabelStyle}>{label}</div>
    </div>
  );
};

const pageStyle = css`
  padding: 32px;
  max-width: 1400px;
  margin: 0 auto;
`;

const headerStyle = css`
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
  gap: 16px;
  flex-wrap: wrap;
`;

const titleStyle = css`
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--accent-primary);

  h1 {
    margin: 0;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-primary);
    font-family: var(--font-display);
  }
`;

const controlsStyle = css`
  display: flex;
  align-items: center;
  gap: 12px;
`;

const selectStyle = css`
  min-width: 200px;
`;

const refreshButtonStyle = css`
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-weight: 600;
  font-size: 0.8125rem;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover:not(:disabled) {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  &:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .spinning {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }
`;

const noticeStyle = css`
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  border-radius: 8px;
  margin-bottom: 24px;
  background: linear-gradient(135deg, rgba(59, 130, 246, 0.08) 0%, rgba(139, 92, 246, 0.08) 100%);
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  font-size: 0.875rem;

  code {
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    color: var(--accent-primary);
    background: var(--bg-elevated);
    padding: 1px 6px;
    border-radius: 4px;
  }
`;

const errorNoticeStyle = css`
  background: rgba(255, 87, 87, 0.08);
  color: var(--status-failure);
`;

const mutedTextStyle = css`
  color: var(--text-tertiary);
  font-size: 0.9rem;
`;

const emptyStateStyle = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 80px 20px;
  color: var(--text-tertiary);
  text-align: center;

  p {
    margin: 8px 0 0;
    font-size: 1.05rem;
    color: var(--text-secondary);
    font-weight: 600;
  }

  span {
    font-size: 0.875rem;
  }
`;

const statsGridStyle = css`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
`;

const statCardStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  padding: 20px 24px;
  text-align: center;
`;

const statValueStyle = css`
  font-size: 2rem;
  font-weight: 700;
  font-family: var(--font-mono);
  margin-bottom: 4px;
`;

const statLabelStyle = css`
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-tertiary);
  font-weight: 600;
`;

const chartsGridStyle = css`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-bottom: 24px;

  @media (max-width: 1100px) {
    grid-template-columns: 1fr;
  }
`;
