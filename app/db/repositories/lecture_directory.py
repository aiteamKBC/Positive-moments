"""
Read-only registry metadata for the Operations UI.

WHAT THIS IS AND IS NOT
-----------------------
It is descriptive: who taught it, what it is called, when it was scheduled,
and which legacy row the live n8n workflows keep for the same meeting. It is
NOT derived: nothing here decides whether a lecture is complete, waiting,
eligible or synced. Those answers have exactly one home in
`app.orchestration`, and a second, cheaper opinion computed here is the one
that would drift.

The reason it exists at all is cost. Resolving the full fifteen-stage matrix
costs a round trip per stage per lecture, and a console that also wants to
show a trainer name should not pay that again for a column that is a plain
string in a table.

The legacy join is a LATERAL on (meeting_id, date) rather than on meeting_id
alone, because a recurring meeting keeps one id across every occurrence and
joining on it alone would attach an arbitrary week's row to today's lecture.
"""
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


CALENDAR = """
SELECT l.session_date,
       count(*)::int AS lecture_count,
       count(*) FILTER (
           WHERE l.metadata ? 'duplicate_suppression')::int AS suppressed_count
  FROM public.lecture_sessions l
 WHERE l.session_date BETWEEN %s AND %s
 GROUP BY l.session_date
 ORDER BY l.session_date DESC
"""

DIRECTORY = """
SELECT l.lecture_id, l.subject, l.module, l.session_date,
       l.scheduled_start, l.scheduled_end,
       d.session_id, d.trainer, d.clips_status, d.positive_clips_count,
       d.clips_analysis_completeness, d.recording_url, d.recording_link_status
  FROM public.lecture_sessions l
  LEFT JOIN LATERAL (
      SELECT q.session_id, q.trainer, q.clips_status, q.positive_clips_count,
             q.clips_analysis_completeness, q.recording_url,
             q.recording_link_status
        FROM public.qa_doctors_sessions q
       WHERE q.meeting_id = l.meeting_id
         AND q.date = l.session_date
       ORDER BY q.session_id
       LIMIT 1) d ON true
 WHERE l.session_date BETWEEN %s AND %s
 ORDER BY l.session_date DESC, l.scheduled_start, l.lecture_id
"""

FIELDS = ("lecture_id", "subject", "module", "session_date", "scheduled_start",
          "scheduled_end", "legacy_session_id", "trainer",
          "clips_status", "positive_clips_count", "clips_analysis_completeness",
          "recording_url", "recording_link_status")


class LectureDirectoryRepository:
    """Descriptive lecture metadata. One query per call, no derivation."""

    def calendar(self, connection, start, end) -> list[dict]:
        """Which business dates carry lectures, newest first."""
        try:
            rows = connection.execute(CALENDAR, (start, end)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture calendar read failed") from exc
        return [{"session_date": row[0].isoformat(),
                 "lecture_count": row[1],
                 "suppressed_duplicate_count": row[2],
                 "business_lecture_count": row[1] - row[2]}
                for row in rows]

    def for_range(self, connection, start, end) -> list[dict]:
        try:
            rows = connection.execute(DIRECTORY, (start, end)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture directory read failed") from exc
        return [{name: _plain(value) for name, value in zip(FIELDS, row)}
                for row in rows]


def _plain(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
