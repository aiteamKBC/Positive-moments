"""
Phase 3C3D integration tests, against the real 2026-09-17 pilot rows.

Three properties are being pinned:

  * attendance source coverage is derivable from persisted evidence alone, for
    every lecture, without reading the external attendance table;
  * the evaluation's attempt aggregate agrees with the authoritative
    append-only attempts table;
  * the attendance-aware Perfect policy blocks a legacy write that v1 allowed,
    without altering anything v1 already wrote.

Every test is read-only or runs inside a transaction that is deliberately
rolled back. Nothing here deletes, rewrites or supersedes a committed pilot
row: the pilot data IS the fixture, and destroying it would destroy the
evidence these tests exist to protect.
"""
from datetime import date

import psycopg
import pytest

from app.attendance.coverage import (
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    classify,
    confirms_zero_attendance,
    is_authoritative,
)
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.config.settings import Settings
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_shadow import QaEvaluationRepository
from app.db.repositories.qa_writer import (
    GenerationAttemptRepository,
    RenderedPayloadRepository,
)
from app.qa.perfect import (
    ELIGIBLE,
    PENDING_ATTENDANCE_DATA,
    PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION_V2,
    apply_attendance_policy,
    evaluate,
)
from app.qa.recovery import recovery_state
from app.qa.structured_output import JSON_OBJECT, STRICT_JSON_SCHEMA
from app.rendering.service import RENDERER_VERSION
from app.writer.modes import (
    DRY_RUN,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PERFECT_WOULD_SKIP_IDENTICAL,
)
from app.writer.perfect_service import PerfectLecturePlanner

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


TARGET_DATE = date(2026, 9, 17)
G2_KEITH = "de8c6c60-6e73-5b50-b74d-ef5806b9d1bb"
STEPHEN = "dd5b591e-d5af-5682-9c20-4778ae095c7f"
MARTECH = "9671a3f1-3b46-5b18-a585-74b90964aa45"
MARTECH_KEY = "2026-09-17|Martech - Thur"


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _coverage(connection, lecture_id):
    return AttendanceCoverageRepository().for_lecture(
        connection, lecture_id,
        attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION)


def _rendered(connection, lecture_id):
    repository = RenderedPayloadRepository()
    rows = [row for row in repository.load_sessions(connection, TARGET_DATE,
                                                    RENDERER_VERSION)
            if str(row["lecture_id"]) == lecture_id]
    if not rows:
        pytest.skip(f"no rendered session for {lecture_id}")
    return rows[0], repository.load_items(connection, rows[0]["rendered_session_id"])


# --- attendance source coverage ---------------------------------------------

def test_coverage_is_readable_for_every_lecture_on_the_pilot_day():
    with _connection() as connection:
        rows = connection.execute(
            "SELECT lecture_id FROM public.lecture_sessions WHERE session_date = %s",
            (TARGET_DATE,)).fetchall()
        assert len(rows) == 3
        for (lecture_id,) in rows:
            coverage = _coverage(connection, lecture_id)
            assert coverage["attendance_coverage_status"] is not None
            assert coverage["attendance_coverage_version"] == "attendance_coverage_v1"
        connection.rollback()


def test_the_lecture_with_source_rows_reports_members():
    """Stephen: four present rows, four surviving members."""
    with _connection() as connection:
        coverage = _coverage(connection, STEPHEN)
        assert coverage["attendance_coverage_status"] == SOURCE_AVAILABLE_WITH_MEMBERS
        assert coverage["attendance_source_authoritative"] is True
        assert coverage["attendance_effective_member_count"] == 4
        connection.rollback()


@pytest.mark.parametrize("lecture_id", [G2_KEITH, MARTECH])
def test_the_lectures_with_no_source_rows_report_missing_not_zero(lecture_id):
    with _connection() as connection:
        coverage = _coverage(connection, lecture_id)
        assert coverage["attendance_coverage_status"] == SOURCE_MISSING
        assert coverage["attendance_source_authoritative"] is False
        assert coverage["attendance_source_row_count"] == 0
        connection.rollback()


def test_a_zero_attended_count_is_not_read_as_confirmed_zero():
    """
    The ambiguity, on the real rows: both lectures persisted attended_count 0,
    and neither of those zeroes is evidence.
    """
    with _connection() as connection:
        for lecture_id in (G2_KEITH, MARTECH):
            attended = connection.execute(
                "SELECT attended_count FROM public.lecture_engagement_metrics "
                " WHERE lecture_id = %s ORDER BY updated_at DESC LIMIT 1",
                (lecture_id,)).fetchone()[0]
            status = _coverage(connection, lecture_id)["attendance_coverage_status"]
            assert attended == 0
            assert confirms_zero_attendance(status, attended) is False
        connection.rollback()


def test_coverage_derives_from_frozen_snapshot_counts_not_the_external_table():
    """
    The derivation must reproduce itself from the snapshot columns alone, so a
    later change to `public.kbc_attendance` cannot retroactively rewrite what
    an old run observed.
    """
    with _connection() as connection:
        for lecture_id in (G2_KEITH, STEPHEN, MARTECH):
            row = connection.execute("""
                SELECT source_row_count, present_row_count, effective_member_count
                  FROM public.lecture_attendance_snapshots
                 WHERE lecture_id = %s ORDER BY created_at DESC LIMIT 1""",
                (lecture_id,)).fetchone()
            assert _coverage(connection, lecture_id)["attendance_coverage_status"] == \
                classify(source_row_count=row[0], present_row_count=row[1],
                         effective_member_count=row[2])
        connection.rollback()


# --- provider attempt accounting --------------------------------------------

def test_the_authoritative_attempts_table_still_holds_the_three_old_attempts():
    """D3: history is repaired by re-deriving an aggregate, never by deleting."""
    with _connection() as connection:
        state = recovery_state(connection, G2_KEITH)
        old = [item for item in state["evaluations"]
               if item["provider_contract_version"] == JSON_OBJECT]
        assert old, "the pre-3C3D evaluation must still exist"
        assert old[0]["attempts_used"] == 3
        assert old[0]["attempt_outcomes"] == ["INVALID_STRUCTURED_OUTPUT"] * 3
        assert old[0]["attempts_remaining"] == 0
        connection.rollback()


def test_recovery_reports_attempts_remaining_per_generation_contract():
    with _connection() as connection:
        state = recovery_state(connection, G2_KEITH)
        for evaluation in state["evaluations"]:
            assert evaluation["attempts_remaining"] == max(
                evaluation["max_model_generations"] - evaluation["attempts_used"], 0)
            # Every contract seen is a named one, never an inferred blank.
            assert evaluation["provider_contract_version"] in (JSON_OBJECT,
                                                               STRICT_JSON_SCHEMA)
        connection.rollback()


def test_reconciling_the_aggregate_matches_the_attempts_table():
    """Rolled back: the assertion is that the repair is correct, not that it ran."""
    with _connection() as connection:
        try:
            with connection.transaction():
                before = recovery_state(connection, G2_KEITH)
                for evaluation in before["evaluations"]:
                    QaEvaluationRepository().sync_provider_attempts(
                        connection, evaluation["source_fingerprint"])
                after = recovery_state(connection, G2_KEITH)
                assert after["attempt_accounting_consistent"] is True
                for evaluation in after["evaluations"]:
                    assert (evaluation["provider_attempts_stored"]
                            == evaluation["attempts_used"])
                raise _Rollback
        except _Rollback:
            pass


def test_reconciling_twice_is_a_no_op_and_never_inflates_the_count():
    with _connection() as connection:
        try:
            with connection.transaction():
                repository = QaEvaluationRepository()
                fingerprints = [item["source_fingerprint"]
                                for item in recovery_state(connection, G2_KEITH)["evaluations"]]
                for fingerprint in fingerprints:
                    repository.sync_provider_attempts(connection, fingerprint)
                first = recovery_state(connection, G2_KEITH)
                for fingerprint in fingerprints:
                    # Already equal: the statement matches no row at all.
                    assert repository.sync_provider_attempts(connection, fingerprint) is None
                assert recovery_state(connection, G2_KEITH)["evaluations"] == \
                    first["evaluations"]
                raise _Rollback
        except _Rollback:
            pass


def test_reconciling_never_touches_an_attempt_row():
    with _connection() as connection:
        try:
            with connection.transaction():
                def attempts():
                    return connection.execute(
                        "SELECT attempt_id, generation_number, outcome, created_at "
                        "  FROM public.lecture_qa_generation_attempts "
                        " WHERE lecture_id = %s ORDER BY generation_number",
                        (G2_KEITH,)).fetchall()

                before = attempts()
                for evaluation in recovery_state(connection, G2_KEITH)["evaluations"]:
                    QaEvaluationRepository().sync_provider_attempts(
                        connection, evaluation["source_fingerprint"])
                assert attempts() == before
                raise _Rollback
        except _Rollback:
            pass


def test_a_completed_lecture_already_agrees_or_is_repairable_the_same_way():
    with _connection() as connection:
        state = recovery_state(connection, STEPHEN)
        evaluation = state["current_evaluation"]
        assert evaluation["qa_status"] == "COMPLETED"
        assert evaluation["attempts_used"] == 1
        connection.rollback()


# --- the Perfect policy, on the real rows ------------------------------------

def _planner(connection, version, **overrides):
    return PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(),
        coverage_repository=AttendanceCoverageRepository(),
        mode=DRY_RUN, eligibility_version=version, **overrides)


def test_v1_still_reproduces_the_written_martech_answer_exactly():
    """19: the historical policy must remain reproducible, forever."""
    with _connection() as connection:
        rendered, items = _rendered(connection, MARTECH)
        facts = evaluate(rendered, items)
        assert (facts["is_perfect"], facts["reason"]) == (True, ELIGIBLE)
        stored = connection.execute(
            "SELECT is_perfect, reason FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s AND eligibility_version = %s",
            (MARTECH, PERFECT_ELIGIBILITY_VERSION)).fetchone()
        assert stored == (True, ELIGIBLE)
        connection.rollback()


def test_v2_would_have_held_martech_back_for_missing_attendance():
    """17: 11/11 with no attendance evidence is pending, not perfect."""
    with _connection() as connection:
        rendered, items = _rendered(connection, MARTECH)
        facts = apply_attendance_policy(
            evaluate(rendered, items),
            coverage_status=_coverage(connection, MARTECH)["attendance_coverage_status"],
            eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
        assert facts["reason"] == PENDING_ATTENDANCE_DATA
        assert facts["base_reason"] == ELIGIBLE
        connection.rollback()


def test_the_v2_planner_proposes_no_legacy_write_for_martech():
    with _connection() as connection:
        rendered, items = _rendered(connection, MARTECH)
        plan = _planner(connection, PERFECT_ELIGIBILITY_VERSION_V2).plan_one(
            connection, rendered, items)
        assert plan["perfect_decision"] == PERFECT_PENDING_ATTENDANCE_DATA
        assert plan["attendance_pending"] is True
        # Crucially NOT a supersede: the existing owned row is untouched.
        assert plan.get("perfect_write_status") is None
        connection.rollback()


def test_the_v1_planner_still_sees_martech_as_an_identical_no_op():
    """20: the historical row stays owned, correct and unrewritten."""
    with _connection() as connection:
        rendered, items = _rendered(connection, MARTECH)
        plan = _planner(connection, PERFECT_ELIGIBILITY_VERSION).plan_one(
            connection, rendered, items)
        assert plan["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
        connection.rollback()


def test_martechs_legacy_row_and_ownership_are_byte_identical_after_planning():
    with _connection() as connection:
        def snapshot():
            row = connection.execute(
                'SELECT lecture_key, session_date, subject, module, trainer, '
                '       engagement, attended_count, met_count, recording_url, '
                '       meeting_id, session_id, recap_url, excel_synced_at, id '
                '  FROM public.qa_perfect_lectures WHERE lecture_key = %s',
                (MARTECH_KEY,)).fetchone()
            owner = connection.execute(
                "SELECT write_status, eligibility_version, post_write_digest, written_at "
                "  FROM public.lecture_perfect_lecture_legacy_writes "
                " WHERE legacy_lecture_key = %s", (MARTECH_KEY,)).fetchone()
            return row, owner

        before = snapshot()
        assert before[0] is not None and before[1] is not None
        rendered, items = _rendered(connection, MARTECH)
        for version in (PERFECT_ELIGIBILITY_VERSION, PERFECT_ELIGIBILITY_VERSION_V2):
            _planner(connection, version).plan_one(connection, rendered, items)
        assert snapshot() == before
        connection.rollback()


def test_a_non_perfect_lecture_is_not_pending_even_with_missing_attendance():
    """18: waiting is only for an answer that would otherwise be yes."""
    with _connection() as connection:
        rendered, items = _rendered(connection, STEPHEN)
        facts = apply_attendance_policy(
            evaluate(rendered, items),
            coverage_status=SOURCE_MISSING,
            eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
        assert facts["reason"] == "NOT_ELIGIBLE_STATUS_NOT_ALL_MET"
        assert facts["attendance_pending"] is False
        connection.rollback()


# --- engagement exposes the coverage state ----------------------------------

def _engagement_service():
    from app.attendance.resolver import RESOLVER_VERSION
    from app.attendance.roles import ROLE_ALGORITHM_VERSION
    from app.db.repositories.engagement import (
        EngagementInputRepository, EngagementRepository, EngagementRunRepository)
    from app.engagement.service import EngagementService
    return EngagementService(
        input_repository=EngagementInputRepository(),
        engagement_repository=EngagementRepository(),
        run_repository=EngagementRunRepository(),
        legacy_parity_repository=None,
        attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
        resolver_version=RESOLVER_VERSION,
        role_algorithm_version=ROLE_ALGORITHM_VERSION)


@pytest.mark.parametrize("lecture_id,expected", [
    (STEPHEN, SOURCE_AVAILABLE_WITH_MEMBERS),
    (G2_KEITH, SOURCE_MISSING),
    (MARTECH, SOURCE_MISSING),
])
def test_engagement_reports_the_coverage_state_it_calculated_under(lecture_id, expected):
    """persist=False: the calculation runs, nothing is written."""
    with _connection() as connection:
        summary = _engagement_service().calculate_lecture(
            connection, lecture_id, persist=False)
        row = summary["lectures"][0]
        assert row["attendance_coverage_status"] == expected
        assert row["attendance_source_authoritative"] == is_authoritative(expected)
        connection.rollback()


def test_engagement_distinguishes_a_data_gap_from_a_finding():
    """
    Both lectures produce `NO_ATTENDED_LEARNERS`. Only the detail says which
    of them is evidence, and neither claims a confirmed zero.
    """
    with _connection() as connection:
        service = _engagement_service()
        gap = service.calculate_lecture(connection, G2_KEITH,
                                        persist=False)["lectures"][0]
        real = service.calculate_lecture(connection, STEPHEN,
                                         persist=False)["lectures"][0]
        assert gap["calculation_status"] == "NO_ATTENDED_LEARNERS"
        assert gap["engagement_status_detail"] == "ATTENDANCE_SOURCE_MISSING"
        assert gap["attendance_zero_confirmed"] is False
        assert real["engagement_status_detail"] == "CALCULATED"
        connection.rollback()


def test_the_coverage_state_is_persisted_where_operations_can_read_it():
    """The same fields go into the row's metadata, not only the summary."""
    with _connection() as connection:
        try:
            with connection.transaction():
                _engagement_service().calculate_lecture(connection, G2_KEITH,
                                                        persist=True)
                metadata = connection.execute(
                    "SELECT metadata FROM public.lecture_engagement_metrics "
                    " WHERE lecture_id = %s ORDER BY updated_at DESC LIMIT 1",
                    (G2_KEITH,)).fetchone()[0]
                assert metadata["attendance_coverage_status"] == SOURCE_MISSING
                assert metadata["attendance_source_authoritative"] is False
                assert metadata["engagement_status_detail"] == "ATTENDANCE_SOURCE_MISSING"
                assert metadata["attendance_zero_confirmed"] is False
                raise _Rollback
        except _Rollback:
            pass


def test_calculating_engagement_dry_writes_nothing():
    with _connection() as connection:
        before = connection.execute(
            "SELECT count(*), max(updated_at) FROM public.lecture_engagement_metrics"
        ).fetchone()
        for lecture_id in (G2_KEITH, STEPHEN, MARTECH):
            _engagement_service().calculate_lecture(connection, lecture_id,
                                                    persist=False)
        assert connection.execute(
            "SELECT count(*), max(updated_at) FROM public.lecture_engagement_metrics"
        ).fetchone() == before
        connection.rollback()


# --- recovery state ----------------------------------------------------------

def test_recovery_state_answers_every_question_a_resume_needs():
    with _connection() as connection:
        state = recovery_state(connection, G2_KEITH)
        for field in ("attendance_coverage_status", "engagement", "current_evaluation",
                      "rendered", "perfect_results", "perfect_legacy_writes",
                      "resume_stage", "generation_contracts_seen"):
            assert field in state
        assert state["attendance_coverage_status"] == SOURCE_MISSING
        connection.rollback()


def test_recovery_state_is_read_only():
    with _connection() as connection:
        def counts():
            return [connection.execute("SELECT count(*) FROM public." + table).fetchone()[0]
                    for table in ("lecture_qa_evaluations",
                                  "lecture_qa_generation_attempts",
                                  "lecture_perfect_lecture_results",
                                  "qa_perfect_lectures", "qa_doctors_sessions")]

        before = counts()
        for lecture_id in (G2_KEITH, STEPHEN, MARTECH):
            recovery_state(connection, lecture_id)
        assert counts() == before
        connection.rollback()
