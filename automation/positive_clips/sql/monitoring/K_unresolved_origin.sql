-- K. V5-final lectures with clips awaiting media but no persisted origin.
-- Reconciliation skips these instead of guessing. Should normally be empty.
SELECT session_id, date, subject, trainer,
       positive_clips_count, assets_completed, clips_unqueued, pipeline_status
FROM public.qa_positive_clip_pipeline_status
WHERE origin_missing
ORDER BY date DESC;
