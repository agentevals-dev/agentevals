-- agentevals baseline schema. Immutable once tagged in a release.
-- Schema changes go in a NEW migration file (000002_*.up.sql, etc.).
-- The {schema} placeholder is substituted by the Python migrator at apply time.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.session (
    session_id   TEXT        PRIMARY KEY,
    trace_id     TEXT        NOT NULL,
    trace_ids    TEXT[]      NOT NULL DEFAULT '{{}}',
    eval_set_id  TEXT,
    source       TEXT        NOT NULL CHECK (source IN ('websocket', 'otlp', 'api')),
    is_complete  BOOLEAN     NOT NULL DEFAULT FALSE,
    has_root_span BOOLEAN    NOT NULL DEFAULT FALSE,
    metadata     JSONB       NOT NULL DEFAULT '{{}}',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS session_expires_at_idx
    ON {schema}.session (expires_at)
    WHERE expires_at IS NOT NULL;

-- Reserved for future per-span / per-log persistence. Spans and logs stay
-- in-process on StreamingTraceManager in this OSS slice; this table exists
-- so a future migration can populate it without an ALTER on session.
CREATE TABLE IF NOT EXISTS {schema}.session_event (
    event_id    BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  TEXT        NOT NULL REFERENCES {schema}.session(session_id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('span', 'log')),
    payload     JSONB       NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS session_event_session_id_idx
    ON {schema}.session_event (session_id, event_id);

-- Run state and work queue. claim_next() relies on the run_queue_idx for
-- SELECT FOR UPDATE SKIP LOCKED ordering.
CREATE TABLE IF NOT EXISTS {schema}.run (
    run_id            UUID        PRIMARY KEY,
    status            TEXT        NOT NULL CHECK (status IN
                                  ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    approach          TEXT        NOT NULL CHECK (approach IN ('trace_replay', 'agent_invoke')),
    spec              JSONB       NOT NULL,
    attempt           INT         NOT NULL DEFAULT 0,
    worker_id         TEXT,
    claimed_at        TIMESTAMPTZ,
    lease_expires_at  TIMESTAMPTZ,
    cancel_requested  BOOLEAN     NOT NULL DEFAULT FALSE,
    error             TEXT,
    summary           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    CONSTRAINT run_running_has_worker
        CHECK (status <> 'running'
               OR (worker_id IS NOT NULL
                   AND claimed_at IS NOT NULL
                   AND lease_expires_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS run_queue_idx
    ON {schema}.run (status, created_at)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS run_lease_idx
    ON {schema}.run (lease_expires_at)
    WHERE status = 'running';

CREATE TABLE IF NOT EXISTS {schema}.result (
    result_id              TEXT               PRIMARY KEY,
    run_id                 UUID               NOT NULL REFERENCES {schema}.run(run_id) ON DELETE CASCADE,
    eval_set_item_id       TEXT               NOT NULL,
    eval_set_item_name     TEXT               NOT NULL,
    evaluator_name         TEXT               NOT NULL,
    evaluator_type         TEXT               NOT NULL CHECK (evaluator_type IN
                                              ('builtin', 'code', 'remote', 'openai_eval')),
    status                 TEXT               NOT NULL CHECK (status IN
                                              ('passed', 'failed', 'errored', 'skipped')),
    score                  DOUBLE PRECISION,
    per_invocation_scores  DOUBLE PRECISION[] NOT NULL DEFAULT '{{}}',
    trace_id               TEXT,
    span_id                TEXT,
    details                JSONB              NOT NULL DEFAULT '{{}}',
    error_text             TEXT,
    tokens_used            JSONB,
    latency_ms             INT,
    created_at             TIMESTAMPTZ        NOT NULL DEFAULT now(),
    expires_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS result_run_id_idx     ON {schema}.result (run_id);
CREATE INDEX IF NOT EXISTS result_expires_at_idx ON {schema}.result (expires_at) WHERE expires_at IS NOT NULL;

-- Reserved for cached evaluator code from external sources (GitHub today,
-- additional sources later). No read/write code in this slice; included here
-- so a future change does not require an ALTER on this table.
CREATE TABLE IF NOT EXISTS {schema}.evaluator_cache (
    source_name     TEXT        NOT NULL,
    evaluator_name  TEXT        NOT NULL,
    ref             TEXT        NOT NULL,
    content         BYTEA       NOT NULL,
    metadata        JSONB       NOT NULL DEFAULT '{{}}',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_name, evaluator_name, ref)
);
