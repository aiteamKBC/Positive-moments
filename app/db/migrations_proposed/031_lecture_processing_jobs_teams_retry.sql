-- ===========================================================================
-- PROPOSED migration 031: a second reason for a lecture processing job - TEAMS_TRANSCRIPT_RETRY.
--
-- Requires 030. Rollback: 031_lecture_processing_jobs_teams_retry.rollback.sql.
-- Widens one CHECK constraint; no column, index or row is changed.
--
-- An operator asks, from the lecture's page, for its transcript to be fetched from Microsoft Teams
-- again (app/orchestration/teams_transcript_retry.py). The job is worked by the same background
-- worker as a manual transcript upload, under the same guarantees: one active job per lecture
-- (whatever its reason), claims with a lease, bounded attempts.
--
-- Until this is applied the console reports the retry as not installed and queues nothing.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_processing_jobs
    DROP CONSTRAINT IF EXISTS lecture_processing_jobs_reason_known;
ALTER TABLE public.lecture_processing_jobs
    ADD CONSTRAINT lecture_processing_jobs_reason_known
        CHECK (reason IN ('MANUAL_TRANSCRIPT_UPLOAD', 'TEAMS_TRANSCRIPT_RETRY'));

COMMIT;
