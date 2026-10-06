-- WARNING: this restores the wider CHECK constraint only. The openai_eval result
-- rows deleted by the up migration are NOT recoverable.

ALTER TABLE {schema}.result DROP CONSTRAINT IF EXISTS result_evaluator_type_check;
ALTER TABLE {schema}.result ADD CONSTRAINT result_evaluator_type_check
    CHECK (evaluator_type IN ('builtin', 'code', 'remote', 'openai_eval'));
