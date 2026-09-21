"""
Phase 4C1: persistence for duplicate calendar event resolution.

Three reads and one write, and the write is the interesting one: it annotates
a row, it never removes one. There is no DELETE in this file and there is no
statement that touches any table other than `public.lecture_sessions`.
"""
import json

from app.common.errors import DATABASE_ERROR, PlatformError
from app.lectures.duplicates import SUPPRESSION_KEY


COLUMNS = """
       l.lecture_id, l.calendar_event_id, l.i_cal_uid, l.meeting_id,
       l.calendar_mapping_status, l.organizer_validation_status,
       l.discovery_status, l.downstream_ready, l.is_cancelled,
       l.session_date, l.normalized_subject, l.module,
       l.scheduled_start, l.scheduled_end, l.subject,
       l.metadata -> '{key}'
""".format(key=SUPPRESSION_KEY)

DAY = f"""
SELECT {COLUMNS}
  FROM public.lecture_sessions l
 WHERE l.session_date = %s
 ORDER BY l.scheduled_start, l.lecture_id
"""

# The group one lecture belongs to, including the lecture itself. Exact
# equality on every component - the same key `group_key` builds - so the
# database and the pure rule can never disagree about membership.
GROUP_FOR_LECTURE = f"""
SELECT {COLUMNS}
  FROM public.lecture_sessions l
  JOIN public.lecture_sessions self ON self.lecture_id = %s
 WHERE l.session_date = self.session_date
   AND l.normalized_subject = self.normalized_subject
   AND l.module IS NOT DISTINCT FROM self.module
   AND l.scheduled_start = self.scheduled_start
   AND l.scheduled_end = self.scheduled_end
 ORDER BY l.scheduled_start, l.lecture_id
"""

ALL_DUPLICATE_GROUPS = f"""
SELECT {COLUMNS}
  FROM public.lecture_sessions l
  JOIN (
        SELECT session_date, normalized_subject, module,
               scheduled_start, scheduled_end
          FROM public.lecture_sessions
         WHERE NOT is_cancelled
         GROUP BY 1, 2, 3, 4, 5
        HAVING count(*) > 1
       ) g
    ON g.session_date = l.session_date
   AND g.normalized_subject = l.normalized_subject
   AND g.module IS NOT DISTINCT FROM l.module
   AND g.scheduled_start = l.scheduled_start
   AND g.scheduled_end = l.scheduled_end
 ORDER BY l.session_date, l.scheduled_start, l.lecture_id
"""

# The safety check. A row that has ever produced downstream work is not a
# duplicate to retire quietly, whatever the calendar says - so suppression
# refuses rather than hiding evidence that the occurrence was real.
FOOTPRINT = """
SELECT
  (SELECT count(*) FROM public.lecture_transcript_candidates c
    WHERE c.lecture_id = %(id)s)::int,
  (SELECT count(*) FROM public.lecture_transcript_documents d
    WHERE d.lecture_id = %(id)s)::int,
  (SELECT count(*) FROM public.lecture_qa_evaluations e
    WHERE e.lecture_id = %(id)s)::int,
  (SELECT count(*) FROM public.lecture_qa_legacy_writes w
    WHERE w.lecture_id = %(id)s)::int,
  (SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes p
    WHERE p.lecture_id = %(id)s)::int
"""

FOOTPRINT_KEYS = ("transcript_candidates", "canonical_documents",
                  "qa_evaluations", "legacy_qa_writes", "perfect_writes")

# `metadata || ...` MERGES: every discovery diagnostic the row already carries
# is kept, and only the suppression key is added. `downstream_ready = false` is
# restated rather than assumed, because the whole point of the annotation is
# that this occurrence never enters the pipeline.
#
# The WHERE clause is the idempotency: a row that already carries the
# annotation is not touched, so `resolved_at` stays the moment the decision was
# actually made and `updated_at` does not churn on every cycle.
SUPPRESS = f"""
UPDATE public.lecture_sessions
   SET metadata = metadata || %(annotation)s::jsonb,
       downstream_ready = false,
       updated_at = now()
 WHERE lecture_id = %(lecture_id)s
   AND NOT (metadata ? '{SUPPRESSION_KEY}')
   AND meeting_id IS NULL
RETURNING lecture_id
"""


class DuplicateEventRepository:
    """Reads the registry, annotates one row. Never deletes, never merges."""

    def load_day(self, connection, session_date) -> list[dict]:
        return self._rows(connection, DAY, (session_date,),
                          "duplicate candidate read failed")

    def load_group_for_lecture(self, connection, lecture_id) -> list[dict]:
        return self._rows(connection, GROUP_FOR_LECTURE, (str(lecture_id),),
                          "duplicate group read failed")

    def load_all_duplicate_groups(self, connection) -> list[dict]:
        return self._rows(connection, ALL_DUPLICATE_GROUPS, (),
                          "duplicate group scan failed")

    def downstream_footprint(self, connection, lecture_id) -> dict:
        try:
            row = connection.execute(FOOTPRINT, {"id": str(lecture_id)}).fetchone()
        except Exception as exc:
            raise PlatformError(
                DATABASE_ERROR, "duplicate footprint check failed") from exc
        return dict(zip(FOOTPRINT_KEYS, row))

    def suppress(self, connection, lecture_id, annotation: dict) -> bool:
        """
        Annotate one occurrence as a suppressed duplicate.

        Returns whether this call was the one that wrote it. False means the
        row already carried an annotation, or acquired a meeting between the
        decision and the write - both of which are correct outcomes, not
        errors.
        """
        try:
            row = connection.execute(SUPPRESS, {
                "lecture_id": str(lecture_id),
                "annotation": json.dumps({SUPPRESSION_KEY: annotation}),
            }).fetchone()
        except Exception as exc:
            raise PlatformError(
                DATABASE_ERROR, "duplicate suppression write failed") from exc
        return row is not None

    def _rows(self, connection, sql, params, message) -> list[dict]:
        try:
            rows = connection.execute(sql, params).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, message) from exc
        return [_row(row) for row in rows]


def _row(row) -> dict:
    return {
        "lecture_id": str(row[0]), "calendar_event_id": row[1],
        "i_cal_uid": row[2], "meeting_id": row[3],
        "calendar_mapping_status": row[4], "organizer_validation_status": row[5],
        "discovery_status": row[6], "downstream_ready": row[7],
        "is_cancelled": row[8], "session_date": row[9],
        "normalized_subject": row[10], "module": row[11],
        "scheduled_start": row[12], "scheduled_end": row[13],
        "subject": row[14], "suppression": row[15],
    }
