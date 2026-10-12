const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ??
    (import.meta.env.DEV ? 'http://localhost:8001' : '');

export const config = {
  api: {
    baseUrl: API_BASE_URL,
    endpoints: {
      runs: `${API_BASE_URL}/api/runs`,
      streamingCreateEvalSet: `${API_BASE_URL}/api/streaming/create-eval-set`,
      streamingGetTrace: `${API_BASE_URL}/api/streaming/get-trace`,
      uiUpdatesStream: `${API_BASE_URL}/stream/ui-updates`,
      debugBundle: `${API_BASE_URL}/api/debug/bundle`,
      debugLoad: `${API_BASE_URL}/api/debug/load`,
    },
  },
} as const;
