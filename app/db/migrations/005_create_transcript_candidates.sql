-- ===========================================================================
-- Migration 005 (Phase 2B): separate raw provider artifacts from the lectures
--                           that can see them.
--
-- PROBLEM. Phase 2A keyed a raw artifact by (provider, provider_transcript_id,
-- lecture_id), because a Teams *series* onlineMeeting exposes the transcripts
-- of every occurrence. That is correct as discovery evidence but does not
-- scale: once recurring-series backfill runs, the SAME provider transcript
-- would be stored - raw bytes included - once per lecture that can see it.
--
-- MODEL.
--   lecture_transcript_artifacts   = one row per unique RAW PROVIDER artifact
--   lecture_transcript_candidates  = association between a canonical lecture
--                                    and an artifact visible during that
--                                    lecture's acquisition
--
-- This migration only ADDS the association table. Migration 006 re-keys the
-- artifact table to provider identity once the association has been backfilled
-- by app/cli/normalize_transcript_artifacts.py, so no evidence is ever
-- orphaned between the two steps.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_transcript_candidates (
    candidate_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE RESTRICT,
    artifact_id uuid NOT NULL
        REFERENCES public.lecture_transcript_artifacts (artifact_id) ON DELETE RESTRICT,

    -- The meeting and user context THIS lecture used to reach the artifact.
    -- Another lecture may legitimately reach the same artifact differently.
    meeting_id text NOT NULL,
    meeting_lookup_user_id text NOT NULL,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_candidates_key UNIQUE (lecture_id, artifact_id)
);

CREATE INDEX IF NOT EXISTS lecture_transcript_candidates_lecture_idx
    ON public.lecture_transcript_candidates (lecture_id);
CREATE INDEX IF NOT EXISTS lecture_transcript_candidates_artifact_idx
    ON public.lecture_transcript_candidates (artifact_id);

COMMENT ON TABLE public.lecture_transcript_candidates IS
    'Which canonical lectures could see which raw provider transcript artifact during acquisition. Visibility only: it asserts nothing about which occurrence a transcript belongs to. Phase 2B selection decides that.';

COMMIT;
