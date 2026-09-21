-- E. Lectures whose every positive clip has been delivered
SELECT session_id, date, subject, trainer, positive_clips_count, assets_completed, last_completed_at
FROM public.qa_positive_clip_pipeline_status
WHERE pipeline_status = 'completed'
ORDER BY date DESC;
