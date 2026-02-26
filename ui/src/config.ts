/**
 * Application configuration.
 *
 * In production, these values can be overridden using environment variables.
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8001';

export const config = {
  api: {
    baseUrl: API_BASE_URL,
    endpoints: {
      health: `${API_BASE_URL}/api/health`,
      metrics: `${API_BASE_URL}/api/metrics`,
      evaluate: `${API_BASE_URL}/api/evaluate`,
      evaluateStream: `${API_BASE_URL}/api/evaluate/stream`,
      validateEvalSet: `${API_BASE_URL}/api/validate/eval-set`,
      streamingCreateEvalSet: `${API_BASE_URL}/api/streaming/create-eval-set`,
      streamingGetTrace: `${API_BASE_URL}/api/streaming/get-trace`,
      streamingSessions: `${API_BASE_URL}/api/streaming/sessions`,
      uiUpdatesStream: `${API_BASE_URL}/stream/ui-updates`,
    },
  },
  websocket: {
    tracesUrl: import.meta.env.VITE_WS_URL || 'ws://localhost:8001/ws/traces',
  },
} as const;
