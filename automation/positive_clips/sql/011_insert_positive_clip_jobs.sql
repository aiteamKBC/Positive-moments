-- ===========================================================================
-- Idempotent INSERT of positive-clip media jobs.
-- Prepend 010_positive_clip_candidates.sql (its trailing SELECT is replaced
-- by this INSERT); the runner and the n8n node both compose them this way.
--
-- Safe to run repeatedly. A second run inserts 0 rows.
-- ===========================================================================

INSERT INTO public.qa_media_jobs (
    job_key, session_id, job_type, part_number, clip_key,
    start_seconds, end_seconds, cut_mode, output_filename,
    source_drive_id, source_item_id,
    destination_drive_id, destination_folder_item_id,
    status, attempt_count, not_before, metadata
)
SELECT
    c.job_key,
    c.session_id,
    'positive_clip',
    NULL,                      -- part_number is only for lecture_part jobs
    c.clip_key,
    c.start_seconds,
    c.end_seconds,
    'precise',
    c.output_filename,
    c.recording_drive_id,
    c.recording_item_id,
    %(dest_drive_id)s,
    %(dest_folder_item_id)s,
    'pending',
    0,
    NOW(),
    jsonb_strip_nulls(jsonb_build_object(
        'lecture_date',           to_char(c.lecture_date::date, 'YYYY-MM-DD'),
        'subject',                c.subject,
        'trainer',                c.trainer,
        'clip_index',             c.clip_index,
        'original_start',         c.original_start,
        'original_end',           c.original_end,
        'original_duration_seconds', round(c.orig_end_seconds - c.orig_start_seconds, 3),
        'start_cue',              c.start_cue,
        'end_cue',                c.end_cue,
        'speaker',                c.clip ->> 'speaker',
        'positive_speakers',      c.clip -> 'positive_speakers',
        'category',               c.clip ->> 'category',
        'positive_quote',         c.clip ->> 'positive_quote',
        'quote',                  c.clip ->> 'quote',
        'reason',                 c.clip ->> 'reason',
        'confidence',             c.clip -> 'confidence',
        'semantic_verification',  c.clip -> 'semantic_verification',
        'padding_before_seconds', 60,
        'padding_after_seconds',  60,
        'produced_by',            'positive-clip-producer-v1'
    ))
FROM candidates c
WHERE
    -- A completed asset means the clip is already delivered. Never re-queue it.
    (c.existing_asset_status IS DISTINCT FROM 'completed')
    -- An active or finished job already covers this clip. A 'failed' job is
    -- deliberately NOT excluded: it is retried through the queue, not duplicated
    -- (the job_key UNIQUE constraint makes the retry reuse the same row).
    AND (c.existing_job_status IS NULL OR c.existing_job_status NOT IN ('pending', 'processing', 'completed'))
ON CONFLICT (job_key) DO NOTHING
RETURNING job_id, job_key, clip_key, session_id, job_type, status,
          start_seconds, end_seconds,
          round(end_seconds - start_seconds, 3) AS duration_seconds,
          cut_mode, output_filename
