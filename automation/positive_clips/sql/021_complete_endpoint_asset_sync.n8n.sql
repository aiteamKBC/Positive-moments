-- ===========================================================================
-- COMPLETE endpoint - qa_positive_clip_assets synchronisation (dual identity)
--
-- Runs in the n8n "kbc-media-jobs-complete" workflow AFTER the SharePoint
-- upload succeeded and AFTER qa_media_jobs was marked completed.
-- Only positive_clip jobs produce an asset; lecture_part jobs are ignored.
--
-- Placeholder: :job_id   (bind as $1 / $1)
--
-- qa_positive_clip_assets carries TWO identities, both of which are preserved:
--     PRIMARY KEY (clip_key)
--     UNIQUE      (session_id, source_start, source_end)
-- Either one can match an existing asset, so a naive ON CONFLICT (clip_key)
-- upsert would raise a unique violation when a re-analysis produced different
-- cue numbers for an identical time range. This statement resolves both.
--
-- Behaviour:
--   A  look up an existing asset by clip_key OR by (session_id, start, end)
--   B  if found -> UPDATE that row with the successful result
--   C  if not found -> INSERT a new row
--   D  never two assets for the same session/time range (both constraints held)
--   E  never overwrite a completed asset with failed/NULL data
--   F  keep the existing clip_key when matched only on the temporal identity
--   G  transaction advisory lock + ON CONFLICT DO NOTHING so a concurrent
--      duplicate completion cannot crash the endpoint
--
-- This is ONE statement, therefore atomic in its own implicit transaction.
-- ===========================================================================

WITH job AS (
    SELECT
        j.job_id, j.clip_key, j.session_id, j.metadata,
        j.start_seconds, j.end_seconds, j.attempt_count,
        j.source_drive_id, j.source_item_id,
        j.output_filename, j.output_drive_id, j.output_item_id, j.output_web_url,
        s.recording_url
    FROM public.qa_media_jobs j
    LEFT JOIN public.qa_doctors_sessions s ON s.session_id = j.session_id
    WHERE j.job_id = $1
      AND j.job_type = 'positive_clip'
      AND j.status   = 'completed'
      AND j.clip_key IS NOT NULL
      -- an asset must never describe an upload that did not land
      AND j.output_item_id  IS NOT NULL
      AND j.output_web_url  IS NOT NULL
      -- source_start / source_end are NOT NULL in the target table
      AND j.metadata ->> 'original_start' IS NOT NULL
      AND j.metadata ->> 'original_end'   IS NOT NULL
),
payload AS (
    SELECT
        j.*,
        j.metadata ->> 'original_start' AS source_start,
        j.metadata ->> 'original_end'   AS source_end,
        coalesce((j.metadata ->> 'clip_index')::int, 1) AS clip_index,
        j.metadata ->> 'speaker'  AS speaker,
        j.metadata ->> 'category' AS category,
        coalesce(j.metadata ->> 'positive_quote', j.metadata ->> 'quote', '') AS quote,
        j.metadata ->> 'reason'   AS reason,
        nullif(j.metadata ->> 'confidence', '')::numeric AS confidence,
        round(j.end_seconds - j.start_seconds, 3) AS duration_seconds,
        -- (G) Serialises concurrent completions for this exact clip identity.
        -- Released automatically at commit. Taken here so that the lookup in
        -- `target` cannot race another transaction's insert.
        pg_advisory_xact_lock(
            hashtext(j.session_id || '|' || (j.metadata ->> 'original_start')
                                  || '|' || (j.metadata ->> 'original_end'))
        ) AS lock_acquired
    FROM job j
),
target AS (
    -- (A) resolve by either identity; (F) an exact clip_key match wins, so the
    -- existing row's own clip_key is what we later update by.
    SELECT p.*, existing.clip_key AS existing_clip_key,
           existing.trim_status   AS existing_status
    FROM payload p
    LEFT JOIN LATERAL (
        SELECT a.clip_key, a.trim_status
        FROM public.qa_positive_clip_assets a
        WHERE a.clip_key = p.clip_key
           OR (a.session_id   = p.session_id
           AND a.source_start = p.source_start
           AND a.source_end   = p.source_end)
        ORDER BY (a.clip_key = p.clip_key) DESC, a.created_at ASC
        LIMIT 1
    ) existing ON TRUE
),
updated AS (
    -- (B) update in place. (F) clip_key is NOT in the SET list, so a row matched
    -- only on the temporal identity keeps its original clip_key.
    UPDATE public.qa_positive_clip_assets a
    SET clip_index                = t.clip_index,
        source_start              = t.source_start,
        source_end                = t.source_end,
        trim_start_seconds        = t.start_seconds,
        trim_end_seconds          = t.end_seconds,
        duration_seconds          = t.duration_seconds,
        speaker                   = coalesce(t.speaker,   a.speaker),
        category                  = coalesce(t.category,  a.category),
        quote                     = coalesce(nullif(t.quote, ''), a.quote),
        reason                    = coalesce(t.reason,    a.reason),
        confidence                = coalesce(t.confidence, a.confidence),
        source_recording_drive_id = t.source_drive_id,
        source_recording_item_id  = t.source_item_id,
        source_recording_url      = coalesce(t.recording_url, a.source_recording_url),
        clip_filename             = t.output_filename,
        clip_drive_id             = t.output_drive_id,
        clip_item_id              = t.output_item_id,
        clip_url                  = t.output_web_url,
        trim_status               = 'completed',
        trim_error                = NULL,
        trim_attempts             = GREATEST(a.trim_attempts, t.attempt_count),
        updated_at                = NOW()
    FROM target t
    WHERE a.clip_key = t.existing_clip_key
      -- (E) only a genuine success may overwrite; guaranteed by the job filter
      -- above and asserted again here.
      AND t.output_item_id IS NOT NULL
      AND t.output_web_url IS NOT NULL
    RETURNING a.clip_key, 'updated'::text AS action
),
inserted AS (
    -- (C) no existing asset under either identity -> create one.
    INSERT INTO public.qa_positive_clip_assets (
        clip_key, session_id, clip_index,
        source_start, source_end,
        trim_start_seconds, trim_end_seconds, duration_seconds,
        speaker, category, quote, reason, confidence,
        source_recording_drive_id, source_recording_item_id, source_recording_url,
        clip_filename, clip_drive_id, clip_item_id, clip_url,
        trim_status, trim_error, trim_attempts, updated_at
    )
    SELECT
        t.clip_key, t.session_id, t.clip_index,
        t.source_start, t.source_end,
        t.start_seconds, t.end_seconds, t.duration_seconds,
        t.speaker, t.category, t.quote, t.reason, t.confidence,
        t.source_drive_id, t.source_item_id, t.recording_url,
        t.output_filename, t.output_drive_id, t.output_item_id, t.output_web_url,
        'completed', NULL, t.attempt_count, NOW()
    FROM target t
    WHERE t.existing_clip_key IS NULL
    -- (G) untargeted DO NOTHING covers BOTH unique constraints, so a racing
    -- duplicate is absorbed silently instead of raising a unique violation.
    ON CONFLICT DO NOTHING
    RETURNING clip_key, 'inserted'::text AS action
)
SELECT action, clip_key FROM updated
UNION ALL
SELECT action, clip_key FROM inserted
