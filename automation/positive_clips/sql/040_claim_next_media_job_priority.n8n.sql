WITH stale AS (
    UPDATE public.qa_media_jobs
    SET status = 'pending',
        locked_by = NULL,
        locked_at = NULL,
        updated_at = NOW()
    WHERE status = 'processing'
      AND locked_at < NOW() - ($2::int * INTERVAL '1 minute')
    RETURNING job_id
),
candidate AS (
    SELECT j.job_id
    FROM public.qa_media_jobs j
    WHERE j.status = 'pending'
      AND j.not_before <= NOW()
      AND j.attempt_count < j.max_attempts
    ORDER BY j.priority DESC, j.created_at ASC, j.job_id ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
),
claimed AS (
    UPDATE public.qa_media_jobs j
    SET status = 'processing',
        locked_by = $1,
        locked_at = NOW(),
        started_at = NOW(),
        attempt_count = j.attempt_count + 1,
        updated_at = NOW()
    FROM candidate c
    WHERE j.job_id = c.job_id
    RETURNING j.*
)
SELECT
    c.job_id,
    c.job_key,
    c.session_id,
    c.job_type,
    c.part_number,
    c.clip_key,
    c.start_seconds,
    c.end_seconds,
    c.cut_mode,
    c.output_filename,
    c.source_drive_id,
    c.source_item_id,
    c.destination_drive_id,
    c.destination_folder_item_id,
    c.priority,
    c.attempt_count,
    c.max_attempts,
    c.metadata
FROM claimed c
