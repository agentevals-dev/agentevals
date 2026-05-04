-- WARNING: dropping the schema deletes ALL agentevals data: sessions, runs,
-- results, and the evaluator cache. This file is invoked only by
-- ``agentevals migrate down --steps N`` and is not safe to run in production.

DROP SCHEMA IF EXISTS {schema} CASCADE;
