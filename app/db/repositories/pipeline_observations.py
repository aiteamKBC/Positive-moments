"""
Phase 4A: read-only observation of the two stages the coded platform does NOT
own.

The recording branch of QA Master Daily v8 fills `recording_url`. The "QA
Perfect Lectures - Excel Sync" workflow stamps `excel_synced_at`. Both are
live, both are correct, and Phase 4A's job is to be able to SAY whether they
have run - not to run them, not to wait on them, and above all not to write
the columns they own.

Every statement here is a SELECT. There is no write path in this module at
all, which is the only form of "will not write" that survives a refactor.
"""
from app.common.errors import DATABASE_ERROR, PlatformError


RECORDING_LINK = """
SELECT s.session_id, s.recording_url
  FROM public.qa_doctors_sessions s
 WHERE s.session_id = %s
"""

EXCEL_SYNC = """
SELECT p.lecture_key, p.excel_synced_at
  FROM public.qa_perfect_lectures p
 WHERE p.lecture_key = %s
"""

RECORDING_COUNTS = """
SELECT count(*)::int,
       count(*) FILTER (WHERE s.recording_url IS NOT NULL)::int
  FROM public.qa_doctors_sessions s
 WHERE s.session_id = ANY(%s)
"""


class LegacyObservationRepository:
    """Read-only windows onto the legacy rows and the two columns they own."""

    def legacy_qa_session(self, connection, legacy_session_id) -> dict | None:
        """
        The legacy QA row, or None. Existence is itself a fact the orchestrator
        needs: a row the coded platform did not write is PROTECTED, and asking
        "does one exist?" is how that is discovered without assuming it.
        """
        try:
            row = connection.execute(RECORDING_LINK, (legacy_session_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy qa session read failed") from exc
        if row is None:
            return None
        return {"legacy_session_id": row[0], "recording_url": row[1]}

    def recording_link(self, connection, legacy_session_id) -> dict | None:
        return self.legacy_qa_session(connection, legacy_session_id)

    def legacy_perfect_row(self, connection, legacy_lecture_key) -> dict | None:
        return self.excel_sync(connection, legacy_lecture_key)

    def excel_sync(self, connection, legacy_lecture_key) -> dict | None:
        try:
            row = connection.execute(EXCEL_SYNC, (legacy_lecture_key,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "excel sync read failed") from exc
        if row is None:
            return None
        return {"legacy_lecture_key": row[0],
                "excel_synced_at": row[1].isoformat() if row[1] else None}

    def recording_counts(self, connection, legacy_session_ids) -> dict:
        ids = [value for value in legacy_session_ids if value]
        if not ids:
            return {"legacy_rows": 0, "recording_present": 0}
        try:
            row = connection.execute(RECORDING_COUNTS, (ids,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "recording link read failed") from exc
        return {"legacy_rows": row[0], "recording_present": row[1]}
