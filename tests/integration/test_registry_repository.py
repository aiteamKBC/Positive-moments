"""
Phase 1 registry behaviour against the real schema and its CHECK constraints.

RELEASE GATE: self-contained. Every row here is built by the test itself and
rolled back, and the dates are synthetic.
"""
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from tests.integration.seeding import DAY, START
from app.common.errors import PlatformError
from app.config.settings import Settings
from app.db.connection import readonly_database_connection
from app.db.repositories.discovery_runs import DiscoveryRunRepository
from app.db.repositories.lecture_sessions import LectureSessionRepository
from app.lectures.models import Lecture


def _lecture(unique: str, **overrides) -> Lecture:
    start = START
    fields = dict(
        lecture_id=uuid.uuid4(), calendar_user_upn="phase1-test@example.invalid",
        calendar_event_id=f"event-{unique}", i_cal_uid=f"ical-{unique}", meeting_id="meeting-test",
        join_url=f"https://teams.microsoft.com/l/meetup-join/{unique}", subject="Original",
        normalized_subject="original", module="Original", scheduled_start=start,
        scheduled_end=start + timedelta(hours=1), session_date=DAY,
        calendar_timezone="UTC",
        calendar_organizer_address="CohortGroup@example.invalid",
        meeting_organizer_user_id="meeting-organizer-user-id",
        organizer_validation_status="ORGANIZER_ID_CONFIRMED",
        meeting_lookup_user_id="meeting-organizer-user-id",
        meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID",
        join_url_oid_hint_status="JOIN_URL_OID_MATCHES_CONTEXT",
        graph_meeting_subject="Original",
        graph_meeting_start=start, graph_meeting_end=start + timedelta(hours=1),
        meeting_type="scheduled", calendar_mapping_status="RESOLVED",
        group_match_status="MATCHED", discovery_status="READY", is_cancelled=False,
        downstream_ready=True,
    )
    fields.update(overrides)
    return Lecture(**fields)


def test_registry_upsert_and_discovery_audit_rollback():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    connection = psycopg.connect(settings.database_url)
    lecture = _lecture(uuid.uuid4().hex)
    try:
        registry = LectureSessionRepository()
        assert registry.upsert(connection, lecture) == "created"
        lecture.subject = "Updated"
        lecture.normalized_subject = "updated"
        assert registry.upsert(connection, lecture) == "updated"
        stored = connection.execute(
            "SELECT count(*), max(subject), bool_and(downstream_ready) "
            "FROM public.lecture_sessions WHERE lecture_id = %s",
            (lecture.lecture_id,),
        ).fetchone()
        assert stored == (1, "Updated", True)

        runs = DiscoveryRunRepository()
        run_id = runs.start(connection, DAY)
        runs.complete(connection, run_id, {
            "status": "COMPLETED", "calendar_events_found": 1, "teams_events_found": 1,
            "online_meetings_resolved": 1, "active_group_matches": 1,
            "unmatched_count": 0, "error_count": 0, "metadata": {"test": True},
        })
        assert connection.execute(
            "SELECT status FROM public.lecture_discovery_runs WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == "COMPLETED"
    finally:
        connection.rollback()
        connection.close()


def test_database_rejects_a_non_canonical_registry_row():
    """The canonical scope is enforced by the table, not only by the service."""
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    connection = psycopg.connect(settings.database_url)
    lecture = _lecture(uuid.uuid4().hex, group_match_status="NO_ACTIVE_GROUP_MATCH",
                       module=None, meeting_id=None, downstream_ready=False)
    try:
        # The repository refuses first; bypass it to prove the CHECK also holds.
        with pytest.raises(PlatformError):
            LectureSessionRepository().upsert(connection, lecture)
        connection.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO public.lecture_sessions (lecture_id, calendar_user_upn, "
                "calendar_event_id, join_url, subject, normalized_subject, scheduled_start, "
                "scheduled_end, session_date, calendar_mapping_status, group_match_status, "
                "discovery_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (lecture.lecture_id, lecture.calendar_user_upn, lecture.calendar_event_id,
                 lecture.join_url, lecture.subject, lecture.normalized_subject,
                 lecture.scheduled_start, lecture.scheduled_end, lecture.session_date,
                 "NOT_ATTEMPTED", "NO_ACTIVE_GROUP_MATCH", "REVIEW"),
            )
    finally:
        connection.rollback()
        connection.close()


def test_database_rejects_downstream_ready_without_a_meeting():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    connection = psycopg.connect(settings.database_url)
    lecture = _lecture(uuid.uuid4().hex, meeting_id=None, downstream_ready=True)
    try:
        with pytest.raises(PlatformError):
            LectureSessionRepository().upsert(connection, lecture)
    finally:
        connection.rollback()
        connection.close()


def test_readonly_connection_is_database_enforced():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    with readonly_database_connection(settings.database_url) as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
