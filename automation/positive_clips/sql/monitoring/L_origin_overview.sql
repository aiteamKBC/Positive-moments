-- L. Persisted origin coverage across analysed lectures.
SELECT
    coalesce(media_origin, '(unset)')                              AS media_origin,
    origin_priority,
    count(*)                                                       AS lectures,
    sum(positive_clips_count)                                      AS clips,
    sum(clips_unqueued)                                            AS clips_awaiting_queue,
    count(*) FILTER (WHERE origin_missing)                         AS origin_missing
FROM public.qa_positive_clip_pipeline_status
WHERE analysis_status = 'positive_clips_v5_final'
GROUP BY 1, 2
ORDER BY origin_priority DESC NULLS LAST;
