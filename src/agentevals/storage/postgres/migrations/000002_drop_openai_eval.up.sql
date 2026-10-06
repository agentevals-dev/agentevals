-- The openai_eval evaluator type has been removed. Purge its historical result
-- rows and narrow the CHECK so the Result model can read every remaining row.
-- run.summary JSON is not rewritten and may still mention removed evaluators.

DELETE FROM {schema}.result WHERE evaluator_type = 'openai_eval';

ALTER TABLE {schema}.result DROP CONSTRAINT IF EXISTS result_evaluator_type_check;
ALTER TABLE {schema}.result ADD CONSTRAINT result_evaluator_type_check
    CHECK (evaluator_type IN ('builtin', 'code', 'remote'));
