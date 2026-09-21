-- ===========================================================================
-- Migration 101: a place to persist the REAL recording duration.
--
-- qa_doctors_sessions.duration is free text at minute granularity
-- ("2 hours 13 minutes"). It describes the meeting, not the recording file,
-- and is far too coarse to end part 3 on. Part 3 runs to the end of the
-- recording, so an inaccurate value would truncate or overrun the lecture.
--
-- This column is the interface for a real value, sourced from
-- Microsoft Graph driveItem.video.duration (milliseconds) or ffprobe.
-- It is deliberately left NULL - no value is fabricated here.
-- ===========================================================================

BEGIN;

ALTER TABLE public.qa_doctors_sessions
    ADD COLUMN IF NOT EXISTS recording_duration_seconds numeric(12,3);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.qa_doctors_sessions'::regclass
          AND conname  = 'qa_doctors_sessions_recording_duration_check'
    ) THEN
        ALTER TABLE public.qa_doctors_sessions
            ADD CONSTRAINT qa_doctors_sessions_recording_duration_check
            CHECK (recording_duration_seconds IS NULL OR recording_duration_seconds > 0);
    END IF;
END $$;

COMMENT ON COLUMN public.qa_doctors_sessions.recording_duration_seconds IS
    'True duration of the source recording in seconds, from Graph driveItem.video.duration or ffprobe. NULL until measured - never inferred from the free-text duration column.';

COMMIT;
