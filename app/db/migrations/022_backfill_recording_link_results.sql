-- ===========================================================================
-- Migration 022: Recording Links outcomes for Operations Backfill runs.
--
-- WHY THIS EXISTS
-- ---------------
-- The Historical Backfill page must explain recording coverage for a range:
-- per lecture, what the RECORDING_LINK stage found or did, and totals that
-- reconcile with that table.
--
-- An EXECUTE run's lecture outcomes already live in `lecture_recording_links`
-- (current state) and `lecture_pipeline_run_items` (history). A PREVIEW run's
-- do not live anywhere: a preview writes nothing to the platform, by design,
-- and its live Graph verdicts would otherwise vanish when the worker moves to
-- the next day. So each run keeps a per-lecture SNAPSHOT of its recording
-- results, written by the backfill runner beside its existing day rows - run
-- progress, exactly like `backfill_run_days`, never platform state.
--
-- One row per (run, business date, lecture or legacy row). A resumed day
-- replaces its rows, so the snapshot always describes the attempt that counted.
--
-- WHAT IT DOES NOT HOLD
-- ---------------------
-- No recording URL, no DriveItem or drive id, no file name, no token, no
-- transcript text, no learner data. `source` is a folder NAME
-- ("channel_recordings", "onedrive_recordings"), never a location.
--
-- Additive and re-runnable.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.backfill_run_recording_items (
    backfill_run_id uuid NOT NULL
        REFERENCES public.backfill_runs (backfill_run_id) ON DELETE CASCADE,
    business_date date NOT NULL,
    -- The lecture id, or 'legacy:<hash>' for a legacy QA row no coded lecture owns.
    item_key text NOT NULL,
    lecture_id uuid,
    subject text,
    -- CODED_LECTURE or LEGACY_ROW_ONLY.
    population text NOT NULL,
    -- RECORDING_LINK_MODE of the runner that produced the row.
    recording_link_mode text NOT NULL,
    -- LIVE_PREVIEW (read-only live Graph evaluation) or EXECUTE (what the
    -- shared orchestrator did).
    evaluation text NOT NULL,
    -- The operator-facing outcome; app/recordings/coverage.py owns the list.
    outcome text NOT NULL,
    -- The stage's own vocabulary, kept verbatim beside the outcome.
    recording_stage_state text,
    recording_status text,
    reason text,
    earlier_stage text,
    earlier_action text,
    would_write boolean NOT NULL DEFAULT false,
    written boolean NOT NULL DEFAULT false,
    perfect_row_updated boolean NOT NULL DEFAULT false,
    perfect_row_would_update boolean NOT NULL DEFAULT false,
    source text,
    timestamp_difference_seconds numeric(10, 3),
    candidate_file_count integer,
    exact_candidate_count integer,
    graph_lookup_status text,
    graph_http_status integer,
    verification text,
    legacy_cancelled boolean NOT NULL DEFAULT false,
    attempt_count integer,
    next_attempt_after timestamptz,
    last_attempted_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (backfill_run_id, business_date, item_key),
    CONSTRAINT backfill_run_recording_items_population_known
        CHECK (population IN ('CODED_LECTURE', 'LEGACY_ROW_ONLY')),
    CONSTRAINT backfill_run_recording_items_evaluation_known
        CHECK (evaluation IN ('LIVE_PREVIEW', 'EXECUTE'))
);

-- A day's recording evaluation can fail while its pipeline day succeeds (for
-- example Graph refusing every read). That is recorded, never hidden.
ALTER TABLE public.backfill_run_days
    ADD COLUMN IF NOT EXISTS recording_error_code text;

COMMENT ON TABLE public.backfill_run_recording_items IS
    'Per-run Recording Links snapshot for the Historical Backfill console: live '
    'preview verdicts or execute outcomes. No URLs, ids, file names or secrets.';

COMMIT;
