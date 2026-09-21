-- ===========================================================================
-- Migration 018 (Phase 6B): let the measured media timeline serve the
-- lectures that actually have positive-moment analysis.
--
-- THE PROBLEM
-- -----------
-- Migration 017 keyed `lecture_recording_parts` on `lecture_id`, because every
-- lecture Phase 6A measured was in the canonical registry. The analysed
-- lectures are not: the positive-moment analysis covers 15-25 July 2026 and the
-- canonical registry begins on 4 September 2026. They do not overlap at all.
--
-- Those July lectures are identified by `qa_doctors_sessions.session_id`, which
-- Phase 6B established IS the Microsoft Graph transcript id - so each one
-- resolves to exactly one correlated recording through the same route and the
-- same proven relationship (`recording.createdDateTime` equals the transcript's
-- to the second).
--
-- WHY NOT A SECOND TABLE
-- ----------------------
-- Because it is the same fact. "Where does canonical time T live in media" has
-- one answer and must have one source, or Positive Clips and Lecture Parts will
-- eventually disagree about the same second of the same lecture. A second table
-- would also mean a second `MediaTimeline` builder, and the whole point of
-- Phase 6A was that there is exactly one transform.
--
-- So the row is keyed by EITHER a canonical lecture OR a legacy session, and a
-- CHECK enforces exactly one. A lecture that later enters the canonical
-- registry gets a canonical row; the legacy row remains valid history.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_recording_parts
    ALTER COLUMN lecture_id DROP NOT NULL;

ALTER TABLE public.lecture_recording_parts
    ADD COLUMN IF NOT EXISTS legacy_session_id text;

-- The old constraint was UNIQUE (lecture_id, part_index). With a nullable
-- lecture_id that no longer constrains the legacy rows at all, because NULLs
-- do not collide - which would silently allow two answers for one part.
ALTER TABLE public.lecture_recording_parts
    DROP CONSTRAINT IF EXISTS lecture_recording_parts_identity;

CREATE UNIQUE INDEX IF NOT EXISTS lecture_recording_parts_canonical_identity
    ON public.lecture_recording_parts (lecture_id, part_index)
    WHERE lecture_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS lecture_recording_parts_legacy_identity
    ON public.lecture_recording_parts (legacy_session_id, part_index)
    WHERE legacy_session_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.lecture_recording_parts'::regclass
          AND conname  = 'lecture_recording_parts_one_identity'
    ) THEN
        ALTER TABLE public.lecture_recording_parts
            ADD CONSTRAINT lecture_recording_parts_one_identity CHECK (
                (lecture_id IS NOT NULL AND legacy_session_id IS NULL)
                OR (lecture_id IS NULL AND legacy_session_id IS NOT NULL)
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS lecture_recording_parts_legacy_idx
    ON public.lecture_recording_parts (legacy_session_id, part_index)
    WHERE legacy_session_id IS NOT NULL;

COMMENT ON COLUMN public.lecture_recording_parts.legacy_session_id IS
    'qa_doctors_sessions.session_id, which is the Graph transcript id. Set instead of lecture_id for an analysed lecture that predates the canonical registry. Exactly one of the two is always set.';

COMMIT;
