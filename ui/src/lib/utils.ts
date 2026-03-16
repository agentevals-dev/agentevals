import type { EvalStatus } from './types';

/**
 * Format duration from microseconds to human-readable string
 */
export function formatDuration(microseconds: number): string {
  const ms = microseconds / 1000;

  if (ms < 1) {
    return `${microseconds}μs`;
  } else if (ms < 1000) {
    return `${ms.toFixed(2)}ms`;
  } else {
    const seconds = ms / 1000;
    if (seconds < 60) {
      return `${seconds.toFixed(2)}s`;
    } else {
      const minutes = Math.floor(seconds / 60);
      const remainingSeconds = seconds % 60;
      return `${minutes}m ${remainingSeconds.toFixed(1)}s`;
    }
  }
}

/**
 * Format timestamp from microseconds to human-readable string
 */
export function formatTimestamp(microseconds: number): string {
  const date = new Date(microseconds / 1000);
  return date.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/**
 * Truncate trace ID for display
 */
export function truncateTraceId(traceId: string, length: number = 12): string {
  if (traceId.length <= length) return traceId;
  return `${traceId.substring(0, length)}...`;
}

/**
 * Get color for status
 */
export function getStatusColor(status: EvalStatus): string {
  switch (status) {
    case 'PASSED':
      return 'var(--status-success)';
    case 'FAILED':
      return 'var(--status-failure)';
    case 'NOT_EVALUATED':
      return 'var(--text-muted)';
    case 'ERROR':
      return 'var(--status-warning)';
    default:
      return 'var(--text-secondary)';
  }
}

/**
 * Get glow effect for status
 */
export function getStatusGlow(status: EvalStatus): string {
  switch (status) {
    case 'PASSED':
      return 'var(--glow-success)';
    case 'FAILED':
      return 'var(--glow-failure)';
    case 'NOT_EVALUATED':
      return 'none';
    case 'ERROR':
      return 'var(--glow-warning)';
    default:
      return 'none';
  }
}

/**
 * Get color for score value
 */
export function getScoreColor(score: number, threshold: number = 0.8): string {
  if (score >= threshold) {
    return 'var(--status-success)';
  } else if (score >= 0.5) {
    return 'var(--status-warning)';
  } else {
    return 'var(--status-failure)';
  }
}

/**
 * Get operation type color for timeline visualization
 */
export function getOperationColor(operationName: string): string {
  if (operationName.includes('invoke_agent')) {
    return 'var(--accent-primary)';
  } else if (operationName.includes('call_llm')) {
    return 'var(--accent-purple)';
  } else if (operationName.includes('execute_tool')) {
    return 'var(--accent-lime)';
  } else if (operationName.includes('http') || operationName.includes('request')) {
    return 'var(--accent-orange)';
  } else {
    return 'var(--text-secondary)';
  }
}

/**
 * Parse JSON safely with error handling
 */
export function safeJsonParse<T>(jsonString: string, fallback: T): T {
  try {
    return JSON.parse(jsonString) as T;
  } catch {
    return fallback;
  }
}

/**
 * Read file as text
 */
export function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      if (e.target?.result) {
        resolve(e.target.result as string);
      } else {
        reject(new Error('Failed to read file'));
      }
    };
    reader.onerror = () => reject(new Error('Failed to read file'));
    reader.readAsText(file);
  });
}

/**
 * Calculate pass rate from results
 */
export function calculatePassRate(results: { evalStatus: EvalStatus }[]): number {
  if (results.length === 0) return 0;
  const passed = results.filter((r) => r.evalStatus === 'PASSED').length;
  return passed / results.length;
}

/**
 * Calculate average score from results
 */
export function calculateAvgScore(results: { score: number | null }[]): number | null {
  const scores = results.map((r) => r.score).filter((s): s is number => s !== null);
  if (scores.length === 0) return null;
  return scores.reduce((sum, score) => sum + score, 0) / scores.length;
}

/**
 * Copy text to clipboard
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function convertSnakeToCamel(obj: any): any {
  if (Array.isArray(obj)) {
    return obj.map(convertSnakeToCamel);
  }

  if (obj !== null && typeof obj === 'object') {
    const converted: any = {};
    for (const [key, value] of Object.entries(obj)) {
      const camelKey = key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
      converted[camelKey] = convertSnakeToCamel(value);
    }
    return converted;
  }

  return obj;
}

export function convertCamelToSnake(obj: any): any {
  if (Array.isArray(obj)) {
    return obj.map(convertCamelToSnake);
  }

  if (obj !== null && typeof obj === 'object') {
    const converted: any = {};
    for (const [key, value] of Object.entries(obj)) {
      const snakeKey = key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
      converted[snakeKey] = convertCamelToSnake(value);
    }
    return converted;
  }

  return obj;
}

/**
 * Get color for extraction type
 */
export function getExtractionTypeColor(type: string): string {
  switch (type) {
    case 'user_input':
      return 'var(--accent-purple)';
    case 'tool_use':
      return 'var(--accent-lime)';
    case 'tool_response':
      return 'var(--accent-orange)';
    case 'final_response':
      return 'var(--accent-primary)';
    default:
      return 'var(--text-secondary)';
  }
}
