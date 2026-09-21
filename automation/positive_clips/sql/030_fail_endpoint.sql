-- ===========================================================================
-- FAIL endpoint — job failure handling.
--
-- Runs in the n8n "kbc-media-jobs-fail" workflow.
-- Placeholders: :job_id, :worker_id, :error  (error already redacted
-- by the worker; it never contains Graph URLs or tokens).
--
-- Statement 1: mark the job failed, retaining attempt_count.
-- Statement 2: record the failure on the asset ONLY if no successful asset
--              already exists. A completed asset is never downgraded.
-- No asset row is ever CREATED by a failure.
-- ===========================================================================

-- 1) Job row -----------------------------------------------------------------
UPDATE public.qa_media_jobs
SET status     = 'failed',
    error      = left(%(error)s, 12000),
    locked_by  = NULL,
    locked_at  = NULL,
    -- back-off before the queue offers it again; the row is reused, never duplicated
    not_before = NOW() + (INTERVAL '5 minutes' * GREATEST(attempt_count, 1)),
    updated_at = NOW()
WHERE job_id = %(job_id)s
RETURNING job_id, job_key, clip_key, status, attempt_count, max_attempts, not_before;

-- 2) Asset row (guarded, update-only) ----------------------------------------
UPDATE public.qa_positive_clip_assets a
SET trim_status   = 'failed',
    trim_error    = left(%(error)s, 4000),
    trim_attempts = GREATEST(a.trim_attempts, j.attempt_count),
    updated_at    = NOW()
FROM public.qa_media_jobs j
WHERE j.job_id = %(job_id)s
  AND j.job_type = 'positive_clip'
  AND a.clip_key = j.clip_key
  -- THE PROTECTION RULE: successful asset data always wins.
  AND a.trim_status IS DISTINCT FROM 'completed'
  AND a.clip_url IS NULL
RETURNING a.clip_key, a.trim_status, a.trim_attempts;
