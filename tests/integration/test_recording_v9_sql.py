"""
Recording links v9: the two SQL statements from the export, run verbatim
against the isolated test database.

Self-contained: every row is seeded here and the transaction is rolled back.
The statements are executed exactly as n8n sends them ($n placeholders), via
PREPARE / EXECUTE, so the SQL under test is the SQL in the export.
"""
from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from tests.integration.seeding import insert, seed_lecture, seed_legacy_row
from tools import recording_v9 as v9


EXACT = dict(status="organization_view_link_created_exact_match", method=v9.MATCH_METHOD)
QA_SNAPSHOT = ("met_count", "strengths", "areas_for_development", "overall_judgement",
               "teaching_quality_rating", "cancelled_session", "trainer", "subject")


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert "test" in name, "refusing: not the isolated test database"
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _sql(name):
    return v9.node(v9.load_v9(), name)["parameters"]["query"]


def execute(connection, node_name, params):
    statement = f"v9_{uuid.uuid4().hex[:12]}"
    connection.execute(f"PREPARE {statement} AS {_sql(node_name)}")
    try:
        # Client-side literals, as n8n's pg-promise sends them: an untyped
        # literal takes the prepared parameter's type.
        placeholders = ", ".join(["%s"] * len(params))
        cursor = psycopg.ClientCursor(connection)
        cursor.execute(f"EXECUTE {statement}({placeholders})", params)
        columns = [d.name for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.execute(f"DEALLOCATE {statement}")


def update(connection, session_id, meeting_id, *, dry_run="false", url="https://t/new",
           status=EXACT["status"], method=EXACT["method"], lecture_key=""):
    [row] = execute(connection, v9.UPDATE_NODE, [
        session_id, url, "item-1", "drive-1", "Lecture-20260911_075648UTC-Meeting Recording.mp4",
        status, lecture_key, meeting_id, method, dry_run])
    return row


def session(connection, session_id):
    cursor = connection.execute(
        "SELECT * FROM public.qa_doctors_sessions WHERE session_id = %s", (session_id,))
    columns = [d.name for d in cursor.description]
    return dict(zip(columns, cursor.fetchone()))


def seed(connection, *, recording_url=None, day=date(2026, 9, 11), cancelled="false"):
    lecture = seed_lecture(connection, session_date=day)
    session_id = f"sess-{uuid.uuid4().hex}"
    seed_legacy_row(connection, session_id, meeting_id=lecture["meeting_id"], day=day,
                    recording_url=recording_url, cancelled_session=cancelled,
                    strengths=["kept"], overall_judgement="kept")
    return session_id, lecture["meeting_id"]


def test_target_query_selects_only_empty_urls_in_range_with_the_organizer(db):
    empty, meeting = seed(db)
    filled, _ = seed(db, recording_url="https://t/existing")
    outside, _ = seed(db, day=date(2026, 8, 31))
    rows = execute(db, "Get Target Lectures", [500, "2026-09-01", "2026-09-30"])
    ids = {r["session_id"] for r in rows}
    assert empty in ids and filled not in ids and outside not in ids
    [row] = [r for r in rows if r["session_id"] == empty]
    assert row["meeting_lookup_user_id"] == "organizer"
    assert row["organizer_count"] == 1
    assert row["lecture_date"] == "2026-09-11"


def test_an_exact_match_writes_only_recording_columns(db):
    session_id, meeting = seed(db)
    before = session(db, session_id)
    result = update(db, session_id, meeting)
    assert result["session_updated"] is True
    after = session(db, session_id)
    assert after["recording_url"] == "https://t/new"
    assert after["recording_link_status"] == EXACT["status"]
    assert after["recording_link_updated_at"] is not None
    for column in QA_SNAPSHOT:
        assert after[column] == before[column], column


def test_an_existing_recording_url_is_never_overwritten(db):
    session_id, meeting = seed(db, recording_url="https://t/existing")
    result = update(db, session_id, meeting)
    assert result["session_updated"] is False
    assert session(db, session_id)["recording_url"] == "https://t/existing"


@pytest.mark.parametrize("overrides", [
    {"dry_run": "true"}, {"dry_run": ""},
    {"method": "exact_call_id_subject_and_timestamp_v8"},
    {"status": "AMBIGUOUS_RECORDING_FILES"},
    {"status": "create_link_failed_no_update"},
    {"url": ""},
])
def test_the_database_refuses_anything_but_an_armed_exact_match(db, overrides):
    session_id, meeting = seed(db)
    result = update(db, session_id, meeting, **overrides)
    assert result["write_eligible"] is False and result["session_updated"] is False
    assert session(db, session_id)["recording_url"] is None


def test_a_mismatched_meeting_id_does_not_update(db):
    session_id, _ = seed(db)
    assert update(db, session_id, "some-other-meeting")["session_updated"] is False


def test_perfect_lecture_update_is_limited_and_follows_the_session(db):
    session_id, meeting = seed(db)
    key = f"2026-09-11|{uuid.uuid4().hex}"
    insert(db, "qa_perfect_lectures", lecture_key=key, session_date=date(2026, 9, 11),
           subject="Synthetic", met_count=11, engagement=7, attended_count=5,
           session_id=session_id, meeting_id=None, recording_url=None)
    result = update(db, session_id, meeting, lecture_key=key)
    assert result["perfect_updated"] is True
    row = db.execute("SELECT recording_url, meeting_id, session_id, met_count, engagement, "
                     "attended_count FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                     (key,)).fetchone()
    assert row[:3] == ("https://t/new", meeting, session_id)
    assert (row[3], float(row[4]), row[5]) == (11, 7.0, 5)
