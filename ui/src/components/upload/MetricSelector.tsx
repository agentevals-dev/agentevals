import React, { useState, useEffect } from 'react';
import { Checkbox, Button } from 'antd';
import { css } from '@emotion/react';
import { AVAILABLE_METRICS, type MetricMetadata } from '../../lib/types';
import { listMetrics } from '../../api/client';

interface MetricSelectorProps {
  selectedMetrics: string[];
  onToggleMetric: (metric: string) => void;
  loadFromAPI?: boolean;
}

const selectorStyle = css`
  display: flex;
  flex-direction: column;
  height: 100%;

  .metric-categories {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .metric-category {
    background-color: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
  }

  .category-title {
    color: var(--accent-primary);
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    margin-bottom: 10px;
    letter-spacing: 0.5px;
  }

  .metric-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .metric-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .metric-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 10px;
  }

  .metric-name {
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .metric-description {
    color: var(--text-secondary);
    font-size: 11px;
    margin-left: 24px;
    line-height: 1.3;
    overflow-wrap: break-word;
    word-break: break-word;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .metric-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    flex-shrink: 0;
  }

  .metric-badge {
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 3px;
    font-weight: 500;
    line-height: 1.4;
  }

  .badge-eval-set {
    background-color: rgba(124, 255, 107, 0.1);
    color: var(--status-success);
    border: 1px solid rgba(124, 255, 107, 0.3);
  }

  .badge-llm {
    background-color: rgba(168, 85, 247, 0.1);
    color: var(--accent-primary);
    border: 1px solid rgba(168, 85, 247, 0.3);
  }

  .badge-gcp {
    background-color: rgba(124, 58, 237, 0.1);
    color: #60A5FA;
    border: 1px solid rgba(124, 58, 237, 0.3);
  }

  .badge-rubrics {
    background-color: rgba(251, 146, 60, 0.1);
    color: #FB923C;
    border: 1px solid rgba(251, 146, 60, 0.3);
  }

  .badge-incomplete {
    background-color: rgba(239, 68, 68, 0.1);
    color: #EF4444;
    border: 1px solid rgba(239, 68, 68, 0.3);
  }

  .selector-actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-default);
  }

  .ant-checkbox-wrapper {
    color: var(--text-primary);
  }

  .ant-checkbox-checked .ant-checkbox-inner {
    background-color: var(--accent-primary);
    border-color: var(--accent-primary);
  }
`;

let cachedMetrics: MetricMetadata[] | null = null;

export const MetricSelector: React.FC<MetricSelectorProps> = ({
  selectedMetrics,
  onToggleMetric,
  loadFromAPI = false,
}) => {
  const [metrics, setMetrics] = useState<MetricMetadata[]>(cachedMetrics ?? AVAILABLE_METRICS);

  useEffect(() => {
    if (!loadFromAPI || cachedMetrics) return;
    let cancelled = false;
    listMetrics()
      .then((apiMetrics) => {
        if (!cancelled) {
          cachedMetrics = apiMetrics;
          setMetrics(apiMetrics);
        }
      })
      .catch((error) => {
        console.error('Failed to load metrics from API, using fallback:', error);
      });
    return () => { cancelled = true; };
  }, [loadFromAPI]);

  const categorizedMetrics = metrics.reduce(
    (acc, metric) => {
      if (!acc[metric.category]) {
        acc[metric.category] = [];
      }
      acc[metric.category].push(metric);
      return acc;
    },
    {} as Record<string, MetricMetadata[]>
  );

  const handleSelectAll = () => {
    metrics.forEach((metric) => {
      if (!selectedMetrics.includes(metric.name)) {
        onToggleMetric(metric.name);
      }
    });
  };

  const handleClearAll = () => {
    selectedMetrics.forEach((metric) => {
      onToggleMetric(metric);
    });
  };

  return (
    <div css={selectorStyle}>
      <div className="metric-categories">
        {Object.entries(categorizedMetrics).map(([category, metrics]) => (
          <div key={category} className="metric-category">
            <div className="category-title">{category}</div>
            <div className="metric-list">
              {metrics.map((metric) => (
                <div key={metric.name} className="metric-item">
                  <div className="metric-row">
                    <Checkbox
                      checked={selectedMetrics.includes(metric.name)}
                      onChange={() => onToggleMetric(metric.name)}
                    >
                      <span className="metric-name">{metric.name}</span>
                    </Checkbox>
                    <div className="metric-badges">
                      {metric.requiresEvalSet && (
                        <span className="metric-badge badge-eval-set">Requires Eval Set</span>
                      )}
                      {metric.requiresLLM && (
                        <span className="metric-badge badge-llm">Uses LLM</span>
                      )}
                      {metric.requiresGCP && (
                        <span className="metric-badge badge-gcp">Requires GCP</span>
                      )}
                      {metric.requiresRubrics && (
                        <span className="metric-badge badge-rubrics">Requires Rubrics</span>
                      )}
                      {metric.working === false && (
                        <span className="metric-badge badge-incomplete">Incomplete</span>
                      )}
                    </div>
                  </div>
                  <div className="metric-description" title={metric.description}>{metric.description}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="selector-actions">
        <Button size="small" onClick={handleSelectAll}>
          Select All
        </Button>
        <Button size="small" onClick={handleClearAll}>
          Clear All
        </Button>
      </div>
    </div>
  );
};
