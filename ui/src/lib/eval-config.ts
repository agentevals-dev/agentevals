import type { EvalConfig } from './types';

// Metrics that report a value to track and never pass or fail; the server rejects a threshold on them.
const INFORMATIONAL_METRICS = new Set([
  'tool_call_count_v1',
  'inference_call_count_v1',
  'token_usage_v1',
  'invocation_duration_v1',
]);

interface BuildEvalConfigParams {
  selectedEvaluatorNames: string[];
  judgeModel: string;
  threshold: number;
  trajectoryMatchType: string;
}

export function buildEvalConfig({
  selectedEvaluatorNames,
  judgeModel,
  threshold,
  trajectoryMatchType,
}: BuildEvalConfigParams): EvalConfig {
  return {
    evaluators: selectedEvaluatorNames.map((evaluatorName) => ({
      type: 'builtin',
      name: evaluatorName,
      judgeModel,
      threshold: INFORMATIONAL_METRICS.has(evaluatorName) ? undefined : threshold,
      trajectoryMatchType,
    })),
  };
}
