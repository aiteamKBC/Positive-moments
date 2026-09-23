"""
Phase 3C3B integration tests: engagement is lecture-scoped.

The controlled-day pilot exposed the defect these pin down - recalculating one
lecture re-upserted every sibling on the same date with identical values.
Harmless in content, wrong for a scheduler and for recovery, because it makes
"what did this run touch?" unanswerable.

Every test either asserts a read-only fact or works inside a transaction that
is deliberately rolled back.
"""
from datetime import date

import psycopg
import pytest

from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.config.settings import Settings
from app.db.repositories.engagement import (
    EngagementInputRepository, EngagementRepository, EngagementRunRepository)
from app.engagement.service import EngagementScopeError, EngagementService

# ---------------------------------------------------------------------------
# PRODUCTION-DATA ACCEPTANCE SUITE
#
# Every test in this module asserts behaviour against KBC's real historical
# evidence: named lectures, real session dates, real transcripts, the real
# legacy dataset. It is NOT part of the RC release gate and is deselected by
#     pytest tests/integration -m "not production_data"
# because on a database without that evidence it can only fail or pass
# vacuously - neither of which validates anything.
#
# The contracts in here that never needed real history have been moved to the
# self-contained gate modules (test_pipeline_contracts.py,
# test_platform_invariants.py, test_safety_fixes_integration.py).
#
# To run this suite, an approved acceptance dataset must be configured - never
# production. See docs/audits/QA_CORE_RC4_TEST_GATE_FINAL_2026-09-22.md.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.production_data


TARGET_DATE = date(2026, 9, 16)
STEVE = "eaccf843-845f-586e-b285-3ef181cd0c85"
ANDREW = "25e85615-aa7a-5f49-bb40-078d7c7b65d0"


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _service():
    return EngagementService(
        input_repository=EngagementInputRepository(),
        engagement_repository=EngagementRepository(),
        run_repository=EngagementRunRepository(),
        legacy_parity_repository=None,
        attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
        resolver_version=RESOLVER_VERSION,
        role_algorithm_version=ROLE_ALGORITHM_VERSION)


def _same_day_rows(connection):
    return {(str(r[0]), str(r[1])): r[2] for r in connection.execute("""
        SELECT m.lecture_id, m.engagement_id, m.updated_at
          FROM public.lecture_engagement_metrics m
          JOIN public.lecture_sessions l ON l.lecture_id = m.lecture_id
         WHERE l.session_date = %s""", (TARGET_DATE,)).fetchall()}


# --------------------------------------------------------------------------
# 1-3: the scope itself
# --------------------------------------------------------------------------

def test_a_lecture_scoped_run_selects_exactly_one_lecture():
    with _connection() as connection:
        try:
            with connection.transaction():
                summary = _service().calculate_lecture(connection, STEVE, persist=True)
                assert summary["scope"] == "LECTURE"
                assert summary["scope_value"] == STEVE
                assert summary["lectures_in_scope"] == 1
                assert {row["lecture_id"] for row in summary["lectures"]} == {STEVE}
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_same_day_siblings_are_not_touched():
    """The actual defect: no sibling may receive a write or a new updated_at."""
    with _connection() as connection:
        before = _same_day_rows(connection)
        assert len(before) > 1, "this test needs siblings to be meaningful"
        try:
            with connection.transaction():
                _service().calculate_lecture(connection, STEVE, persist=True)
                after = _same_day_rows(connection)
                assert set(after) == set(before), "no row may appear or vanish"
                moved = [key for key, stamp in after.items() if before[key] != stamp]
                assert all(key[0] == STEVE for key in moved), moved
                assert len(moved) <= 1
                raise _Rollback
        except _Rollback:
            pass
        assert _same_day_rows(connection) == before
        connection.rollback()


def test_the_expected_snapshot_and_versions_are_selected():
    with _connection() as connection:
        summary = _service().calculate_lecture(connection, STEVE, persist=False)
        assert summary["attendance_resolution_version"] == ATTENDANCE_RESOLUTION_VERSION
        assert summary["resolver_version"] == RESOLVER_VERSION
        assert summary["role_algorithm_version"] == ROLE_ALGORITHM_VERSION
        row = summary["lectures"][0]
        current, = connection.execute("""
            SELECT snapshot_id FROM public.lecture_attendance_snapshots
             WHERE lecture_id = %s AND attendance_resolution_version = %s
             ORDER BY created_at DESC LIMIT 1""",
            (STEVE, ATTENDANCE_RESOLUTION_VERSION)).fetchone()
        assert row["attendance_snapshot_id"] == str(current)
        assert row["is_current_snapshot"] is True
        connection.rollback()


# --------------------------------------------------------------------------
# 4-5: refusals
# --------------------------------------------------------------------------


def test_a_lecture_without_attendance_evidence_is_refused_not_silently_skipped():
    with _connection() as connection:
        missing, = connection.execute("""
            SELECT l.lecture_id FROM public.lecture_sessions l
             WHERE NOT EXISTS (SELECT 1 FROM public.lecture_attendance_snapshots s
                                WHERE s.lecture_id = l.lecture_id)
             LIMIT 1""").fetchone() or (None,)
        if missing is None:
            pytest.skip("every lecture currently has an attendance snapshot")
        with pytest.raises(EngagementScopeError):
            _service().calculate_lecture(connection, missing, persist=False)
        connection.rollback()


# --------------------------------------------------------------------------
# 6-7: idempotency and scope isolation
# --------------------------------------------------------------------------

def test_a_rerun_on_unchanged_evidence_reuses_the_same_row():
    with _connection() as connection:
        first = _service().calculate_lecture(connection, STEVE, persist=False)
        second = _service().calculate_lecture(connection, STEVE, persist=False)
        assert first["lectures"][0]["engagement_id"] == second["lectures"][0]["engagement_id"]
        assert (first["lectures"][0]["source_fingerprint_prefix"]
                == second["lectures"][0]["source_fingerprint_prefix"])
        for field in ("attended_count", "spoke_count", "engagement_percentage",
                      "engagement_score", "calculation_status"):
            assert first["lectures"][0][field] == second["lectures"][0][field]
        connection.rollback()


def test_lecture_mode_cannot_widen_to_the_date():
    """
    The scope is a separate SQL statement with no session_date predicate, so
    there is no argument to leave unset that would silently widen it.
    """
    from app.db.repositories.engagement import (LOAD_SNAPSHOTS,
                                                LOAD_SNAPSHOTS_FOR_LECTURE)
    assert "session_date" in LOAD_SNAPSHOTS
    assert "session_date" not in LOAD_SNAPSHOTS_FOR_LECTURE
    assert "sn.lecture_id = %s" in LOAD_SNAPSHOTS_FOR_LECTURE

    with _connection() as connection:
        rows = EngagementInputRepository().load_snapshots_for_lecture(
            connection, ANDREW,
            attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
            resolver_version=RESOLVER_VERSION,
            role_algorithm_version=ROLE_ALGORITHM_VERSION)
        assert rows
        assert {str(row["lecture_id"]) for row in rows} == {ANDREW}
        connection.rollback()
