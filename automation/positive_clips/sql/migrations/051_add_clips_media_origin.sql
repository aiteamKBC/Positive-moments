-- ===========================================================================
-- Migration 051: explicit, persistent V5 processing origin per lecture.
--
-- Replaces the LIVE_WINDOW_DAYS date heuristic. A lecture can be recent by date
-- yet still be part of a historical backfill, so date can never decide this.
-- The origin is recorded on the lecture BEFORE media jobs are created, so it
-- survives a failure between "V5 result saved" and "media job created" - the
-- exact gap that qa_media_jobs.metadata.queue_origin cannot cover.
--
--   history -> priority 10
--   live    -> priority 100
--
-- One explicit field. No booleans, no is_live/is_history pair, no audit table.
-- Additive and nullable: qa_doctors_sessions is managed = False in Django, so
-- the ORM neither sees nor migrates this column.
-- ===========================================================================

BEGIN;

ALTER TABLE public.qa_doctors_sessions
    ADD COLUMN IF NOT EXISTS clips_media_origin text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.qa_doctors_sessions'::regclass
          AND conname  = 'qa_doctors_sessions_clips_media_origin_check'
    ) THEN
        ALTER TABLE public.qa_doctors_sessions
            ADD CONSTRAINT qa_doctors_sessions_clips_media_origin_check
            CHECK (clips_media_origin IS NULL OR clips_media_origin IN ('history', 'live'));
    END IF;
END $$;

-- Pre-production backlog: everything V5 has ALREADY analysed predates the
-- live cutover and is therefore history. Only rows with no explicit origin are
-- touched; unanalysed / future rows are deliberately left NULL.
UPDATE public.qa_doctors_sessions
SET clips_media_origin = 'history'
WHERE clips_analysis_completeness = 'positive_clips_v5_final'
  AND clips_media_origin IS NULL;

COMMENT ON COLUMN public.qa_doctors_sessions.clips_media_origin IS
    'Explicit V5 processing origin: history (priority 10) or live (priority 100). Set by the producer before media jobs are created. Never inferred from lecture date.';

COMMIT;
