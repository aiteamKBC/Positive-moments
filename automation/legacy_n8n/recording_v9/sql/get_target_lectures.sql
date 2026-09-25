-- Recording v9: lectures in [$2, $3] that still have no recording_url.
-- $1 max_lectures, $2 date_from, $3 date_to (YYYY-MM-DD).
-- The organizer mailbox comes from the coded registry. A meeting that maps to
-- more than one lookup user is reported, never guessed (organizer_count > 1).
WITH organizer AS (
  SELECT ls.meeting_id::text AS meeting_id,
         COUNT(DISTINCT BTRIM(ls.meeting_lookup_user_id)) AS organizer_count,
         MIN(BTRIM(ls.meeting_lookup_user_id)) AS meeting_lookup_user_id
    FROM public.lecture_sessions ls
   WHERE NULLIF(BTRIM(ls.meeting_lookup_user_id), '') IS NOT NULL
     AND NULLIF(BTRIM(ls.meeting_id::text), '') IS NOT NULL
   GROUP BY ls.meeting_id::text
),
target AS (
  SELECT DISTINCT ON (s.session_id)
         p.lecture_key,
         to_char(s.date::date, 'YYYY-MM-DD') AS lecture_date,
         to_char(s.date::date, 'YYYY-MM-DD') AS date,
         to_char(s.date::date, 'YYYY-MM-DD') AS session_date,
         s.subject,
         s.trainer,
         p.module,
         p.engagement,
         p.attended_count,
         p.met_count,
         s.meeting_id,
         s.session_id,
         s.recording_url,
         s.recording_link_status,
         s.cancelled_session,
         o.meeting_lookup_user_id,
         COALESCE(o.organizer_count, 0) AS organizer_count
    FROM public.qa_doctors_sessions s
    LEFT JOIN public.qa_perfect_lectures p
      ON p.session_id = s.session_id
    LEFT JOIN organizer o
      ON o.meeting_id = s.meeting_id::text
   WHERE NULLIF(BTRIM(s.recording_url), '') IS NULL
     AND NULLIF(BTRIM(s.meeting_id), '') IS NOT NULL
     AND NULLIF(BTRIM(s.session_id), '') IS NOT NULL
     AND s.date::date BETWEEN $2::date AND $3::date
   ORDER BY s.session_id, p.lecture_key NULLS LAST
)
SELECT *
  FROM target
 ORDER BY lecture_date, session_id
 LIMIT $1::integer;
