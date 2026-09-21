-- ===========================================================================
-- public.qa_positive_clip_pipeline_status
--
-- One row per lecture. Fully derived - nothing is stored, so it can never go
-- stale and no boolean flag is added to qa_doctors_sessions.
--
-- pipeline_status:
--   not_analyzed      V5 has not finished for this lecture
--   no_positive_clips V5 final but the clips array is empty
--   awaiting_media    clips exist, nothing queued yet (producer/reconciler due)
--   processing        queued or running; some may already be done
--   completed         every clip has a completed asset
--   partially_failed  some clips delivered, others exhausted their attempts
--   failed            nothing delivered and every job exhausted its attempts
--   origin_missing    V5 final with clips to queue, but clips_media_origin is
--                     NULL - a configuration mistake. Priority is NEVER guessed
--                     from the lecture date, so nothing is queued until an
--                     operator sets the origin.
-- ===========================================================================

-- Dropped and recreated rather than CREATE OR REPLACE: replacing a view cannot
-- insert or reorder columns. Nothing depends on this view (it is read by
-- operators and monitoring queries only).
DROP VIEW IF EXISTS public.qa_positive_clip_pipeline_status;

CREATE VIEW public.qa_positive_clip_pipeline_status AS
WITH clips AS (
    SELECT
        s.session_id,
        s.date,
        s.subject,
        s.trainer,
        s.clips_analysis_completeness,
        s.clips_analyzed_at,
        s.clips_media_origin,
        CASE WHEN jsonb_typeof(s.positive_clips) = 'array'
             THEN jsonb_array_length(s.positive_clips) ELSE 0 END AS positive_clips_count,
        (coalesce(s.recording_drive_id, '') <> '' AND coalesce(s.recording_item_id, '') <> '') AS has_recording
    FROM public.qa_doctors_sessions s
),
jobs AS (
    SELECT
        j.session_id,
        count(*)                                                        AS jobs_total,
        count(*) FILTER (WHERE j.status = 'pending')                    AS jobs_pending,
        count(*) FILTER (WHERE j.status = 'processing')                 AS jobs_processing,
        count(*) FILTER (WHERE j.status = 'completed')                  AS jobs_completed,
        count(*) FILTER (WHERE j.status = 'failed'
                           AND j.attempt_count < j.max_attempts)        AS jobs_failed,
        count(*) FILTER (WHERE j.status = 'failed'
                           AND j.attempt_count >= j.max_attempts)       AS jobs_exhausted,
        max(j.priority)                                                 AS max_priority,
        min(j.created_at)                                               AS first_queued_at,
        max(j.completed_at)                                             AS last_completed_at
    FROM public.qa_media_jobs j
    WHERE j.job_type = 'positive_clip'
    GROUP BY j.session_id
),
assets AS (
    SELECT a.session_id,
           count(*) FILTER (WHERE a.trim_status = 'completed'
                              AND a.clip_url IS NOT NULL) AS assets_completed
    FROM public.qa_positive_clip_assets a
    GROUP BY a.session_id
)
SELECT
    c.session_id,
    c.date,
    c.subject,
    c.trainer,
    c.clips_analysis_completeness                AS analysis_status,
    c.clips_analyzed_at,
    c.clips_media_origin                         AS media_origin,
    CASE c.clips_media_origin WHEN 'live' THEN 100 WHEN 'history' THEN 10 END
                                                 AS origin_priority,
    (c.clips_analysis_completeness = 'positive_clips_v5_final'
     AND c.positive_clips_count > 0
     AND c.clips_media_origin IS NULL
     AND coalesce(a.assets_completed, 0) < c.positive_clips_count) AS origin_missing,
    c.has_recording,
    c.positive_clips_count,
    coalesce(j.jobs_total, 0)                    AS jobs_total,
    coalesce(j.jobs_pending, 0)                  AS jobs_pending,
    coalesce(j.jobs_processing, 0)               AS jobs_processing,
    coalesce(j.jobs_completed, 0)                AS jobs_completed,
    coalesce(j.jobs_failed, 0)                   AS jobs_failed,
    coalesce(j.jobs_exhausted, 0)                AS jobs_exhausted,
    coalesce(a.assets_completed, 0)              AS assets_completed,
    j.max_priority,
    j.first_queued_at,
    j.last_completed_at,
    -- Clips that still have no delivered asset and nothing in flight.
    GREATEST(c.positive_clips_count
             - coalesce(a.assets_completed, 0)
             - coalesce(j.jobs_pending, 0)
             - coalesce(j.jobs_processing, 0)
             - coalesce(j.jobs_failed, 0)
             - coalesce(j.jobs_exhausted, 0), 0) AS clips_unqueued,
    CASE
        WHEN c.clips_analysis_completeness IS DISTINCT FROM 'positive_clips_v5_final'
            THEN 'not_analyzed'
        WHEN c.positive_clips_count = 0
            THEN 'no_positive_clips'
        WHEN c.clips_media_origin IS NULL
             AND coalesce(a.assets_completed, 0) < c.positive_clips_count
            THEN 'origin_missing'
        WHEN coalesce(a.assets_completed, 0) >= c.positive_clips_count
            THEN 'completed'
        WHEN coalesce(j.jobs_pending, 0) + coalesce(j.jobs_processing, 0) > 0
            THEN 'processing'
        WHEN coalesce(j.jobs_exhausted, 0) > 0 AND coalesce(a.assets_completed, 0) > 0
            THEN 'partially_failed'
        WHEN coalesce(j.jobs_exhausted, 0) > 0
            THEN 'failed'
        WHEN coalesce(j.jobs_total, 0) = 0
            THEN 'awaiting_media'
        ELSE 'awaiting_media'
    END AS pipeline_status
FROM clips c
LEFT JOIN jobs   j ON j.session_id = c.session_id
LEFT JOIN assets a ON a.session_id = c.session_id;

COMMENT ON VIEW public.qa_positive_clip_pipeline_status IS
    'Derived per-lecture positive-clip pipeline state. Source of truth; nothing is stored.';
