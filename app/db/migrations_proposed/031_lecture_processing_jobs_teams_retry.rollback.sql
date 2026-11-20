-- Rollback for PROPOSED migration 031. Refuses while any Teams transcript retry job exists: jobs
-- are the operators' audit record of what was queued and are never removed as a side effect.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.lecture_processing_jobs WHERE reason = 'TEAMS_TRANSCRIPT_RETRY') THEN
        RAISE EXCEPTION '031 rollback refused: TEAMS_TRANSCRIPT_RETRY jobs exist';
    END IF;
END $$;

ALTER TABLE public.lecture_processing_jobs
    DROP CONSTRAINT IF EXISTS lecture_processing_jobs_reason_known;
ALTER TABLE public.lecture_processing_jobs
    ADD CONSTRAINT lecture_processing_jobs_reason_known
        CHECK (reason IN ('MANUAL_TRANSCRIPT_UPLOAD'));

COMMIT;
