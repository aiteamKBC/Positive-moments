"""
Read-only loaders: LMS occurrences (LMS database) and lecture facts (LI).

Join URLs and Graph meeting ids are reduced to meeting keys the moment they
are read and are never returned, logged or stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg

from app.common.errors import LMS_MODULE_QUERY_ERROR, PlatformError
from app.lms import meeting_key

LMS_OCCURRENCES_SQL = """
SELECT o.id, o.live_session_id, o.session_number, o.status, o.scheduled_start, o.scheduled_end,
       o.join_url, o.online_meeting_id, s.join_url, s.online_meeting_id,
       s.status, s.meeting_type, s.module_catalogue_id,
       m.module_catalogue_id IS NOT NULL,
       (m.deleted_at IS NOT NULL OR coalesce(m.is_programme_deleted, false)),
       m.programme_name
  FROM curriculum.live_session_occurrences o
  JOIN curriculum.live_sessions s ON s.id = o.live_session_id
  LEFT JOIN curriculum.modules m ON m.module_catalogue_id = s.module_catalogue_id
 WHERE o.scheduled_start >= %s AND o.scheduled_start < %s
 ORDER BY o.scheduled_start, o.id
"""
LMS_SHARES_SQL = """
SELECT module_catalogue_id, source_module_catalogue_id
  FROM curriculum.module_teams_shares
 WHERE module_catalogue_id IS NOT NULL AND source_module_catalogue_id IS NOT NULL
"""
LI_LECTURES_SQL = """
SELECT l.lecture_id, l.session_date, l.scheduled_start, l.scheduled_end, l.join_url, l.meeting_id,
       l.metadata -> 'duplicate_suppression' IS NOT NULL
  FROM public.lecture_sessions l
 WHERE l.session_date BETWEEN %s AND %s
 ORDER BY l.session_date, l.scheduled_start, l.lecture_id
"""


def _utc(value: datetime) -> datetime:
    # LMS timestamps are naive UTC (verified 67/67, audit section 4).
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class LmsOccurrence:
    occurrence_id: str
    live_session_id: str
    session_number: int | None
    status: str
    scheduled_start: datetime
    scheduled_end: datetime
    # Keys from the occurrence's and its series' join URLs and meeting ids.
    meeting_keys: frozenset
    # Keys from the online-meeting ids only (the stronger confirmation).
    online_meeting_keys: frozenset
    series_status: str
    meeting_type: str
    module_catalogue_id: str | None
    module_exists: bool
    module_deleted: bool
    programme_name: str | None

    @property
    def meeting_key_hash(self) -> str | None:
        return meeting_key.key_hash(min(self.meeting_keys)) if self.meeting_keys else None


@dataclass(frozen=True)
class LmsSnapshot:
    occurrences: tuple
    # module_catalogue_id -> source_module_catalogue_id (curriculum.module_teams_shares)
    shares: dict
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class LectureFacts:
    lecture_id: str
    session_date: date
    scheduled_start: datetime
    scheduled_end: datetime | None
    meeting_keys: frozenset
    online_meeting_key: str | None
    duplicate_suppressed: bool

    @property
    def meeting_key_hash(self) -> str | None:
        return meeting_key.key_hash(min(self.meeting_keys)) if self.meeting_keys else None


def occurrence_from_row(row) -> LmsOccurrence:
    (oid, sid, number, status, start, end, o_url, o_mid, s_url, s_mid,
     s_status, m_type, module_id, module_exists, module_deleted, programme) = row
    return LmsOccurrence(
        occurrence_id=oid, live_session_id=sid, session_number=number, status=status,
        scheduled_start=_utc(start), scheduled_end=_utc(end),
        meeting_keys=meeting_key.keys_for(join_urls=(o_url, s_url), online_meeting_ids=(o_mid, s_mid)),
        online_meeting_keys=meeting_key.keys_for(online_meeting_ids=(o_mid, s_mid)),
        series_status=s_status, meeting_type=m_type, module_catalogue_id=module_id,
        module_exists=bool(module_exists), module_deleted=bool(module_deleted), programme_name=programme)


def lecture_from_row(row) -> LectureFacts:
    lecture_id, session_date, start, end, url, mid, suppressed = row
    return LectureFacts(
        lecture_id=str(lecture_id), session_date=session_date, scheduled_start=_utc(start),
        scheduled_end=_utc(end) if end else None,
        meeting_keys=meeting_key.keys_for(join_urls=(url,), online_meeting_ids=(mid,)),
        online_meeting_key=meeting_key.from_online_meeting_id(mid), duplicate_suppressed=bool(suppressed))


class LmsOccurrenceSource:
    """Reads the LMS inside a transaction PostgreSQL enforces as read-only."""

    def __init__(self, database_url: str, *, statement_timeout_ms: int = 60_000, connect_timeout: int = 20):
        self._database_url = database_url
        self._timeout = int(statement_timeout_ms)
        self._connect_timeout = int(connect_timeout)

    def load(self, window_start: datetime, window_end: datetime) -> LmsSnapshot:
        if not self._database_url:
            raise PlatformError(LMS_MODULE_QUERY_ERROR, "LMS_DATABASE_URL is not configured")
        try:
            with psycopg.connect(self._database_url, prepare_threshold=None,
                                 connect_timeout=self._connect_timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                if connection.execute("SHOW transaction_read_only").fetchone()[0] != "on":
                    raise PlatformError(LMS_MODULE_QUERY_ERROR, "LMS connection is not read-only")
                connection.execute(f"SET LOCAL statement_timeout = {self._timeout}")
                # LMS columns are naive UTC: compare with naive UTC bounds.
                lo = window_start.astimezone(timezone.utc).replace(tzinfo=None)
                hi = window_end.astimezone(timezone.utc).replace(tzinfo=None)
                rows = connection.execute(LMS_OCCURRENCES_SQL, (lo, hi)).fetchall()
                shares = dict(connection.execute(LMS_SHARES_SQL).fetchall())
                connection.rollback()
        except PlatformError:
            raise
        except Exception as exc:
            # Not chained into the message: a connection error can quote the URL.
            raise PlatformError(LMS_MODULE_QUERY_ERROR,
                                f"LMS occurrence query failed ({type(exc).__name__})") from None
        return LmsSnapshot(occurrences=tuple(occurrence_from_row(r) for r in rows), shares=shares,
                           window_start=window_start, window_end=window_end)


def window_for(date_from: date, date_to: date, *, margin_days: int = 8) -> tuple[datetime, datetime]:
    """LMS window around LI dates: wide enough for the +-7 day reschedule check."""
    start = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=margin_days)
    end = datetime.combine(date_to, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=margin_days + 1)
    return start, end


def load_lectures(connection, date_from: date, date_to: date) -> list[LectureFacts]:
    """LI lectures by session_date, on the caller's connection (any isolation)."""
    cursor = psycopg.ClientCursor(connection)
    cursor.execute(LI_LECTURES_SQL, (date_from, date_to))
    return [lecture_from_row(row) for row in cursor.fetchall()]
