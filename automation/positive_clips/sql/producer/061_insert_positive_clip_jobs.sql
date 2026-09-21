INSERT INTO public.qa_media_jobs (
    job_key, session_id, job_type, part_number, clip_key,
    start_seconds, end_seconds, cut_mode, output_filename,
    source_drive_id, source_item_id,
    destination_drive_id, destination_folder_item_id,
    status, attempt_count, priority, not_before, metadata
)
SELECT
    c.job_key,
    c.session_id,
    'positive_clip',
    NULL,
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
    CASE c.media_origin WHEN 'live' THEN 100::smallint ELSE 10::smallint END,
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
        'queue_origin',           c.media_origin,
        'produced_by',            %(produced_by)s::text,
        'produced_at',            to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
    ))
FROM candidates c
WHERE
    (c.existing_asset_status IS DISTINCT FROM 'completed')
    AND (c.existing_job_status IS NULL OR c.existing_job_status NOT IN ('pending', 'processing', 'completed'))
    AND c.media_origin IN ('history', 'live')
ON CONFLICT (job_key) DO NOTHING
RETURNING job_id, job_key, clip_key, session_id, job_type, status, priority,
          start_seconds, end_seconds,
          round(end_seconds - start_seconds, 3) AS duration_seconds,
          cut_mode, output_filename, metadata ->> 'queue_origin' AS queue_origin
