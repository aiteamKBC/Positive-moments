-- ===========================================================================
-- Sync Lecture Part Asset - future COMPLETE endpoint node.
--
-- Runs after "Sync Positive Clip Asset" in the chain. The two are mutually
-- exclusive by job_type, so the order between them does not matter, but both
-- must run after the job row is marked completed.
--
-- Parameter :job_id  (bind as $1)
--
-- Acts ONLY on job_type = 'lecture_part'. A positive_clip job falls out of the
-- WHERE clause and the statement returns zero rows - a no-op, not an error.
--
-- Identity is (session_id, split_version, part_number), taken from the job's
-- metadata. Idempotent: replaying the same completion updates in place.
-- A completed asset is never downgraded to NULL or to a failed state.
-- ===========================================================================

WITH job AS (
    SELECT
        j.job_id, j.session_id, j.part_number, j.metadata,
        j.start_seconds, j.end_seconds, j.attempt_count,
        j.source_drive_id, j.source_item_id,
        j.output_filename, j.output_drive_id, j.output_item_id,
        j.output_web_url, j.output_size_bytes,
        coalesce(j.metadata ->> 'split_version', 'semantic_balanced_v1') AS split_version
    FROM public.qa_media_jobs j
    WHERE j.job_id = $1
      AND j.job_type = 'lecture_part'
      AND j.status   = 'completed'
      AND j.part_number BETWEEN 1 AND 3
      AND j.output_item_id IS NOT NULL
      AND j.output_web_url IS NOT NULL
),
plan AS (
    SELECT j.*, p.split_plan_id
    FROM job j
    JOIN public.qa_lecture_split_plans p
      ON p.session_id = j.session_id
     AND p.split_version = j.split_version
    WHERE p.planner_status = 'planned'
)
INSERT INTO public.qa_lecture_part_assets (
    split_plan_id, session_id, split_version, part_number,
    source_start_seconds, source_end_seconds, duration_seconds,
    source_drive_id, source_item_id,
    output_filename, output_drive_id, output_item_id, output_web_url, output_size_bytes,
    status, error, attempt_count, updated_at
)
SELECT
    p.split_plan_id, p.session_id, p.split_version, p.part_number,
    p.start_seconds, p.end_seconds, round(p.end_seconds - p.start_seconds, 3),
    p.source_drive_id, p.source_item_id,
    p.output_filename, p.output_drive_id, p.output_item_id, p.output_web_url, p.output_size_bytes,
    'completed', NULL, p.attempt_count, NOW()
FROM plan p
ON CONFLICT (session_id, split_version, part_number) DO UPDATE SET
    split_plan_id        = EXCLUDED.split_plan_id,
    source_start_seconds = EXCLUDED.source_start_seconds,
    source_end_seconds   = EXCLUDED.source_end_seconds,
    duration_seconds     = EXCLUDED.duration_seconds,
    source_drive_id      = EXCLUDED.source_drive_id,
    source_item_id       = EXCLUDED.source_item_id,
    output_filename      = EXCLUDED.output_filename,
    output_drive_id      = EXCLUDED.output_drive_id,
    output_item_id       = EXCLUDED.output_item_id,
    output_web_url       = EXCLUDED.output_web_url,
    output_size_bytes    = EXCLUDED.output_size_bytes,
    status               = 'completed',
    error                = NULL,
    attempt_count        = GREATEST(qa_lecture_part_assets.attempt_count, EXCLUDED.attempt_count),
    updated_at           = NOW()
WHERE EXCLUDED.output_item_id IS NOT NULL
  AND EXCLUDED.output_web_url IS NOT NULL
RETURNING part_asset_id, session_id, split_version, part_number, status, duration_seconds
