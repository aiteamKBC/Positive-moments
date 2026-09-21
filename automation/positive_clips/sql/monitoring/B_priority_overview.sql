-- B. Pending work split by priority lane
SELECT
    priority,
    CASE WHEN priority >= 100 THEN 'live' ELSE 'history' END AS lane,
    count(*)                                                  AS pending_jobs,
    count(*) FILTER (WHERE not_before <= NOW())               AS claimable_now,
    min(created_at)                                           AS oldest_queued
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip' AND status = 'pending'
GROUP BY priority
ORDER BY priority DESC;
