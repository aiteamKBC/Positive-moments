-- V5-final lectures that still have positive clips needing media jobs but no
-- persisted clips_media_origin. Reconciliation deliberately SKIPS these rather
-- than guessing a priority; they surface here for an operator to resolve.
-- After migration 051 this should normally return zero rows.
SELECT
    s.session_id,
    s.date,
    s.subject,
    s.trainer,
    s.clips_analyzed_at,
    jsonb_array_length(s.positive_clips) AS positive_clips_count,
    -- Diagnostic ONLY. Never applied automatically.
    CASE WHEN s.date >= (NOW()::date - %(live_window_days)s::int)
         THEN 'live (suggestion only)' ELSE 'history (suggestion only)' END AS date_based_hint
FROM public.qa_doctors_sessions s
WHERE s.clips_analysis_completeness = 'positive_clips_v5_final'
  AND jsonb_typeof(s.positive_clips) = 'array'
  AND jsonb_array_length(s.positive_clips) > 0
  AND s.clips_media_origin IS NULL
ORDER BY s.date DESC
