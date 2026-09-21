-- ===========================================================================
-- Migration 016 (Phase 4A): orchestration run audit.
--
-- Two tables. Deliberately two, and deliberately not an event platform.
--
-- The questions this has to answer are finite and were written down before
-- the schema was: when did a run start, what window did it cover, which
-- lectures did it see, what did it attempt, what succeeded, what failed, what
-- is retryable, what needs a human, and when did it finish. A generic event
-- log answers all of those badly, because every answer becomes a query over
-- untyped rows; two typed tables answer all of them with a SELECT.
--
-- WHAT IS NOT HERE, ON PURPOSE:
--   * no transcript text, no model output, no prompt, no API key, no learner
--     name and no email. A run row records WHAT HAPPENED, never the content
--     it happened to. `error_message_safe` is named for the rule it carries.
--   * no per-stage row. The stage matrix is DERIVED state, recomputable at any
--     time from the tables that own it, and persisting a copy would create a
--     second source of truth that starts drifting the moment it is written.
--   * no lock table. Locking is PostgreSQL advisory locks (see
--     app/orchestration/locks.py), which are released by the server when a
--     connection dies. A lock row in a table is a lock that outlives the
--     process holding it, which is exactly the failure this must not have.
--
-- Nothing here touches any legacy table, and nothing here is written by a
-- dry run.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. One row per orchestration cycle.
--
-- run_id is a random uuid rather than a deterministic one: two cycles over the
-- same day are two genuinely different events, and collapsing them would
-- destroy the history that makes "is the second cycle idempotent?" answerable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_pipeline_runs (
    run_id uuid PRIMARY KEY,

    -- MANUAL, SCHEDULED, RECONCILE or DRY_RUN. DRY_RUN is a run type rather
    -- than a flag so that "show me what the scheduler would have done" is a
    -- first-class, queryable part of the history.
    run_type text NOT NULL,
    orchestration_version text NOT NULL,

    -- The window. target_date is the primary day; window_start/window_end
    -- carry the lookback, so a cycle that revisited three days says so.
    target_date date NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,

    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL,

    -- The n8n read-only safety precheck, recorded as evidence rather than as
    -- a log line. A production run that cannot show this is a run that cannot
    -- prove the legacy QA node was disabled while it wrote.
    legacy_qa_precheck_status text,
    legacy_qa_node_disabled boolean,

    lectures_seen integer NOT NULL DEFAULT 0,
    completed_count integer NOT NULL DEFAULT 0,
    waiting_count integer NOT NULL DEFAULT 0,
    review_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    skipped_count integer NOT NULL DEFAULT 0,

    -- Cost, as data. A scheduler whose provider call count is not visible is a
    -- scheduler nobody can safely leave running.
    graph_calls integer NOT NULL DEFAULT 0,
    provider_calls integer NOT NULL DEFAULT 0,
    legacy_rows_written integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_pipeline_runs_type CHECK (run_type IN (
        'MANUAL', 'SCHEDULED', 'RECONCILE', 'DRY_RUN')),
    CONSTRAINT lecture_pipeline_runs_status CHECK (status IN (
        'RUNNING', 'COMPLETED', 'COMPLETED_WITH_FAILURES', 'FAILED',
        'BLOCKED_LEGACY_QA_ACTIVE', 'BLOCKED_WRITER_INTEGRITY', 'ABORTED')),
    CONSTRAINT lecture_pipeline_runs_window CHECK (window_end >= window_start),
    CONSTRAINT lecture_pipeline_runs_counts CHECK (
        lectures_seen >= 0 AND completed_count >= 0 AND waiting_count >= 0
        AND review_count >= 0 AND failed_count >= 0 AND skipped_count >= 0
        AND graph_calls >= 0 AND provider_calls >= 0 AND legacy_rows_written >= 0),
    -- A finished run cannot finish before it started.
    CONSTRAINT lecture_pipeline_runs_order CHECK (
        finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS lecture_pipeline_runs_date_idx
    ON public.lecture_pipeline_runs (target_date, started_at DESC);
CREATE INDEX IF NOT EXISTS lecture_pipeline_runs_recent_idx
    ON public.lecture_pipeline_runs (started_at DESC);

COMMENT ON TABLE public.lecture_pipeline_runs IS
    'Phase 4A orchestration cycles. Audit only: owns no pipeline state and is never read to decide what to do next.';
COMMENT ON COLUMN public.lecture_pipeline_runs.legacy_qa_node_disabled IS
    'Result of the read-only n8n precheck: was "Execute QA One Lecture" still disabled when this run started?';


-- ---------------------------------------------------------------------------
-- 2. One row per lecture per run.
--
-- initial_state and final_state are the next_action before and after, not the
-- whole stage matrix: the matrix is recomputable, the DECISION is not. Keeping
-- the decision is what makes "did this cycle change anything?" answerable
-- without re-deriving history that no longer exists.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_pipeline_run_items (
    item_id uuid PRIMARY KEY,
    run_id uuid NOT NULL
        REFERENCES public.lecture_pipeline_runs (run_id) ON DELETE CASCADE,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,

    initial_state text NOT NULL,
    action text NOT NULL,
    final_state text,
    status text NOT NULL,

    -- A stable code an operator and a future UI can branch on, and a message
    -- that is safe to display. The message is scrubbed of anything that could
    -- carry transcript content or a credential before it reaches this column.
    error_code text,
    error_message_safe text,
    retryable boolean NOT NULL DEFAULT false,

    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    duration_ms integer,

    graph_calls integer NOT NULL DEFAULT 0,
    provider_calls integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_pipeline_run_items_status CHECK (status IN (
        'SUCCEEDED', 'SKIPPED', 'WAITING', 'REVIEW_REQUIRED', 'FAILED',
        'BLOCKED', 'LOCKED')),
    CONSTRAINT lecture_pipeline_run_items_counts CHECK (
        graph_calls >= 0 AND provider_calls >= 0
        AND (duration_ms IS NULL OR duration_ms >= 0)),
    -- One decision per lecture per run. A second row for the same lecture in
    -- the same run would mean the cycle processed it twice, which is the thing
    -- the locking exists to prevent.
    CONSTRAINT lecture_pipeline_run_items_key UNIQUE (run_id, lecture_id)
);

CREATE INDEX IF NOT EXISTS lecture_pipeline_run_items_lecture_idx
    ON public.lecture_pipeline_run_items (lecture_id, created_at DESC);
CREATE INDEX IF NOT EXISTS lecture_pipeline_run_items_run_idx
    ON public.lecture_pipeline_run_items (run_id);
CREATE INDEX IF NOT EXISTS lecture_pipeline_run_items_failed_idx
    ON public.lecture_pipeline_run_items (status, created_at DESC)
    WHERE status IN ('FAILED', 'REVIEW_REQUIRED', 'BLOCKED');

COMMENT ON TABLE public.lecture_pipeline_run_items IS
    'Phase 4A per-lecture orchestration outcome. Holds decisions and error codes only - never transcript text, model output or credentials.';
COMMENT ON COLUMN public.lecture_pipeline_run_items.error_message_safe IS
    'Operator-displayable message. Scrubbed: never contains transcript content, learner data or credentials.';

COMMIT;
