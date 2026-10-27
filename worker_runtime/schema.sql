CREATE TABLE IF NOT EXISTS worker_schema (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_processes (
    worker_id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    pid integer NOT NULL,
    hostname text NOT NULL CHECK (length(hostname) <= 255),
    concurrency integer NOT NULL CHECK (concurrency BETWEEN 1 AND 16),
    state text NOT NULL CHECK (state IN ('active', 'draining', 'stopped')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type text NOT NULL CHECK (job_type IN ('image_metadata', 'csv_transform', 'synthetic_report', 'notification_simulation')),
    payload jsonb NOT NULL CHECK (pg_column_size(payload) <= 524288),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'succeeded', 'dead', 'canceled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    max_runtime_seconds integer NOT NULL DEFAULT 30 CHECK (max_runtime_seconds BETWEEN 1 AND 300),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    priority integer NOT NULL DEFAULT 0 CHECK (priority BETWEEN -10 AND 10),
    worker_id uuid REFERENCES worker_processes(worker_id),
    lease_token uuid,
    lease_until timestamptz,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    idempotency_key text CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 128),
    request_fingerprint text CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$'),
    submitted_by text CHECK (submitted_by IS NULL OR length(submitted_by) BETWEEN 1 AND 64),
    cancel_requested_at timestamptz,
    replay_of bigint REFERENCES worker_jobs(id),
    result jsonb CHECK (result IS NULL OR pg_column_size(result) <= 524288),
    last_error_code text CHECK (last_error_code IS NULL OR length(last_error_code) <= 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((state = 'running') = (worker_id IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK (state = 'succeeded' OR result IS NULL)
);

-- Idempotent additive migration for databases created by worker Phase 3.
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS max_runtime_seconds integer NOT NULL DEFAULT 30;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS replay_of bigint REFERENCES worker_jobs(id);
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS request_fingerprint text;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS submitted_by text;
ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz;
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_state_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_state_check
    CHECK (state IN ('queued', 'running', 'succeeded', 'dead', 'canceled'));
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_priority_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_priority_check CHECK (priority BETWEEN -10 AND 10);
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_request_fingerprint_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_request_fingerprint_check
    CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_submitted_by_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_submitted_by_check
    CHECK (submitted_by IS NULL OR length(submitted_by) BETWEEN 1 AND 64);
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_max_runtime_seconds_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_max_runtime_seconds_check
    CHECK (max_runtime_seconds BETWEEN 1 AND 300);
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_generation_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_generation_check CHECK (generation >= 0);
ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS worker_jobs_idempotency_key_check;
ALTER TABLE worker_jobs ADD CONSTRAINT worker_jobs_idempotency_key_check
    CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 128);
CREATE UNIQUE INDEX IF NOT EXISTS worker_jobs_idempotency_key
    ON worker_jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;

DROP INDEX IF EXISTS worker_jobs_queued_order;
CREATE INDEX IF NOT EXISTS worker_jobs_queued_priority_order
    ON worker_jobs (priority DESC, available_at, created_at, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS worker_jobs_expired_lease
    ON worker_jobs (lease_until, created_at, id) WHERE state = 'running';

CREATE TABLE IF NOT EXISTS worker_job_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES worker_jobs(id) ON DELETE CASCADE,
    event_type text NOT NULL CHECK (event_type IN (
        'enqueued', 'claimed', 'lease_reclaimed', 'succeeded', 'retry_scheduled',
        'timed_out', 'permanent_failure', 'abandoned', 'lease_lost'
    )),
    attempt integer NOT NULL CHECK (attempt >= 0),
    worker_id uuid REFERENCES worker_processes(worker_id),
    lease_token uuid,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_column_size(details) <= 4096)
);
ALTER TABLE worker_job_events ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE worker_job_events DROP CONSTRAINT IF EXISTS worker_job_events_generation_check;
ALTER TABLE worker_job_events ADD CONSTRAINT worker_job_events_generation_check CHECK (generation >= 0);
ALTER TABLE worker_job_events DROP CONSTRAINT IF EXISTS worker_job_events_event_type_check;
ALTER TABLE worker_job_events ADD CONSTRAINT worker_job_events_event_type_check CHECK (event_type IN (
    'enqueued', 'claimed', 'lease_reclaimed', 'succeeded', 'retry_scheduled',
    'timed_out', 'permanent_failure', 'abandoned', 'lease_lost',
    'submission_deduplicated', 'operator_retry', 'replay_created',
        'effect_applied', 'effect_deduplicated', 'cancel_requested', 'canceled', 'scheduled', 'started'
));

-- Separate API/scheduler control plane state. Occurrence identity is a UTC instant.
CREATE TABLE IF NOT EXISTS worker_schedules (
    schedule_id uuid PRIMARY KEY,
    job_type text NOT NULL CHECK (job_type IN ('image_metadata', 'csv_transform', 'synthetic_report', 'notification_simulation')),
    payload jsonb NOT NULL CHECK (pg_column_size(payload) <= 524288),
    cron_expression text NOT NULL CHECK (length(cron_expression) BETWEEN 9 AND 128),
    timezone text NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    priority integer NOT NULL DEFAULT 0 CHECK (priority BETWEEN -10 AND 10),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    max_runtime_seconds integer NOT NULL DEFAULT 30 CHECK (max_runtime_seconds BETWEEN 1 AND 300),
    enabled boolean NOT NULL DEFAULT true,
    next_occurrence_at timestamptz NOT NULL,
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS worker_schedules_due
    ON worker_schedules (next_occurrence_at, schedule_id) WHERE enabled;

CREATE TABLE IF NOT EXISTS worker_schedule_occurrences (
    schedule_id uuid NOT NULL REFERENCES worker_schedules(schedule_id),
    occurrence_at timestamptz NOT NULL,
    job_id bigint NOT NULL UNIQUE REFERENCES worker_jobs(id),
    materialized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (schedule_id, occurrence_at)
);

CREATE TABLE IF NOT EXISTS worker_schedule_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schedule_id uuid NOT NULL REFERENCES worker_schedules(schedule_id),
    event_type text NOT NULL CHECK (event_type IN ('created','disabled','materialized')),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 64),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_column_size(details) <= 4096)
);
CREATE INDEX IF NOT EXISTS worker_schedule_events_order
    ON worker_schedule_events (schedule_id,id);

-- Authenticated request rate is shared by API replicas and bounded to one row/key.
CREATE TABLE IF NOT EXISTS worker_api_rate_limits (
    principal text PRIMARY KEY CHECK (length(principal) BETWEEN 1 AND 64),
    window_start timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count >= 0)
);
CREATE INDEX IF NOT EXISTS worker_job_events_job_order
    ON worker_job_events (job_id, id);
CREATE INDEX IF NOT EXISTS worker_job_events_time_type
    ON worker_job_events (occurred_at,event_type);
CREATE INDEX IF NOT EXISTS worker_jobs_current_worker
    ON worker_jobs (worker_id) WHERE state='running';

-- Idempotency ledger for the simulated notification effect, kept in the same DB.
CREATE TABLE IF NOT EXISTS worker_demo_effects (
    job_id bigint NOT NULL REFERENCES worker_jobs(id) ON DELETE CASCADE,
    effect_type text NOT NULL CHECK (effect_type = 'notification_simulation'),
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 160),
    result jsonb NOT NULL CHECK (pg_column_size(result) <= 4096),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_id,effect_type)
);

INSERT INTO worker_schema (version) VALUES (1),(2),(3) ON CONFLICT (version) DO NOTHING;

-- Phase 6 safe, shared defaults used by submissions when callers omit overrides.
CREATE TABLE IF NOT EXISTS worker_queue_policy (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    default_max_attempts integer NOT NULL CHECK (default_max_attempts BETWEEN 1 AND 10),
    default_max_runtime_seconds integer NOT NULL CHECK (default_max_runtime_seconds BETWEEN 1 AND 300),
    updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 64),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO worker_queue_policy(singleton,default_max_attempts,default_max_runtime_seconds,updated_by)
VALUES (true,5,30,'system') ON CONFLICT (singleton) DO NOTHING;
INSERT INTO worker_schema (version) VALUES (4),(5) ON CONFLICT (version) DO NOTHING;
INSERT INTO worker_schema (version) VALUES (6) ON CONFLICT (version) DO NOTHING;
