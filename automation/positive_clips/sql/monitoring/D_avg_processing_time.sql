-- D. Processing time of completed jobs
SELECT
    count(*)                                                                    AS completed_jobs,
    round(avg(EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1)      AS avg_seconds,
    round(min(EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1)      AS min_seconds,
    round(max(EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1)      AS max_seconds,
    round(avg(output_size_bytes) / 1048576.0, 2)                                 AS avg_output_mb,
    round(avg(EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric
          / NULLIF(avg(end_seconds - start_seconds), 0), 3)                      AS seconds_per_clip_second
FROM public.qa_media_jobs
WHERE job_type = 'positive_clip' AND status = 'completed'
  AND started_at IS NOT NULL AND completed_at IS NOT NULL;
