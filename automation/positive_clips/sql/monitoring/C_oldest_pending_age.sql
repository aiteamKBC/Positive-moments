-- C. Oldest pending job age (queue latency signal)
SELECT
    job_id, job_key, priority,
    CASE WHEN priority >= 100 THEN 'live' ELSE 'history' END AS lane,
    created_at,
    NOW() - created_at                                        AS age,
    round(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600, 2) AS age_hours
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip' AND status = 'pending'
ORDER BY created_at ASC
LIMIT 20;
