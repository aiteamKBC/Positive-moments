-- A. Queue overview by status (positive_clip jobs)
SELECT
    status,
    count(*) FILTER (WHERE status <> 'failed')                                AS jobs,
    count(*) FILTER (WHERE status = 'failed' AND attempt_count <  max_attempts) AS failed_retryable,
    count(*) FILTER (WHERE status = 'failed' AND attempt_count >= max_attempts) AS failed_exhausted,
    count(*)                                                                   AS total
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip'
GROUP BY status
ORDER BY status;
