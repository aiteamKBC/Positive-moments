-- ===========================================================================
-- public.qa_lecture_split_pipeline_status
--
-- One row per lecture: those with a split plan, plus every lecture that could
-- be planned but has not been. Fully derived; nothing is stored.
--
-- pipeline_status:
--   not_planned       no split plan exists yet
--   planning_failed   a plan exists with planner_status='rejected'
--   planned           plan accepted, no media jobs created yet
--   queued            jobs exist, none started
--   processing        at least one part in flight or partially delivered
--   completed         all three parts delivered
--   partially_failed  some parts exhausted their attempts
-- ===========================================================================

DROP VIEW IF EXISTS public.qa_lecture_split_pipeline_status;

CREATE VIEW public.qa_lecture_split_pipeline_status AS
WITH plans AS (
    SELECT p.session_id, p.split_version, p.split_plan_id,
           p.planner_status, p.planner_confidence, p.media_origin,
           p.recording_duration_seconds,
           p.cut_1_seconds, p.cut_2_seconds, p.created_at AS planned_at
    FROM public.qa_lecture_split_plans p
),
jobs AS (
    SELECT j.session_id,
           coalesce(j.metadata ->> 'split_version', 'semantic_balanced_v1') AS split_version,
           count(*)                                                          AS parts_total,
           count(*) FILTER (WHERE j.status = 'pending')                      AS parts_pending,
           count(*) FILTER (WHERE j.status = 'processing')                   AS parts_processing,
           count(*) FILTER (WHERE j.status = 'completed')                    AS parts_completed,
           count(*) FILTER (WHERE j.status = 'failed'
                              AND j.attempt_count <  j.max_attempts)         AS parts_failed,
           count(*) FILTER (WHERE j.status = 'failed'
                              AND j.attempt_count >= j.max_attempts)         AS parts_exhausted,
           max(j.priority)                                                   AS max_priority
    FROM public.qa_media_jobs j
    WHERE j.job_type = 'lecture_part'
    GROUP BY 1, 2
),
assets AS (
    SELECT a.session_id, a.split_version,
           count(*) FILTER (WHERE a.status = 'completed'
                              AND a.output_item_id IS NOT NULL) AS assets_completed
    FROM public.qa_lecture_part_assets a
    GROUP BY 1, 2
)
SELECT
    s.session_id,
    s.date,
    s.subject,
    s.trainer,
    s.clips_media_origin                          AS media_origin,
    s.recording_duration_seconds                  AS session_recording_duration_seconds,
    (coalesce(s.recording_drive_id, '') <> ''
     AND coalesce(s.recording_item_id, '') <> '') AS has_recording,
    p.split_version,
    p.planner_status,
    p.planner_confidence,
    p.recording_duration_seconds                  AS planned_duration_seconds,
    p.cut_1_seconds,
    p.cut_2_seconds,
    p.planned_at,
    coalesce(j.parts_total, 0)                    AS parts_total,
    coalesce(j.parts_pending, 0)                  AS parts_pending,
    coalesce(j.parts_processing, 0)               AS parts_processing,
    coalesce(j.parts_completed, 0)                AS parts_completed,
    coalesce(j.parts_failed, 0)                   AS parts_failed,
    coalesce(j.parts_exhausted, 0)                AS parts_exhausted,
    coalesce(a.assets_completed, 0)               AS assets_completed,
    j.max_priority,
    CASE
        WHEN p.planner_status IS NULL                       THEN 'not_planned'
        WHEN p.planner_status = 'rejected'                  THEN 'planning_failed'
        WHEN coalesce(a.assets_completed, 0) >= 3           THEN 'completed'
        WHEN coalesce(j.parts_exhausted, 0) > 0             THEN 'partially_failed'
        WHEN coalesce(j.parts_processing, 0) > 0
             OR coalesce(a.assets_completed, 0) > 0
             OR coalesce(j.parts_completed, 0) > 0          THEN 'processing'
        WHEN coalesce(j.parts_total, 0) > 0                 THEN 'queued'
        ELSE 'planned'
    END AS pipeline_status
FROM public.qa_doctors_sessions s
LEFT JOIN plans  p ON p.session_id = s.session_id
LEFT JOIN jobs   j ON j.session_id = s.session_id AND j.split_version = p.split_version
LEFT JOIN assets a ON a.session_id = s.session_id AND a.split_version = p.split_version;

COMMENT ON VIEW public.qa_lecture_split_pipeline_status IS
    'Derived per-lecture three-part split pipeline state. Source of truth; nothing stored.';
