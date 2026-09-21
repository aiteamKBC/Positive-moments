-- ===========================================================================
-- Migration 019 (Phase 6C): give each recording part its own source location.
--
-- THE DEFECT THIS CLOSES
-- ----------------------
-- `qa_media_jobs` carries one `source_drive_id`/`source_item_id` per job, and
-- the lecture-part producer filled both from `qa_doctors_sessions`, which
-- carries one recording location per SESSION. For a single-recording lecture
-- that is correct and the Femi pilot proved it end to end.
--
-- For a lecture recorded in two parts it is silently wrong. Andrew-Scheduling
-- Professional (2026-09-16) is two recordings joined at 14336.082s; its part 3
-- is media 0.000 -> 2859.290 OF THE SECOND RECORDING. Pointed at the first
-- recording's file, that range is the opening 48 minutes of the lecture. The
-- worker would download a real file, cut a real range, upload it as "Part 3"
-- and report success. Nothing in the pipeline would notice - not the duration
-- check, not the coverage check, not the asset sync - because every number
-- involved is internally consistent. Only a viewer would find out.
--
-- So the source location belongs where the recording is described, one per
-- part, not one per session. Phase 6A's write-up flagged this ("the existing
-- schema carries a single source_drive_id/source_item_id pair and will need
-- extending"); this is that extension.
--
-- WHY NULLABLE
-- ------------
-- Because the columns are populated by whatever locates a recording in
-- SharePoint, and that is upstream of this platform. A NULL means "not
-- located yet", which the producer must treat as a refusal for a multipart
-- lecture rather than a reason to fall back to the session's single location.
-- For a single-recording lecture the session location remains authoritative
-- and needs no row here, which is why no backfill is attempted.
-- ===========================================================================

BEGIN;

ALTER TABLE public.lecture_recording_parts
    ADD COLUMN IF NOT EXISTS source_drive_id text,
    ADD COLUMN IF NOT EXISTS source_item_id  text,
    ADD COLUMN IF NOT EXISTS source_located_at timestamptz;

-- Half a location is worse than none: it would pass a naive presence check and
-- then fail deep inside the worker, after a download attempt.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.lecture_recording_parts'::regclass
          AND conname  = 'lecture_recording_parts_source_pair'
    ) THEN
        ALTER TABLE public.lecture_recording_parts
            ADD CONSTRAINT lecture_recording_parts_source_pair
            CHECK ((source_drive_id IS NULL) = (source_item_id IS NULL));
    END IF;
END $$;

COMMENT ON COLUMN public.lecture_recording_parts.source_drive_id IS
    'SharePoint drive holding THIS recording part. NULL = not located yet; a '
    'multipart lecture cannot be cut until every part has one.';
COMMENT ON COLUMN public.lecture_recording_parts.source_item_id IS
    'SharePoint item for THIS recording part, not for the session.';

COMMIT;
