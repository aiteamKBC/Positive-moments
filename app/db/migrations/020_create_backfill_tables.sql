-- ===========================================================================
-- Migration 020 (QA Core RC2): durable Operations Backfill runs.
--
-- WHY THIS EXISTS
-- ---------------
-- The legacy n8n QA flow was stopped deliberately, so September 2026 contains
-- lectures the coded platform has never processed. Recovering them means
-- asking the normal pipeline "what existed on this business date?" for every
-- date in a range - which is minutes of Graph calls, model calls and writes
-- per day, and therefore cannot live inside an HTTP request.
--
-- These two tables are the ONLY new state. They hold the intent (a date range
-- an operator asked for) and the progress (how far the runner has got). They
-- deliberately hold NO lecture outcomes: every lecture result already lives in
-- `lecture_pipeline_runs` / `lecture_pipeline_run_items`, written by the same
-- orchestrator the nightly scheduler uses. Copying those here would create a
-- second history that could disagree with the first.
--
-- WHAT RESUMABILITY NEEDS
-- -----------------------
-- Exactly one fact: `current_business_date`, the next day still to process.
-- A runner that restarts reads it and continues. Every day is idempotent
-- through existing stage resolution, so re-running the interrupted day is
-- safe and is what we do rather than trying to record partial-day progress.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.backfill_runs (
    backfill_run_id uuid PRIMARY KEY,

    -- The operator's request. Inclusive at both ends, business dates in
    -- Africa/Cairo - the same calendar the whole platform reconciles on.
    requested_from date NOT NULL,
    requested_to   date NOT NULL,

    -- PENDING, RUNNING, COMPLETED, CANCEL_REQUESTED, CANCELLED, FAILED,
    -- BLOCKED_LEGACY_QA_ACTIVE.
    status text NOT NULL DEFAULT 'PENDING',

    -- PREVIEW or EXECUTE.
    --
    -- Preview is durable for the same reason execution is: measured on real
    -- September data, one day costs ~7s of Microsoft Graph plus ~2.6s of stage
    -- resolution, so previewing 01-21 September takes roughly three minutes.
    -- That does not fit in an HTTP request, and a preview that times out
    -- halfway is worse than no preview.
    --
    -- Giving the existing run model a mode is deliberately less machinery than
    -- a second job system: one table, one runner, one status endpoint, one set
    -- of resume and cancel semantics. A PREVIEW run writes nothing beyond its
    -- own progress rows.
    mode text NOT NULL DEFAULT 'EXECUTE',

    -- Whoever pressed Start. The Operations API is authenticated, so this is
    -- a real username rather than a hopeful default.
    created_by text,

    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz,

    -- THE resume checkpoint: the next business date still to process. NULL
    -- once the range is finished.
    current_business_date date,
    -- Purely for the console's "currently working on" line. Never read back
    -- as control state, because a lecture is re-resolved from scratch anyway.
    current_lecture_id uuid,
    current_lecture_label text,

    total_days     integer NOT NULL DEFAULT 0,
    completed_days integer NOT NULL DEFAULT 0,

    -- Roll-ups of what the orchestrator reported, per run. These are a cache
    -- for the console, not the source of truth.
    discovered_count      integer NOT NULL DEFAULT 0,
    matched_count         integer NOT NULL DEFAULT 0,
    processed_count       integer NOT NULL DEFAULT 0,
    already_complete_count integer NOT NULL DEFAULT 0,
    waiting_count         integer NOT NULL DEFAULT 0,
    review_required_count integer NOT NULL DEFAULT 0,
    failed_count          integer NOT NULL DEFAULT 0,
    suppressed_count      integer NOT NULL DEFAULT 0,

    -- Cancellation is cooperative: the runner finishes the lecture it is on,
    -- then stops. Nothing here ever kills a transaction.
    cancel_requested boolean NOT NULL DEFAULT false,
    cancel_requested_at timestamptz,
    cancel_requested_by text,

    error_summary text,
    -- Which build produced this run, so an odd result can be traced to code.
    runner_version text,
    -- Written on every day boundary. A RUNNING row whose heartbeat is hours
    -- old is a crashed runner, and the next runner may safely take it over.
    heartbeat_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT backfill_runs_range_is_ordered
        CHECK (requested_from <= requested_to),
    CONSTRAINT backfill_runs_status_known
        CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'CANCEL_REQUESTED',
                          'CANCELLED', 'FAILED', 'BLOCKED_LEGACY_QA_ACTIVE')),
    CONSTRAINT backfill_runs_mode_known
        CHECK (mode IN ('PREVIEW', 'EXECUTE'))
);

-- Re-runnable: the table above is created only when absent, so an instance
-- that already has migration 020 applied needs the column added here.
ALTER TABLE public.backfill_runs
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'EXECUTE';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.backfill_runs'::regclass
          AND conname  = 'backfill_runs_mode_known'
    ) THEN
        ALTER TABLE public.backfill_runs
            ADD CONSTRAINT backfill_runs_mode_known
            CHECK (mode IN ('PREVIEW', 'EXECUTE'));
    END IF;
END $$;

-- The runner claims the oldest unfinished run; the console lists newest first.
CREATE INDEX IF NOT EXISTS backfill_runs_claimable
    ON public.backfill_runs (status, created_at)
    WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED');

CREATE INDEX IF NOT EXISTS backfill_runs_recent
    ON public.backfill_runs (created_at DESC);

-- One row per business date attempted. This is the per-day audit the console
-- shows; it is a SUMMARY, and the lectures it counts remain in the pipeline
-- run tables where every other part of the platform already reads them.
CREATE TABLE IF NOT EXISTS public.backfill_run_days (
    backfill_run_id uuid NOT NULL
        REFERENCES public.backfill_runs (backfill_run_id) ON DELETE CASCADE,
    business_date date NOT NULL,

    -- PENDING, COMPLETED, FAILED, SKIPPED_CANCELLED, DEFERRED_CYCLE_BUSY.
    status text NOT NULL DEFAULT 'PENDING',

    -- The orchestrator run this day produced, so the console can link a day
    -- to the full pipeline audit instead of duplicating it.
    pipeline_run_id uuid,

    calendar_events_considered integer NOT NULL DEFAULT 0,
    matched_lectures     integer NOT NULL DEFAULT 0,
    newly_discovered     integer NOT NULL DEFAULT 0,
    already_complete     integer NOT NULL DEFAULT 0,
    processed            integer NOT NULL DEFAULT 0,
    waiting              integer NOT NULL DEFAULT 0,
    review_required      integer NOT NULL DEFAULT 0,
    failed               integer NOT NULL DEFAULT 0,
    suppressed           integer NOT NULL DEFAULT 0,

    graph_calls    integer NOT NULL DEFAULT 0,
    provider_calls integer NOT NULL DEFAULT 0,

    error_code text,
    error_message text,
    started_at  timestamptz,
    finished_at timestamptz,
    duration_ms integer,

    PRIMARY KEY (backfill_run_id, business_date),
    CONSTRAINT backfill_run_days_status_known
        CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED',
                          'SKIPPED_CANCELLED', 'DEFERRED_CYCLE_BUSY'))
);

-- A backfill day is a real orchestrator cycle and writes a normal
-- `lecture_pipeline_runs` row. It needs its own run_type so the audit can tell
-- "an operator recovered September" apart from "an operator retried one
-- lecture" - both of which are otherwise MANUAL.
--
-- Widening a CHECK is additive: every value that was legal stays legal, so no
-- existing row can be invalidated by this.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.lecture_pipeline_runs'::regclass
          AND conname  = 'lecture_pipeline_runs_type'
          AND pg_get_constraintdef(oid) NOT LIKE '%BACKFILL%'
    ) THEN
        ALTER TABLE public.lecture_pipeline_runs
            DROP CONSTRAINT lecture_pipeline_runs_type;
        ALTER TABLE public.lecture_pipeline_runs
            ADD CONSTRAINT lecture_pipeline_runs_type CHECK (run_type IN (
                'MANUAL', 'SCHEDULED', 'RECONCILE', 'DRY_RUN', 'BACKFILL'));
    END IF;
END $$;

COMMENT ON TABLE public.backfill_runs IS
    'Operator-requested historical recovery over a business-date range. Holds '
    'intent and progress only; lecture outcomes live in lecture_pipeline_runs.';
COMMENT ON COLUMN public.backfill_runs.current_business_date IS
    'The resume checkpoint: the next business date still to process.';
COMMENT ON TABLE public.backfill_run_days IS
    'Per-day summary for the console. Links to the pipeline run that holds the '
    'authoritative per-lecture audit rather than duplicating it.';

COMMIT;
