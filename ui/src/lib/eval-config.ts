import type { EvalConfig } from './types';

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
      threshold,
      trajectoryMatchType,
    })),
  };
}
