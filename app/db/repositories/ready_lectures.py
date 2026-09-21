"""
Read-only Phase 1 input contract.

public.lecture_sessions is the ONLY canonical lecture source for Phase 2A.
Nothing here rediscovers lectures from the calendar, and nothing here writes to
the Phase 1 registry.
"""
from app.common.errors import DATABASE_ERROR, PlatformError
from app.transcripts.models import ReadyLecture


SELECT_DAY = """
SELECT lecture_id, meeting_id, meeting_lookup_user_id, scheduled_start, scheduled_end,
       session_date, subject, module, downstream_ready, discovery_status,
       calendar_mapping_status, meeting_lookup_context_source, join_url_oid_hint_status
  FROM public.lecture_sessions
 WHERE session_date = %s AND NOT is_cancelled
 ORDER BY scheduled_start, subject
"""


class ReadyLectureRepository:
    """Canonical lectures for a business date, split into ready and skipped."""

    def load_day(self, connection, target_date) -> dict:
        try:
            rows = connection.execute(SELECT_DAY, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "canonical lecture query failed") from exc

        ready: list[ReadyLecture] = []
        skipped: list[dict] = []
        for row in rows:
            (lecture_id, meeting_id, user_id, start, end, session_date, subject, module,
             downstream_ready, discovery_status, mapping_status, context_source,
             oid_hint) = row
            # The Phase 1 contract: downstream_ready AND a resolved meeting. The
            # user context must also be present, because it is the only allowed
            # way to address Graph.
            if not downstream_ready or not meeting_id or not user_id:
                skipped.append({
                    "lecture_id": str(lecture_id),
                    "subject": subject,
                    "scheduled_start": start.isoformat(),
                    "downstream_ready": bool(downstream_ready),
                    "has_meeting_id": meeting_id is not None,
                    "has_meeting_lookup_user_id": user_id is not None,
                    "discovery_status": discovery_status,
                    "calendar_mapping_status": mapping_status,
                    "skip_reason": "NOT_DOWNSTREAM_READY",
                })
                continue
            ready.append(ReadyLecture(
                lecture_id=lecture_id, meeting_id=meeting_id,
                meeting_lookup_user_id=user_id, scheduled_start=start, scheduled_end=end,
                session_date=session_date, subject=subject, module=module,
                meeting_lookup_context_source=context_source,
                join_url_oid_hint_status=oid_hint,
            ))
        return {"considered": len(rows), "ready": ready, "skipped": skipped}


class LegacyQaEvidenceRepository:
    """
    Read-only legacy QA evidence.

    The old QA flow stored the SELECTED transcript artifact ID in
    qa_doctors_sessions.session_id. That is legacy compatibility behaviour and
    is deliberately not adopted by the new architecture; here it is used only to
    measure whether Phase 2A rediscovered the same provider transcript IDs.
    """

    def load_day(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(
                "SELECT session_id, subject, meeting_id FROM public.qa_doctors_sessions "
                "WHERE date = %s ORDER BY subject",
                (target_date,),
            ).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy QA evidence query failed") from exc
        return [{"session_id": row[0], "subject": row[1], "meeting_id": row[2]} for row in rows]
