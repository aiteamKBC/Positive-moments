-- G. Exhausted jobs requiring manual review (never auto-requeued)
SELECT job_id, job_key, session_id, clip_key, priority,
       attempt_count, max_attempts, updated_at,
       left(coalesce(error, ''), 300) AS error_excerpt
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip'
  AND status = 'failed'
  AND attempt_count >= max_attempts
ORDER BY updated_at DESC;
