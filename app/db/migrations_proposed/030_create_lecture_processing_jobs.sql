-- ===========================================================================
-- PROPOSED migration 030: durable lecture processing jobs (manual transcript recovery).
--
-- Rollback: 030_create_lecture_processing_jobs.rollback.sql (refuses while jobs exist).
-- Requires only public.lecture_sessions. Purely additive: one table; nothing existing is altered.
--
-- WHY A TABLE, AND WHY THIS ONE
-- -----------------------------
-- A lecture whose transcript was uploaded by hand must finish its pipeline - the paid quality
-- evaluation included - without an operator, a browser or a CLI. What already exists:
--
--   * the nightly scheduler (positive-moments-scheduler) - processes lectures of the last
--     SCHEDULER_LOOKBACK_DAYS (3) days, whole days at a time; an older lecture is never reached,
--     and it takes no per-lecture request;
--   * Operations Backfill (public.backfill_runs, the positive-moments-backfill service) - durable
--     and claimable, but its unit of work is a business-date RANGE: each day is a discovering
--     run_window over every lecture of that day, including the day-wide Graph transcript sweep;
--     its rows, counts and console are day-shaped.
--
-- Neither has a "this one lecture" unit. This table is that unit and nothing more: the INTENT
-- (process lecture X) and its PROGRESS (queued / running / completed / failed, attempts, lease).
-- It is consumed by the EXISTING positive-moments-backfill service between backfill days; the
-- work itself is the existing lecture-scoped PipelineOrchestrator.run_window
-- (app/orchestration/transcript_recovery.py). Lecture outcomes stay where they always are
-- (lecture_pipeline_runs and the stage tables); `outcome` keeps codes and counts only.
--
-- DUPLICATE SAFETY
--   * at most ONE active (QUEUED or RUNNING) job per lecture: a partial unique index, so two
--     clicks or two requests cannot both enqueue;
--   * a claim is FOR UPDATE SKIP LOCKED with a lease; an expired lease (a dead worker) is
--     reclaimable, and every claim counts an attempt against max_attempts;
--   * inside the work, the scheduler cycle lock and the per-lecture advisory lock, and stage
--     resolution: a COMPLETE QA evaluation is never run again.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_processing_jobs (
    job_id              uuid PRIMARY KEY,
    lecture_id          uuid NOT NULL REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    -- Why the lecture was queued. One reason today: MANUAL_TRANSCRIPT_UPLOAD.
    reason              text NOT NULL,
    -- QUEUED, RUNNING, COMPLETED, FAILED.
    status              text NOT NULL DEFAULT 'QUEUED',
    requested_by        text,

    attempt_count       integer NOT NULL DEFAULT 0,
    max_attempts        integer NOT NULL DEFAULT 3,
    -- A QUEUED job is not claimed before this instant (retry back-off, a busy cycle).
    next_attempt_after  timestamptz NOT NULL DEFAULT now(),

    -- The worker holding a RUNNING job, and until when. An expired lease is a dead worker.
    claimed_by          text,
    lease_expires_at    timestamptz,
    heartbeat_at        timestamptz,

    -- The pipeline audit row the last attempt wrote (lecture_pipeline_runs.run_id).
    pipeline_run_id     uuid,
    last_error_code     text,
    last_error_message  text,
    -- Codes and counts only (final action, provider calls ...). Never transcript text.
    outcome             jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_processing_jobs_status_known
        CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT lecture_processing_jobs_reason_known
        CHECK (reason IN ('MANUAL_TRANSCRIPT_UPLOAD')),
    CONSTRAINT lecture_processing_jobs_attempts
        CHECK (attempt_count >= 0 AND max_attempts >= 1),
    CONSTRAINT lecture_processing_jobs_outcome_is_object
        CHECK (jsonb_typeof(outcome) = 'object')
);

-- One active job per lecture. Enqueue is INSERT ... ON CONFLICT DO NOTHING against this index.
CREATE UNIQUE INDEX IF NOT EXISTS lecture_processing_jobs_one_active
    ON public.lecture_processing_jobs (lecture_id)
    WHERE status IN ('QUEUED', 'RUNNING');

-- The claim: due QUEUED jobs, oldest first; and RUNNING jobs whose lease expired.
CREATE INDEX IF NOT EXISTS lecture_processing_jobs_claimable
    ON public.lecture_processing_jobs (status, next_attempt_after, created_at)
    WHERE status IN ('QUEUED', 'RUNNING');

-- The console: a lecture's latest job, and the recent jobs.
CREATE INDEX IF NOT EXISTS lecture_processing_jobs_by_lecture
    ON public.lecture_processing_jobs (lecture_id, created_at DESC);
CREATE INDEX IF NOT EXISTS lecture_processing_jobs_recent
    ON public.lecture_processing_jobs (created_at DESC);

COMMENT ON TABLE public.lecture_processing_jobs IS
    'Durable per-lecture processing requests (manual transcript recovery), consumed by the '
    'positive-moments-backfill service. Intent and progress only; outcomes live in '
    'lecture_pipeline_runs.';

COMMIT;
