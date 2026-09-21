-- H. Historical backlog remaining (priority 10)
SELECT
    count(*) FILTER (WHERE status = 'pending')                                   AS pending,
    count(*) FILTER (WHERE status = 'processing')                                AS processing,
    count(*) FILTER (WHERE status = 'failed' AND attempt_count <  max_attempts)  AS failed_retryable,
    count(*) FILTER (WHERE status = 'failed' AND attempt_count >= max_attempts)  AS exhausted,
    count(*) FILTER (WHERE status = 'completed')                                 AS completed
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip' AND priority < 100;
