"""
Persistence for the coded RECORDING_LINK stage.

Three statements matter:

* TARGET - what the stage knows about one lecture: the canonical lecture row,
  its legacy QA row, and (if any) its legacy Perfect row. Read-only.
* the attempt row in `lecture_recording_links` (migration 021) - the stage's
  own durable memory. Coded-owned.
* WRITE - the ONLY statement that touches a legacy table. It is the v9 guarded
  UPDATE with every guard kept in SQL: an empty recording_url, the exact
  meeting, the v9 match method, an exact link status. It names only the six
  recording-owned columns of qa_doctors_sessions and, for the Perfect row,
  recording_url plus meeting_id / session_id only where they are NULL.

The write runs in a savepoint and re-reads the row: if anything other than the
recording-owned columns changed, it rolls back and raises. That check is the
executable form of "QA fields are byte-identical before and after".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from app.common.errors import DATABASE_ERROR, PlatformError
from app.recordings.models import (
    ATTEMPTS_EXHAUSTED,
    LINK_DRIVE_ITEM_WEB_URL,
    LINK_ORGANIZATION_VIEW,
    MATCH_METHOD,
    MAX_ATTEMPTS,
    RECORDING_LINK_VERSION,
    RETRY_BASE_HOURS,
    RETRY_MAX_HOURS,
    RETRYABLE_STATUSES,
    STATE_REVIEW,
    STATE_WAITING,
    RecordingTarget,
)
from app.transcripts.identity import canonical_transcript_identity


# The six columns this stage owns on public.qa_doctors_sessions.
SESSION_RECORDING_COLUMNS = (
    "recording_url", "recording_item_id", "recording_drive_id",
    "recording_filename", "recording_link_status", "recording_link_updated_at",
)
PERFECT_RECORDING_COLUMNS = ("recording_url", "meeting_id", "session_id")

TARGET = """
SELECT l.lecture_id, l.session_date, l.meeting_id, l.meeting_lookup_user_id,
       l.meeting_organizer_user_id, l.is_cancelled,
       q.session_id, q.meeting_id, q.subject, q.recording_url, q.cancelled_session,
       p.lecture_key, p.recording_url
  FROM public.lecture_sessions l
  LEFT JOIN public.qa_doctors_sessions q ON q.session_id = %(session_id)s
  LEFT JOIN LATERAL (
        SELECT pl.lecture_key, pl.recording_url
          FROM public.qa_perfect_lectures pl
         WHERE pl.session_id = q.session_id
         ORDER BY pl.lecture_key
         LIMIT 1) p ON true
 WHERE l.lecture_id = %(lecture_id)s
"""

ATTEMPT = """
SELECT status, stage_state, reason, attempt_count, next_attempt_after,
       recording_url_written, legacy_session_id, last_attempted_at
  FROM public.lecture_recording_links
 WHERE lecture_id = %s
"""

UPSERT_ATTEMPT = """
INSERT INTO public.lecture_recording_links (
    lecture_id, legacy_session_id, link_version, status, stage_state, reason,
    graph_lookup_status, graph_http_status, graph_recording_count,
    candidate_file_count, exact_candidate_count, timestamp_difference_seconds,
    match_method, recording_item_id, recording_drive_id, recording_filename,
    recording_url_written, attempt_count, first_attempted_at, last_attempted_at,
    next_attempt_after, written_at, metadata)
VALUES (
    %(lecture_id)s, %(legacy_session_id)s, %(link_version)s, %(status)s,
    %(stage_state)s, %(reason)s, %(graph_lookup_status)s, %(graph_http_status)s,
    %(graph_recording_count)s, %(candidate_file_count)s, %(exact_candidate_count)s,
    %(timestamp_difference_seconds)s, %(match_method)s, %(recording_item_id)s,
    %(recording_drive_id)s, %(recording_filename)s, %(recording_url_written)s,
    1, %(now)s, %(now)s, %(next_attempt_after)s, %(written_at)s, %(metadata)s)
ON CONFLICT (lecture_id) DO UPDATE SET
    legacy_session_id            = EXCLUDED.legacy_session_id,
    link_version                 = EXCLUDED.link_version,
    status                       = EXCLUDED.status,
    stage_state                  = EXCLUDED.stage_state,
    reason                       = EXCLUDED.reason,
    graph_lookup_status          = EXCLUDED.graph_lookup_status,
    graph_http_status            = EXCLUDED.graph_http_status,
    graph_recording_count        = EXCLUDED.graph_recording_count,
    candidate_file_count         = EXCLUDED.candidate_file_count,
    exact_candidate_count        = EXCLUDED.exact_candidate_count,
    timestamp_difference_seconds = EXCLUDED.timestamp_difference_seconds,
    match_method                 = EXCLUDED.match_method,
    recording_item_id            = EXCLUDED.recording_item_id,
    recording_drive_id           = EXCLUDED.recording_drive_id,
    recording_filename           = EXCLUDED.recording_filename,
    recording_url_written        = public.lecture_recording_links.recording_url_written
                                   OR EXCLUDED.recording_url_written,
    attempt_count                = public.lecture_recording_links.attempt_count + 1,
    last_attempted_at            = EXCLUDED.last_attempted_at,
    next_attempt_after           = EXCLUDED.next_attempt_after,
    written_at                   = COALESCE(public.lecture_recording_links.written_at,
                                            EXCLUDED.written_at),
    metadata                     = EXCLUDED.metadata
RETURNING attempt_count
"""

# Everything on the row EXCEPT the recording-owned columns, as one value.
SESSION_FINGERPRINT = """
SELECT md5((to_jsonb(q) - %(owned)s::text[])::text)
  FROM public.qa_doctors_sessions q
 WHERE q.session_id = %(session_id)s
"""
PERFECT_FINGERPRINT = """
SELECT md5((to_jsonb(p) - %(owned)s::text[])::text)
  FROM public.qa_perfect_lectures p
 WHERE p.lecture_key = %(lecture_key)s
"""

WRITE = """
WITH input AS (
  SELECT %(session_id)s::text            AS session_id,
         %(meeting_id)s::text            AS meeting_id,
         %(recording_url)s::text         AS recording_url,
         %(recording_item_id)s::text     AS recording_item_id,
         %(recording_drive_id)s::text    AS recording_drive_id,
         %(recording_filename)s::text    AS recording_filename,
         %(recording_link_status)s::text AS recording_link_status,
         %(match_method)s::text          AS match_method,
         %(lecture_key)s::text           AS lecture_key
),
eligible AS (
  SELECT i.* FROM input i
   WHERE i.match_method = %(required_method)s
     AND i.recording_link_status = ANY(%(link_statuses)s::text[])
     AND i.session_id IS NOT NULL AND i.meeting_id IS NOT NULL
     AND NULLIF(BTRIM(i.recording_url), '') IS NOT NULL
     AND i.recording_item_id IS NOT NULL AND i.recording_drive_id IS NOT NULL
),
session_update AS (
  UPDATE public.qa_doctors_sessions s
     SET recording_url = i.recording_url,
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
     SET recording_url = i.recording_url,
         meeting_id = COALESCE(p.meeting_id, i.meeting_id),
         session_id = COALESCE(p.session_id, i.session_id)
    FROM eligible i
   WHERE p.lecture_key = i.lecture_key
     AND (p.session_id IS NULL OR p.session_id = i.session_id)
     AND NULLIF(BTRIM(p.recording_url), '') IS NULL
     AND EXISTS (SELECT 1 FROM session_update)
  RETURNING p.lecture_key
)
SELECT EXISTS (SELECT 1 FROM eligible),
       (SELECT count(*) FROM session_update)::int,
       (SELECT count(*) FROM perfect_update)::int
"""


def _db(message):
    return PlatformError(DATABASE_ERROR, message)


def retry_after(attempt_count: int, now: datetime) -> datetime:
    hours = min(RETRY_BASE_HOURS * (2 ** max(attempt_count - 1, 0)), RETRY_MAX_HOURS)
    return now + timedelta(hours=hours)


class RecordingLinkRepository:

    # -- reads ---------------------------------------------------------------

    def target(self, connection, lecture_id, legacy_session_id) -> RecordingTarget | None:
        try:
            row = connection.execute(TARGET, {"lecture_id": str(lecture_id),
                                              "session_id": legacy_session_id}).fetchone()
        except Exception as exc:
            raise _db("recording link target read failed") from exc
        if row is None:
            return None
        (lecture_id_, session_date, lecture_meeting, lookup_user, organizer_user,
         is_cancelled, session_id, legacy_meeting, subject, recording_url,
         cancelled_session, lecture_key, perfect_url) = row
        legacy_cancelled = str(cancelled_session or "").strip().lower() == "true"
        identity = canonical_transcript_identity(session_id) if session_id else None
        return RecordingTarget(
            lecture_id=str(lecture_id_), session_date=session_date.isoformat(),
            legacy_session_id=session_id, legacy_meeting_id=legacy_meeting,
            lecture_meeting_id=lecture_meeting, subject=subject,
            meeting_lookup_user_id=lookup_user,
            meeting_organizer_user_id=organizer_user,
            existing_recording_url=recording_url,
            cancelled=bool(is_cancelled) or legacy_cancelled,
            cancelled_reason=("LECTURE_CANCELLED" if is_cancelled else
                              "LEGACY_ROW_CANCELLED_SESSION" if legacy_cancelled else None),
            lecture_key=lecture_key,
            perfect_recording_url_empty=(None if lecture_key is None
                                         else not str(perfect_url or "").strip()),
            thread_id=identity.thread_id if identity and identity.decoded else None)

    def attempt(self, connection, lecture_id) -> dict | None:
        try:
            row = connection.execute(ATTEMPT, (str(lecture_id),)).fetchone()
        except Exception as exc:
            raise _db("recording link state read failed") from exc
        if row is None:
            return None
        keys = ("status", "stage_state", "reason", "attempt_count",
                "next_attempt_after", "recording_url_written", "legacy_session_id",
                "last_attempted_at")
        return dict(zip(keys, row))

    # -- the stage's own memory ---------------------------------------------

    def record(self, connection, decision, *, now: datetime | None = None,
               written: bool = False) -> dict:
        now = now or datetime.now(timezone.utc)
        previous = self.attempt(connection, decision.lecture_id)
        attempts = (previous["attempt_count"] if previous else 0) + 1
        stage_state = decision.stage_state
        reason = decision.reason
        next_after = None
        if decision.status in RETRYABLE_STATUSES:
            if attempts >= MAX_ATTEMPTS:
                stage_state, reason = STATE_REVIEW, f"{ATTEMPTS_EXHAUSTED}: {reason}"
            else:
                stage_state, next_after = STATE_WAITING, retry_after(attempts, now)
        graph, match = decision.graph, decision.match
        candidate = match.candidate if match else None
        params = {
            "lecture_id": decision.lecture_id,
            "legacy_session_id": decision.legacy_session_id,
            "link_version": RECORDING_LINK_VERSION,
            "status": decision.status, "stage_state": stage_state,
            "reason": (reason or "")[:500],
            "graph_lookup_status": graph.status if graph else None,
            "graph_http_status": graph.http_status if graph else None,
            "graph_recording_count": graph.recordings_returned if graph else None,
            "candidate_file_count": match.candidate_file_count if match else None,
            "exact_candidate_count": match.exact_candidate_count if match else None,
            "timestamp_difference_seconds": (match.timestamp_difference_seconds
                                             if match else None),
            "match_method": MATCH_METHOD if candidate else None,
            "recording_item_id": candidate.item_id if candidate else None,
            "recording_drive_id": candidate.drive_id if candidate else None,
            "recording_filename": candidate.name if candidate else None,
            "recording_url_written": written, "now": now,
            "next_attempt_after": next_after,
            "written_at": now if written else None,
            "metadata": Jsonb(decision.attempt_detail()),
        }
        try:
            connection.execute(UPSERT_ATTEMPT, params)
        except Exception as exc:
            raise _db("recording link state write failed") from exc
        return {"stage_state": stage_state, "next_attempt_after": next_after,
                "attempt_count": attempts}

    # -- the legacy write ------------------------------------------------------

    def _fingerprints(self, connection, session_id, lecture_key):
        session = connection.execute(SESSION_FINGERPRINT, {
            "owned": list(SESSION_RECORDING_COLUMNS), "session_id": session_id}).fetchone()
        perfect = None
        if lecture_key:
            perfect = connection.execute(PERFECT_FINGERPRINT, {
                "owned": list(PERFECT_RECORDING_COLUMNS),
                "lecture_key": lecture_key}).fetchone()
        return (session[0] if session else None, perfect[0] if perfect else None)

    def write_link(self, connection, *, session_id, meeting_id, lecture_key,
                   recording_url, candidate, link_status) -> dict:
        params = {
            "session_id": session_id, "meeting_id": meeting_id,
            "recording_url": recording_url,
            "recording_item_id": candidate.item_id,
            "recording_drive_id": candidate.drive_id,
            "recording_filename": candidate.name,
            "recording_link_status": link_status,
            "match_method": MATCH_METHOD, "required_method": MATCH_METHOD,
            "link_statuses": [LINK_ORGANIZATION_VIEW, LINK_DRIVE_ITEM_WEB_URL],
            "lecture_key": lecture_key,
        }
        try:
            with connection.transaction():
                before = self._fingerprints(connection, session_id, lecture_key)
                eligible, sessions, perfects = connection.execute(WRITE, params).fetchone()
                after = self._fingerprints(connection, session_id, lecture_key)
                if before != after:
                    raise _VerificationFailed()
        except _VerificationFailed:
            raise PlatformError(DATABASE_ERROR,
                                "recording link write changed a non-recording column; "
                                "rolled back") from None
        except PlatformError:
            raise
        except Exception as exc:
            raise _db("recording link write failed") from exc
        return {"write_eligible": bool(eligible), "sessions_updated": sessions,
                "perfect_rows_updated": perfects}


class _VerificationFailed(Exception):
    pass
