import type { RunResult, EvalConfig, TraceResult, MetricMetadata, StandardResponse, ConvertTracesResponse } from '../lib/types';
import { config } from '../config';

const API_BASE_URL = `${config.api.baseUrl}/api`;

async function unwrap<T>(response: Response): Promise<T> {
  const json: StandardResponse<T> = await response.json();
  if (json.error) {
    throw new Error(json.error);
  }
  return json.data;
}

export async function convertTraces(traceFiles: File[], traceFormat?: string): Promise<ConvertTracesResponse> {
  const formData = new FormData();
  traceFiles.forEach(file => formData.append('trace_files', file));
  if (traceFormat) {
    formData.append('trace_format', traceFormat);
  }

  const response = await fetch(`${API_BASE_URL}/convert`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    let errorMessage = `API error: ${response.statusText}`;
    try {
      const errorData = await response.json();
      if (errorData.detail) {
        errorMessage = errorData.detail;
      }
    } catch {
      // Fallback to statusText
    }
    throw new Error(errorMessage);
  }

  return unwrap<ConvertTracesResponse>(response);
}

export async function evaluateTracesAPI(
  traceFiles: File[],
  evalSetFile: File | null,
  evalConfig: EvalConfig
): Promise<RunResult> {
  const formData = new FormData();

  traceFiles.forEach(file => {
    formData.append('trace_files', file);
  });

  if (evalSetFile) {
    formData.append('eval_set_file', evalSetFile);
  }

  formData.append('config', JSON.stringify(evalConfig));

  try {
    const response = await fetch(`${API_BASE_URL}/evaluate`, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      let errorMessage = `API error: ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Fallback to statusText if JSON parsing fails
      }
      throw new Error(errorMessage);
    }

    return unwrap<RunResult>(response);

  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Unknown error occurred while evaluating traces');
  }
}

export async function evaluateTracesStreaming(
  traceFiles: File[],
  evalSetFile: File | null,
  evalConfig: EvalConfig,
  onProgress: (message: string) => void,
  onTraceProgress: (traceId: string, status: string, partialResult?: TraceResult) => void,
  onComplete: (result: RunResult) => void,
  onError: (error: Error) => void
): Promise<void> {
  const formData = new FormData();

  traceFiles.forEach(file => {
    formData.append('trace_files', file);
  });

  if (evalSetFile) {
    formData.append('eval_set_file', evalSetFile);
  }

  formData.append('config', JSON.stringify(evalConfig));

  try {
    const response = await fetch(`${API_BASE_URL}/evaluate/stream`, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let eventType = '';

    while (true) {
      const { done, value } = await reader.read();

      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');

      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const eventData = JSON.parse(line.slice(6));

          if (eventType === 'performance_metrics') {
            const tm = eventData.traceMetadata ?? {};
            const partialResult: TraceResult = {
              traceId: eventData.traceId,
              numInvocations: 0,
              metricResults: [],
              conversionWarnings: [],
              performanceMetrics: eventData.performanceMetrics,
              agentName: tm.agentName,
              sessionId: tm.sessionName,
              model: tm.model,
              startTime: tm.startTime,
              userInputPreview: tm.userInputPreview,
              finalOutputPreview: tm.finalOutputPreview,
            };
            onTraceProgress(eventData.traceId, 'loading', partialResult);
            eventType = '';
          } else if (eventData.error) {
            onError(new Error(eventData.error));
            return;
          } else if (eventData.done) {
            onComplete(eventData.result as RunResult);
            return;
          } else if (eventData.traceProgress) {
            const tp = eventData.traceProgress;
            const partialResult = tp.partialResult as TraceResult | undefined;
            onTraceProgress(tp.traceId, tp.status || '', partialResult);
          } else if (eventData.message) {
            onProgress(eventData.message);
          }
        }
      }
    }
  } catch (error) {
    if (error instanceof Error) {
      onError(error);
    } else {
      onError(new Error('Unknown error occurred during streaming evaluation'));
    }
  }
}

export async function listMetrics(): Promise<MetricMetadata[]> {
  try {
    const response = await fetch(`${API_BASE_URL}/metrics`);

    if (!response.ok) {
      throw new Error(`Failed to fetch metrics: ${response.statusText}`);
    }

    return unwrap<MetricMetadata[]>(response);
  } catch (error) {
    console.error('Failed to list metrics:', error);
    throw error;
  }
}

export async function validateEvalSet(evalSetFile: File): Promise<{ valid: boolean; evalSetId?: string; numCases?: number; errors?: string[] }> {
  const formData = new FormData();
  formData.append('eval_set_file', evalSetFile);

  try {
    const response = await fetch(`${API_BASE_URL}/validate/eval-set`, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`Failed to validate eval set: ${response.statusText}`);
    }

    return unwrap<{ valid: boolean; evalSetId?: string; numCases?: number; errors?: string[] }>(response);
  } catch (error) {
    console.error('Failed to validate eval set:', error);
    throw error;
  }
}

export async function getConfig(): Promise<{ apiKeys: { google: boolean; anthropic: boolean; openai: boolean } }> {
  const response = await fetch(`${API_BASE_URL}/config`);
  if (!response.ok) {
    throw new Error(`Failed to fetch config: ${response.statusText}`);
  }
  return unwrap(response);
}

export async function generateBugReport(diagnostics: {
  user_description: string;
  browser_info: Record<string, unknown>;
  console_logs: unknown[];
  app_state: Record<string, unknown>;
  network_errors: unknown[];
}): Promise<Blob> {
  const response = await fetch(config.api.endpoints.debugBundle, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(diagnostics),
  });

  if (!response.ok) {
    let errorMessage = `Failed to generate bug report: ${response.statusText}`;
    try {
      const errorData = await response.json();
      if (errorData.detail) {
        errorMessage = errorData.detail;
      }
    } catch {
      // Fallback to statusText
    }
    throw new Error(errorMessage);
  }

  return response.blob();
}

export async function loadBugReport(
  file: File,
): Promise<{ loadedSessions: string[]; count: number }> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(config.api.endpoints.debugLoad, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    let errorMessage = `Failed to load bug report: ${response.statusText}`;
    try {
      const errorData = await response.json();
      if (errorData.detail) {
        errorMessage = errorData.detail;
      }
    } catch {
      // Fallback to statusText
    }
    throw new Error(errorMessage);
  }

  return unwrap<{ loadedSessions: string[]; count: number }>(response);
}

export async function healthCheck(): Promise<{ status: string; version: string }> {
  try {
    const response = await fetch(`${API_BASE_URL}/health`);

    if (!response.ok) {
      throw new Error(`Health check failed: ${response.statusText}`);
    }

    return unwrap<{ status: string; version: string }>(response);
  } catch (error) {
    console.error('Health check failed:', error);
    throw error;
  }
}
