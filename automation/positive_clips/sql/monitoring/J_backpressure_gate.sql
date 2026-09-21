-- J. Backpressure decision for the historical V5 batch scheduler.
-- Returns a single row; schedule the next history batch only when ok_to_schedule.
SELECT
    count(*) FILTER (WHERE priority <  100 AND status IN ('pending', 'processing')) AS history_in_queue,
    count(*) FILTER (WHERE priority >= 100 AND status IN ('pending', 'processing')) AS live_in_queue,
    200                                                                              AS history_threshold,
    (count(*) FILTER (WHERE priority < 100 AND status IN ('pending', 'processing')) <= 200)
                                                                                     AS ok_to_schedule
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip';
