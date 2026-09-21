-- Read-only summary of how much history still needs V5 analysis.
SELECT
    count(*)                                                                   AS total_not_v5_final,
    count(*) FILTER (WHERE lower(coalesce(cancelled_session,'false')) IN ('true','yes','y','1')) AS cancelled,
    count(*) FILTER (WHERE coalesce(recording_drive_id,'') = ''
                        OR coalesce(recording_item_id,'') = '')                AS missing_recording,
    count(*) FILTER (WHERE lower(coalesce(cancelled_session,'false')) NOT IN ('true','yes','y','1')
                       AND coalesce(recording_drive_id,'') <> ''
                       AND coalesce(recording_item_id,'') <> '')               AS eligible_for_v5,
    min(date)                                                                  AS oldest,
    max(date)                                                                  AS newest
FROM public.qa_doctors_sessions
WHERE coalesce(clips_analysis_completeness, '') <> 'positive_clips_v5_final';
