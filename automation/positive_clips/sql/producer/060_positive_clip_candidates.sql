-- ===========================================================================
-- KBC positive-clip media-job producer  (canonical, idempotent)
--
-- Expands qa_doctors_sessions.positive_clips (V5) into queueable media jobs.
-- Pure SQL: every timestamp, key and filename is derived deterministically.
-- No AI is asked to compute seconds.
--
-- Named placeholders (psycopg / n8n "Execute Query" style):
--   :dest_drive_id        destination SharePoint drive id
--   :dest_folder_item_id  destination SharePoint folder item id
--   :session_ids          text[] of session_ids; empty array = all V5 lectures
--   :clip_keys            text[] of clip_keys; empty array = no clip filter
--
-- Priority comes from the lecture's persisted clips_media_origin, never from
-- the lecture date. Clips whose lecture has no origin are still returned here
-- so monitoring can see them, but the INSERT skips them.
--
-- Idempotency: a clip is excluded when it already has a completed asset OR a
-- pending/processing/completed media job. The final INSERT additionally relies
-- on the qa_media_jobs_job_key_key UNIQUE constraint.
-- ===========================================================================

WITH expanded AS (
    SELECT
        s.session_id,
        s.date                         AS lecture_date,
        s.subject,
        s.trainer,
        s.recording_drive_id,
        s.recording_item_id,
        s.clips_media_origin           AS media_origin,
        c.ord::int                     AS clip_index,
        c.clip
    FROM public.qa_doctors_sessions s
    CROSS JOIN LATERAL jsonb_array_elements(s.positive_clips) WITH ORDINALITY AS c(clip, ord)
    WHERE s.clips_analysis_completeness = 'positive_clips_v5_final'
      AND jsonb_typeof(s.positive_clips) = 'array'
      AND jsonb_array_length(s.positive_clips) > 0
      AND coalesce(s.recording_drive_id, '') <> ''
      AND coalesce(s.recording_item_id, '') <> ''
      AND coalesce(s.session_id, '') <> ''
      AND (cardinality(%(session_ids)s::text[]) = 0 OR s.session_id = ANY(%(session_ids)s::text[]))
),
parsed AS (
    SELECT
        e.*,
        e.clip ->> 'start'                       AS original_start,
        e.clip ->> 'end'                         AS original_end,
        e.clip ->> 'start_cue'                   AS start_cue_txt,
        e.clip ->> 'end_cue'                     AS end_cue_txt,
        -- Strict HH:MM:SS.mmm validation. Anything else is rejected, never guessed.
        (e.clip ->> 'start') ~ '^\d{1,3}:[0-5]\d:[0-5]\d\.\d{1,3}$' AS start_ts_valid,
        (e.clip ->> 'end')   ~ '^\d{1,3}:[0-5]\d:[0-5]\d\.\d{1,3}$' AS end_ts_valid,
        (e.clip ->> 'start_cue') ~ '^\d+$'       AS start_cue_valid,
        (e.clip ->> 'end_cue')   ~ '^\d+$'       AS end_cue_valid
    FROM expanded e
),
computed AS (
    SELECT
        p.*,
        -- interval arithmetic is exact to the microsecond and fully deterministic
        CASE WHEN p.start_ts_valid THEN
            round(EXTRACT(EPOCH FROM p.original_start::interval)::numeric, 3) END AS orig_start_seconds,
        CASE WHEN p.end_ts_valid THEN
            round(EXTRACT(EPOCH FROM p.original_end::interval)::numeric, 3)   END AS orig_end_seconds
    FROM parsed p
),
padded AS (
    SELECT
        c.*,
        -- Business rule: 60s before, 60s after the positive moment.
        GREATEST(0::numeric, c.orig_start_seconds - 60) AS start_seconds,
        c.orig_end_seconds + 60                         AS end_seconds,
        c.start_cue_txt::int                            AS start_cue,
        c.end_cue_txt::int                              AS end_cue
    FROM computed c
    WHERE c.start_ts_valid AND c.end_ts_valid
      AND c.start_cue_valid AND c.end_cue_valid
      AND c.orig_end_seconds > c.orig_start_seconds     -- original_end > original_start
),
keyed AS (
    SELECT
        p.*,
        -- Stable clip identity, unchanged from the existing system.
        p.session_id || ':' || p.start_cue || ':' || p.end_cue                    AS clip_key,
        -- Deterministic media-job key. md5(session_id)[:8] is the same short id
        -- already used in existing clip filenames.
        'positive:' || left(md5(p.session_id), 8) || ':' || p.start_cue || ':' || p.end_cue AS job_key,
        -- Filename matches the existing convention exactly:
        -- YYYYMMDD_<safe-subject>_<short-id>_clip-NN_cue-X-Y.mp4
        to_char(p.lecture_date::date, 'YYYYMMDD') || '_'
          || left(trim(BOTH '-' FROM regexp_replace(coalesce(p.subject, 'lecture'), '[^A-Za-z0-9]+', '-', 'g')), 60) || '_'
          || left(md5(p.session_id), 8) || '_clip-'
          || lpad(p.clip_index::text, 2, '0') || '_cue-'
          || p.start_cue || '-' || p.end_cue || '.mp4'                            AS output_filename
    FROM padded p
    WHERE p.start_seconds >= 0
      AND p.end_seconds > p.start_seconds
),
candidates AS (
    SELECT
        k.*,
        round(k.end_seconds - k.start_seconds, 3) AS requested_duration_seconds,
        a.trim_status AS existing_asset_status,
        j.status      AS existing_job_status
    FROM keyed k
    LEFT JOIN public.qa_positive_clip_assets a ON a.clip_key = k.clip_key
    LEFT JOIN LATERAL (
        SELECT mj.status FROM public.qa_media_jobs mj
        WHERE mj.clip_key = k.clip_key
        ORDER BY CASE mj.status WHEN 'completed' THEN 1 WHEN 'processing' THEN 2
                                WHEN 'pending' THEN 3 ELSE 4 END
        LIMIT 1
    ) j ON TRUE
    WHERE (cardinality(%(clip_keys)s::text[]) = 0 OR k.clip_key = ANY(%(clip_keys)s::text[]))
)
SELECT * FROM candidates
