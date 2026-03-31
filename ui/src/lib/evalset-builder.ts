import type { Invocation, EvalSet, EvalCase } from './types';
import { convertCamelToSnake } from './utils';

/**
 * Generate an EvalSet from pre-converted invocations (backend is source of truth).
 */
export function generateEvalSet(
  invocationsByTrace: Array<{ traceId: string; invocations: Invocation[] }>,
  baseFilename: string
): EvalSet {
  const timestamp = new Date().toISOString().split('T')[0];
  const cleanFilename = baseFilename.replace(/\.json$/i, '').replace(/[^a-z0-9_]/gi, '_');
  const evalSetId = `evalset_${cleanFilename}_${timestamp}`;

  const evalCases: EvalCase[] = [];

  for (const { traceId, invocations } of invocationsByTrace) {
    if (invocations.length === 0) continue;

    invocations.forEach((invocation, idx) => {
      evalCases.push({
        eval_id: `${traceId.substring(0, 8)}_case_${idx + 1}`,
        conversation: [invocation],
      });
    });
  }

  return {
    eval_set_id: evalSetId,
    name: `Eval Set for ${baseFilename}`,
    description: `Generated from ${invocationsByTrace.length} trace(s) on ${new Date().toLocaleString()}`,
    eval_cases: evalCases,
  };
}

/**
 * Validate an EvalSet structure
 *
 * @param evalSet - The eval set to validate
 * @returns Array of error messages (empty if valid)
 */
export function validateEvalSet(evalSet: EvalSet): string[] {
  const errors: string[] = [];

  if (!evalSet.eval_set_id || evalSet.eval_set_id.trim() === '') {
    errors.push('eval_set_id is required');
  }

  if (!evalSet.name || evalSet.name.trim() === '') {
    errors.push('name is required');
  }

  if (!evalSet.eval_cases || evalSet.eval_cases.length === 0) {
    errors.push('At least one eval case is required');
  }

  evalSet.eval_cases?.forEach((ec, idx) => {
    if (!ec.eval_id || ec.eval_id.trim() === '') {
      errors.push(`Eval case ${idx + 1}: eval_id is required`);
    }
    if (!ec.conversation || ec.conversation.length === 0) {
      errors.push(`Eval case ${idx + 1}: conversation must have at least one invocation`);
    }
  });

  return errors;
}

/**
 * Download an EvalSet as a JSON file
 *
 * @param evalSet - The eval set to download
 */
export function downloadEvalSet(evalSet: EvalSet): void {
  // Convert to snake_case for Python backend compatibility
  const snakeCaseEvalSet = convertCamelToSnake(evalSet);

  const jsonString = JSON.stringify(snakeCaseEvalSet, null, 2);
  const blob = new Blob([jsonString], { type: 'application/json' });
  const url = URL.createObjectURL(blob);

  const link = document.createElement('a');
  link.href = url;
  link.download = `${evalSet.eval_set_id}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

/**
 * Copy text to clipboard
 *
 * @param text - Text to copy
 * @returns Promise that resolves to true if successful
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    console.error('Failed to copy to clipboard:', error);
    return false;
  }
}
