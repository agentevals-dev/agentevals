import React from 'react';
import { css } from '@emotion/react';
import { Bar } from 'react-chartjs-2';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';
import type { TraceResult } from '../../lib/types';

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend
);

interface PerformanceChartsProps {
  traceResults: TraceResult[];
  hoveredTraceId?: string | null;
}

export const PerformanceCharts: React.FC<PerformanceChartsProps> = ({ traceResults, hoveredTraceId }) => {
  const tracesWithPerf = traceResults.filter(tr => tr.performanceMetrics);

  if (tracesWithPerf.length === 0) {
    return null;
  }

  // Sort by trace ID to maintain consistent ordering
  const sortedTraces = [...tracesWithPerf].sort((a, b) => a.traceId.localeCompare(b.traceId));

  const labels = sortedTraces.map(tr => tr.traceId.substring(0, 8));

  const hoveredIndex = hoveredTraceId
    ? sortedTraces.findIndex(tr => tr.traceId === hoveredTraceId)
    : -1;

  const getBackgroundColor = (baseColor: string, dataLength: number) => {
    if (hoveredIndex === -1) {
      return Array.from({ length: dataLength }, () => baseColor);
    }
    return Array.from({ length: dataLength }, (_, i) =>
      i === hoveredIndex ? baseColor : baseColor.replace('0.2)', '0.1)')
    );
  };

  const getBorderColor = (baseColor: string, dataLength: number) => {
    const solidColor = baseColor.replace('rgba', 'rgb').replace(/, 0\.\d+\)/, ')');
    if (hoveredIndex === -1) {
      return Array.from({ length: dataLength }, () => solidColor);
    }
    return Array.from({ length: dataLength }, (_, i) =>
      i === hoveredIndex ? solidColor : solidColor.replace('rgb', 'rgba').replace(')', ', 0.3)')
    );
  };

  const latencyData = {
    labels,
    datasets: [
      {
        label: 'p50',
        data: sortedTraces.map(tr => tr.performanceMetrics?.latency.overall.p50 || 0),
        backgroundColor: getBackgroundColor('rgba(75, 192, 192, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(75, 192, 192, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
      {
        label: 'p95',
        data: sortedTraces.map(tr => tr.performanceMetrics?.latency.overall.p95 || 0),
        backgroundColor: getBackgroundColor('rgba(255, 159, 64, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(255, 159, 64, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
      {
        label: 'p99',
        data: sortedTraces.map(tr => tr.performanceMetrics?.latency.overall.p99 || 0),
        backgroundColor: getBackgroundColor('rgba(255, 99, 132, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(255, 99, 132, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
    ],
  };

  const tokenData = {
    labels,
    datasets: [
      {
        label: 'Total Tokens',
        data: sortedTraces.map(tr => tr.performanceMetrics?.tokens.total || 0),
        backgroundColor: getBackgroundColor('rgba(153, 102, 255, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(153, 102, 255, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
      {
        label: 'Prompt Tokens',
        data: sortedTraces.map(tr => tr.performanceMetrics?.tokens.totalPrompt || 0),
        backgroundColor: getBackgroundColor('rgba(54, 162, 235, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(54, 162, 235, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
      {
        label: 'Output Tokens',
        data: sortedTraces.map(tr => tr.performanceMetrics?.tokens.totalOutput || 0),
        backgroundColor: getBackgroundColor('rgba(255, 206, 86, 0.2)', sortedTraces.length),
        borderColor: getBorderColor('rgba(255, 206, 86, 0.2)', sortedTraces.length),
        borderWidth: 2,
      },
    ],
  };

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'bottom' as const,
        labels: {
          color: 'rgb(209, 213, 219)',
          font: {
            size: 13,
          },
          padding: 15,
          usePointStyle: true,
        },
      },
      tooltip: {
        backgroundColor: 'rgba(0, 0, 0, 0.9)',
        titleColor: '#fff',
        bodyColor: '#fff',
        padding: 12,
        cornerRadius: 6,
      },
    },
    scales: {
      y: {
        beginAtZero: true,
        ticks: {
          color: 'rgb(209, 213, 219)',
          font: {
            size: 12,
          },
        },
        grid: {
          color: 'rgba(209, 213, 219, 0.15)',
        },
      },
      x: {
        ticks: {
          color: 'rgb(209, 213, 219)',
          font: {
            size: 12,
          },
        },
        grid: {
          color: 'rgba(209, 213, 219, 0.15)',
        },
      },
    },
  };

  const latencyOptions = {
    ...chartOptions,
    scales: {
      ...chartOptions.scales,
      y: {
        ...chartOptions.scales.y,
        title: {
          display: true,
          text: 'Latency (ms)',
          color: 'rgb(209, 213, 219)',
          font: {
            size: 13,
          },
        },
      },
    },
  };

  const tokenOptions = {
    ...chartOptions,
    scales: {
      ...chartOptions.scales,
      y: {
        ...chartOptions.scales.y,
        title: {
          display: true,
          text: 'Tokens',
          color: 'rgb(209, 213, 219)',
          font: {
            size: 13,
          },
        },
      },
    },
  };

  return (
    <div css={chartsContainerStyle}>
      <div css={chartCardStyle}>
        <h3>Latency Across Traces</h3>
        <div css={chartWrapperStyle}>
          <Bar data={latencyData} options={latencyOptions} />
        </div>
      </div>

      <div css={chartCardStyle}>
        <h3>Token Usage Across Traces</h3>
        <div css={chartWrapperStyle}>
          <Bar data={tokenData} options={tokenOptions} />
        </div>
      </div>
    </div>
  );
};

const chartsContainerStyle = css`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-bottom: 24px;

  @media (max-width: 1400px) {
    grid-template-columns: 1fr;
  }
`;

const chartCardStyle = css`
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 20px;

  h3 {
    margin: 0 0 16px 0;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
  }
`;

const chartWrapperStyle = css`
  height: 350px;
  position: relative;
`;
