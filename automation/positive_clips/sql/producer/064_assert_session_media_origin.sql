-- ===========================================================================
-- Persist the V5 processing origin on the lecture BEFORE any media job exists.
--
-- The producer MUST run this first. If it succeeds and the job insert then
-- fails, hourly reconciliation still recovers the correct priority, because the
-- origin is already on the lecture row.
--
-- Parameters:
--   :session_ids     text[]  lectures to stamp (must not be empty)
--   :mode            text    'live' or 'history'
--   :allow_override  bool    false in normal operation. true is a deliberate
--                            administrative override and is reported as such.
--
-- Outcomes per session:
--   set        origin was NULL, now stamped
--   unchanged  origin already equals the requested mode (idempotent re-run)
--   conflict   origin differs and override was NOT granted - NOTHING changed
--   overridden origin differs and override WAS granted - value replaced
--   not_found  session_id does not exist
--
-- A conflict never silently converts history to live or live to history. The
-- caller is expected to stop when any row comes back 'conflict'.
-- ===========================================================================

WITH requested AS (
    SELECT unnest(%(session_ids)s::text[]) AS session_id
),
current AS (
    SELECT r.session_id,
           s.session_id IS NOT NULL AS exists,
           s.clips_media_origin     AS stored_origin,
           s.clips_analysis_completeness AS analysis_status
    FROM requested r
    LEFT JOIN public.qa_doctors_sessions s ON s.session_id = r.session_id
),
decided AS (
    SELECT c.*,
           CASE
               WHEN NOT c.exists                               THEN 'not_found'
               WHEN c.stored_origin IS NULL                    THEN 'set'
               WHEN c.stored_origin = %(mode)s::text           THEN 'unchanged'
               WHEN %(allow_override)s::boolean                THEN 'overridden'
               ELSE 'conflict'
           END AS outcome
    FROM current c
),
applied AS (
    UPDATE public.qa_doctors_sessions s
    SET clips_media_origin = %(mode)s::text
    FROM decided d
    WHERE s.session_id = d.session_id
      AND d.outcome IN ('set', 'overridden')
    RETURNING s.session_id
)
SELECT
    d.session_id,
    %(mode)s::text        AS requested_mode,
    d.stored_origin       AS previous_origin,
    d.analysis_status,
    d.outcome,
    CASE WHEN d.outcome IN ('set', 'overridden', 'unchanged') THEN %(mode)s::text
         ELSE d.stored_origin END AS effective_origin,
    CASE WHEN d.outcome IN ('set', 'overridden', 'unchanged')
         THEN (CASE WHEN %(mode)s::text = 'live' THEN 100 ELSE 10 END)
         ELSE NULL END    AS effective_priority
FROM decided d
