const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ??
    (import.meta.env.DEV ? 'http://localhost:8001' : '');

export const config = {
  api: {
    baseUrl: API_BASE_URL,
    endpoints: {
      health: `${API_BASE_URL}/api/health`,
      metrics: `${API_BASE_URL}/api/metrics`,
      evaluate: `${API_BASE_URL}/api/evaluate`,
      evaluateStream: `${API_BASE_URL}/api/evaluate/stream`,
      runs: `${API_BASE_URL}/api/runs`,
      validateEvalSet: `${API_BASE_URL}/api/validate/eval-set`,
      streamingCreateEvalSet: `${API_BASE_URL}/api/streaming/create-eval-set`,
      streamingGetTrace: `${API_BASE_URL}/api/streaming/get-trace`,
      streamingSessions: `${API_BASE_URL}/api/streaming/sessions`,
      uiUpdatesStream: `${API_BASE_URL}/stream/ui-updates`,
      debugBundle: `${API_BASE_URL}/api/debug/bundle`,
      debugLoad: `${API_BASE_URL}/api/debug/load`,
    },
  },
} as const;
