-- ===========================================================================
-- READ-ONLY preview of the next historical V5 analysis batch.
--
-- Selects lectures that have NOT reached positive_clips_v5_final, are not
-- cancelled, and have a usable source recording. Ordered newest-first so the
-- most operationally relevant history is analysed before the deep archive.
--
-- Every lecture in this batch is HISTORICAL BACKFILL by definition, so the
-- planned origin is always 'history' and the planned priority always 10 -
-- regardless of how recent the lecture date is.
--
-- Parameter:  :batch_size  (default 25)
--
-- This query NEVER writes. V5 itself establishes completion state - no flag
-- is set here.
-- ===========================================================================

SELECT
    s.session_id,
    s.date,
    s.subject,
    s.trainer,
    s.clips_analysis_completeness       AS current_completeness,
    s.clips_status                      AS current_clips_status,
    s.transcript_id IS NOT NULL         AS has_transcript_id,
    (coalesce(s.recording_drive_id, '') <> '' AND coalesce(s.recording_item_id, '') <> '') AS has_recording,
    lower(coalesce(s.cancelled_session, 'false')) IN ('true','yes','y','1') AS is_cancelled,
    s.clips_media_origin                AS current_origin,
    -- Historical backfill is ALWAYS history, whatever the lecture date says.
    'history'::text                     AS planned_origin,
    10                                  AS planned_priority
FROM public.qa_doctors_sessions s
WHERE coalesce(s.clips_analysis_completeness, '') <> 'positive_clips_v5_final'
  AND lower(coalesce(s.cancelled_session, 'false')) NOT IN ('true', 'yes', 'y', '1')
  AND coalesce(s.recording_drive_id, '') <> ''
  AND coalesce(s.recording_item_id, '') <> ''
ORDER BY s.date DESC, s.session_id ASC
LIMIT %(batch_size)s
