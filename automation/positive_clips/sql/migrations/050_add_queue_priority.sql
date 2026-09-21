-- ===========================================================================
-- Migration 050: queue priority for qa_media_jobs
--
-- Minimal and additive. No table rebuild, no data rewrite, no constraint
-- removal. Existing rows keep priority 10 (historical default).
--
--   priority 100 = new / live lecture
--   priority  10 = historical backfill
--
-- The existing partial index (status, not_before, created_at) is REPLACED by a
-- superset that also orders by priority, so the claim query stays index-served
-- and no redundant index is left behind. The new index leads with the same
-- (status, not_before) prefix, so every query the old index served is still
-- served.
-- ===========================================================================

BEGIN;

ALTER TABLE public.qa_media_jobs
    ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 10;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.qa_media_jobs'::regclass
          AND conname  = 'qa_media_jobs_priority_check'
    ) THEN
        ALTER TABLE public.qa_media_jobs
            ADD CONSTRAINT qa_media_jobs_priority_check
            CHECK (priority BETWEEN 0 AND 1000);
    END IF;
END $$;

-- Claim ordering: status, not_before, priority DESC, created_at ASC, job_id ASC
CREATE INDEX IF NOT EXISTS qa_media_jobs_claim_idx
    ON public.qa_media_jobs (status, not_before, priority DESC, created_at, job_id)
    WHERE status IN ('pending', 'failed');

DROP INDEX IF EXISTS public.qa_media_jobs_pending_idx;

COMMENT ON COLUMN public.qa_media_jobs.priority IS
    'Queue priority. 100 = new/live lecture, 10 = historical backfill. Higher is claimed first.';

COMMIT;
