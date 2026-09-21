UPDATE public.qa_media_jobs j
SET status     = 'pending',
    locked_by  = NULL,
    locked_at  = NULL,
    started_at = NULL,
    not_before = NOW() + (LEAST(j.attempt_count, 6) * INTERVAL '5 minutes'),
    updated_at = NOW()
WHERE j.job_type = 'positive_clip'
  AND j.status = 'failed'
  AND j.attempt_count < j.max_attempts
  AND (%(job_ids)s::bigint[] = '{}'::bigint[] OR j.job_id = ANY(%(job_ids)s::bigint[]))
  AND NOT EXISTS (
      SELECT 1 FROM public.qa_positive_clip_assets a
      WHERE a.clip_key = j.clip_key
        AND a.trim_status = 'completed'
        AND a.clip_url IS NOT NULL
  )
RETURNING job_id, job_key, clip_key, status, attempt_count, max_attempts, priority, not_before
