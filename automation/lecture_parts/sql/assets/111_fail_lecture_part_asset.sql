-- FAIL endpoint companion for lecture parts. UPDATE-only and guarded, so a
-- failed retry can never destroy a delivered part, and a failure never creates
-- an asset row.
-- Parameters: $1 = job_id, $2 = redacted error text
UPDATE public.qa_lecture_part_assets a
SET status        = 'failed',
    error         = left($2, 4000),
    attempt_count = GREATEST(a.attempt_count, j.attempt_count),
    updated_at    = NOW()
FROM public.qa_media_jobs j
WHERE j.job_id = $1
  AND j.job_type = 'lecture_part'
  AND a.session_id    = j.session_id
  AND a.split_version = coalesce(j.metadata ->> 'split_version', 'semantic_balanced_v1')
  AND a.part_number   = j.part_number
  AND a.status IS DISTINCT FROM 'completed'
  AND a.output_item_id IS NULL
RETURNING a.part_asset_id, a.session_id, a.part_number, a.status
