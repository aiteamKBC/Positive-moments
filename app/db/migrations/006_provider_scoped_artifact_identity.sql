-- ===========================================================================
-- Migration 006 (Phase 2B): re-key raw transcript artifacts to PROVIDER
--                           identity.
--
-- Run ONLY by app/cli/normalize_transcript_artifacts.py, and only after that
-- script has, inside the same transaction:
--   1. backfilled public.lecture_transcript_candidates from the existing
--      artifact rows, and
--   2. remapped artifact_id from the lecture-scoped UUIDv5 to the
--      provider-scoped UUIDv5 across artifacts, contents, and candidates.
--
-- After this migration the physical raw artifact identity is
-- (provider, provider_transcript_id) and raw content is stored exactly once per
-- provider artifact. The lecture relationship lives entirely in
-- lecture_transcript_candidates, so nothing is lost by dropping lecture_id here.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_transcript_artifacts
    DROP CONSTRAINT IF EXISTS lecture_transcript_artifacts_provider_key;

ALTER TABLE public.lecture_transcript_artifacts
    DROP COLUMN IF EXISTS lecture_id;

ALTER TABLE public.lecture_transcript_artifacts
    ADD CONSTRAINT lecture_transcript_artifacts_provider_key
        UNIQUE (provider, provider_transcript_id);

COMMENT ON TABLE public.lecture_transcript_artifacts IS
    'One row per unique RAW Microsoft Graph transcript artifact, keyed by provider identity. Raw content is stored once per provider artifact; which lectures can see it lives in lecture_transcript_candidates.';
COMMENT ON COLUMN public.lecture_transcript_artifacts.meeting_id IS
    'The onlineMeeting this artifact was first acquired through. Per-lecture acquisition context lives on lecture_transcript_candidates.';

COMMIT;
