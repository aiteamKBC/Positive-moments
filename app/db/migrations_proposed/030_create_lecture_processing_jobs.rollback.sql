-- Rollback for PROPOSED migration 030. Refuses while any job exists: jobs are the operators'
-- audit record of what was queued and are never removed as a side effect.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.lecture_processing_jobs') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.lecture_processing_jobs) THEN
        RAISE EXCEPTION '030 rollback refused: lecture_processing_jobs has rows';
    END IF;
END $$;

DROP TABLE IF EXISTS public.lecture_processing_jobs;

COMMIT;
