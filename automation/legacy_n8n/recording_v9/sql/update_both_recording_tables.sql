-- Recording v9: persist ONE exact recording match.
-- $1 session_id, $2 recording_url, $3 recording_item_id, $4 recording_drive_id,
-- $5 recording_filename, $6 recording_link_status, $7 lecture_key,
-- $8 meeting_id, $9 recording_match_method, $10 dry_run ('true'/'false').
--
-- Every guard is repeated here, in the database, so a mis-wired node cannot
-- write: dry_run must be exactly false, the match must be the v9 exact method,
-- and an existing recording_url is never replaced. Only recording-owned
-- columns are written. QA columns are not referenced.
WITH input AS (
  SELECT
    NULLIF($1, '')::text AS session_id,
    NULLIF($2, '')::text AS recording_url,
    NULLIF($3, '')::text AS recording_item_id,
    NULLIF($4, '')::text AS recording_drive_id,
    NULLIF($5, '')::text AS recording_filename,
    NULLIF($6, '')::text AS recording_link_status,
    NULLIF($7, '')::text AS lecture_key,
    NULLIF($8, '')::text AS meeting_id,
    NULLIF($9, '')::text AS recording_match_method,
    NULLIF($10, '')::boolean AS dry_run
),
eligible AS (
  SELECT i.*
    FROM input i
   WHERE i.dry_run IS FALSE
     AND i.recording_match_method = 'exact_call_id_subject_timestamp_v9'
     AND i.recording_link_status IN ('organization_view_link_created_exact_match',
                                     'drive_item_web_url_used_exact_match')
     AND i.session_id IS NOT NULL
     AND i.meeting_id IS NOT NULL
     AND i.recording_url IS NOT NULL
     AND i.recording_item_id IS NOT NULL
     AND i.recording_drive_id IS NOT NULL
),
session_update AS (
  UPDATE public.qa_doctors_sessions s
  SET
    recording_url = i.recording_url,
    recording_item_id = i.recording_item_id,
    recording_drive_id = i.recording_drive_id,
    recording_filename = i.recording_filename,
    recording_link_status = i.recording_link_status,
    recording_link_updated_at = NOW()
  FROM eligible i
  WHERE s.session_id = i.session_id
    AND s.meeting_id = i.meeting_id
    AND NULLIF(BTRIM(s.recording_url), '') IS NULL
  RETURNING s.session_id
),
perfect_update AS (
  UPDATE public.qa_perfect_lectures p
  SET
    recording_url = i.recording_url,
    meeting_id = COALESCE(p.meeting_id, i.meeting_id),
    session_id = COALESCE(p.session_id, i.session_id)
  FROM eligible i
  WHERE p.lecture_key = i.lecture_key
    AND (p.session_id IS NULL OR p.session_id = i.session_id)
    AND NULLIF(BTRIM(p.recording_url), '') IS NULL
    AND EXISTS (SELECT 1 FROM session_update)
  RETURNING p.lecture_key
)
SELECT
  i.session_id,
  i.lecture_key,
  i.recording_url,
  i.recording_filename,
  i.recording_link_status,
  i.dry_run,
  EXISTS (SELECT 1 FROM eligible) AS write_eligible,
  EXISTS (SELECT 1 FROM session_update) AS session_updated,
  EXISTS (SELECT 1 FROM perfect_update) AS perfect_updated
FROM input i;
