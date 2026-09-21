-- F. Lectures still in flight or partially delivered
SELECT session_id, date, subject, trainer,
       positive_clips_count, assets_completed,
       jobs_pending, jobs_processing, jobs_failed, jobs_exhausted, clips_unqueued,
       pipeline_status
FROM public.qa_positive_clip_pipeline_status
WHERE pipeline_status IN ('awaiting_media', 'processing', 'partially_failed')
ORDER BY max_priority DESC NULLS LAST, date DESC;
