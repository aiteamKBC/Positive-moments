"""
The 2026-11-20 nightly failure, on real PostgreSQL with a real postgres_fdw server.

Both scheduler cycles that night ran ~10 minutes and then failed at COMMIT with

    terminating connection due to idle-in-transaction timeout
    CONTEXT: remote SQL command: COMMIT TRANSACTION

postgres_fdw keeps its REMOTE transaction open until the LOCAL top-level transaction ends (a
savepoint does not end it). Attendance read the lms_sync foreign tables on the cycle's own
connection, so the LMS session sat idle in transaction until the cycle's single COMMIT; once
that was longer than the LMS server's idle_in_transaction_session_timeout (5 min on Neon) the
server ended the session and the COMMIT - the whole night - failed.

Here the "LMS" is a loopback foreign server onto this same test database whose sessions time
out after 1.5 s of idle transaction. Everything this module creates (an lms_sync schema, a
source schema, the server) is dropped at the end; the extension too, if this module created it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg import sql

from app.attendance import lms_source as lms
from app.config.settings import Settings
from app.lms import matching
from tools.integration_db_guard import assert_isolated

SERVER = "lms_idle_timeout_test"
SOURCE_SCHEMA = "lms_idle_timeout_src"
REMOTE_IDLE_MS = 1500
IDLE_SECONDS = 3.0
START = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def _url():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    return url


@pytest.fixture
def lms_server():
    """A loopback 'LMS' with lms_sync foreign tables and a short remote idle timeout."""
    admin = psycopg.connect(_url(), autocommit=True)
    assert_isolated(admin)
    if admin.execute("SELECT to_regnamespace('lms_sync')").fetchone()[0] is not None:
        admin.close()
        pytest.skip("this database already has an lms_sync schema; not touching it")
    created_extension = admin.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgres_fdw')").fetchone()[0]
    try:
        admin.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(f"postgres_fdw unavailable on the test database: {type(exc).__name__}")
    port = admin.execute("SHOW port").fetchone()[0]
    database = admin.execute("SELECT current_database()").fetchone()[0]
    try:
        admin.execute(f"CREATE SCHEMA {SOURCE_SCHEMA}")
        admin.execute(f"CREATE TABLE {SOURCE_SCHEMA}.live_session_occurrences (id text, status text)")
        admin.execute(f"""CREATE TABLE {SOURCE_SCHEMA}.live_session_learner_attendance (
            occurrence_id text, learner_profile_id int, learner_name text, learner_email text,
            module_catalogue_id text, attendance_status text)""")
        admin.execute(f"INSERT INTO {SOURCE_SCHEMA}.live_session_occurrences VALUES ('O1', 'completed')")
        admin.execute(f"""INSERT INTO {SOURCE_SCHEMA}.live_session_learner_attendance VALUES
            ('O1', 1, 'Learner One', 'one@example.invalid', 'M1', 'present'),
            ('O1', 2, 'Learner Two', 'two@example.invalid', 'M1', 'absent')""")
        admin.execute(sql.SQL(
            "CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw OPTIONS "
            "(host 'localhost', port {}, dbname {}, options {})").format(
                sql.Identifier(SERVER), sql.Literal(str(port)),
                sql.Literal(database),
                sql.Literal(f"-c idle_in_transaction_session_timeout={REMOTE_IDLE_MS}")))
        info = admin.info
        admin.execute(sql.SQL(
            "CREATE USER MAPPING FOR CURRENT_USER SERVER {} OPTIONS (user {}, password {})").format(
                sql.Identifier(SERVER), sql.Literal(info.user),
                sql.Literal(info.password or "")))
        admin.execute("CREATE SCHEMA lms_sync")
        admin.execute(f"IMPORT FOREIGN SCHEMA {SOURCE_SCHEMA} FROM SERVER {SERVER} INTO lms_sync")
        yield
    finally:
        admin.execute("DROP SCHEMA IF EXISTS lms_sync CASCADE")
        admin.execute(f"DROP SERVER IF EXISTS {SERVER} CASCADE")
        admin.execute(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE")
        if created_extension:
            admin.execute("DROP EXTENSION IF EXISTS postgres_fdw")
        admin.close()


@pytest.fixture
def cycle():
    """The scheduler cycle's connection: one transaction, committed once at the end."""
    connection = psycopg.connect(_url())
    assert_isolated(connection)
    # A real cycle has long been inside its transaction when attendance runs (its run row is
    # already written). Without this, psycopg's transaction() block in the gateway would BE the
    # outermost transaction and commit on exit - hiding exactly the failure under test.
    connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def verified_match(monkeypatch):
    """The lecture's saved VERIFIED match (a local-table read; not what is under test)."""
    link = matching.OccurrenceLink("O1", "S1", "M1", 1, START, "PRIMARY", None)
    monkeypatch.setattr(lms.LmsAttendanceSource, "_persisted_match", staticmethod(
        lambda connection, lecture_id: matching.MatchResult(
            lecture_id=lecture_id, status=matching.VERIFIED, links=(link,),
            evidence={"source": "persisted_evaluation"})))


def _remote_sessions(connection):
    return connection.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'postgres_fdw' "
        "AND state = 'idle in transaction'").fetchone()[0]


def test_reproduction_a_foreign_read_on_the_cycle_connection_loses_the_cycle_at_commit(
        lms_server, cycle, verified_match):
    decision = lms.LmsAttendanceSource().decide(cycle, "L1")          # the 2026-11-20 code path
    assert decision.used and decision.source_rows_present == 1
    time.sleep(IDLE_SECONDS)                                          # the rest of the cycle
    with pytest.raises(psycopg.OperationalError) as failure:
        cycle.commit()
    message = str(failure.value) + str(getattr(failure.value.diag, "context", "") or "")
    assert "idle-in-transaction" in message and "COMMIT TRANSACTION" in message


def test_fixed_the_register_is_read_on_its_own_connection_and_the_cycle_commits(
        lms_server, cycle, verified_match):
    source = lms.LmsAttendanceSource(lms.register_connection_factory(_url()))
    decision = source.decide(cycle, "L1")
    assert decision.used and decision.source_rows_present == 1
    assert decision.counts == {"learners_attended": 1, "learners_absent": 1,
                               "duplicate_rows_collapsed": 0}
    assert _remote_sessions(cycle) == 0                               # nothing left open remotely
    time.sleep(IDLE_SECONDS)
    cycle.commit()                                                    # the night is kept


def test_fixed_decision_is_identical_to_the_single_connection_decision(
        lms_server, cycle, verified_match):
    split = lms.LmsAttendanceSource(lms.register_connection_factory(_url())).decide(cycle, "L1")
    single = lms.LmsAttendanceSource().decide(cycle, "L1")
    cycle.rollback()
    assert split == single
