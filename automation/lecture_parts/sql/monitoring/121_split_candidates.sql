-- Lectures that could be split but have no plan yet.
--
-- recording_duration_seconds is REQUIRED: part 3 ends at the true end of the
-- recording, so a lecture without a measured duration is not a candidate.
-- blocked_missing_duration shows how many are waiting only on that measurement.
SELECT session_id,
       date,
       subject,
       trainer,
       media_origin,
       session_recording_duration_seconds,
       has_recording,
       (session_recording_duration_seconds IS NULL) AS blocked_missing_duration
FROM public.qa_lecture_split_pipeline_status
WHERE pipeline_status = 'not_planned'
  AND has_recording
ORDER BY date DESC;
