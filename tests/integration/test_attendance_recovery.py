"""
Phase 3C3E integration tests: what happens when attendance arrives later.

G2 Keith is the real fixture. Its upstream evidence is genuine - discovery,
transcript, canonical cues, speakers, a COMPLETED Phase 3A evaluation and a
RENDERED Phase 3B payload - and its attendance source is genuinely empty. What
these tests substitute is ONLY the external source gateway, so the arrival of
attendance can be exercised end to end without writing a single row to
`public.kbc_attendance`. That table is read-only and stays read-only; the fake
never touches it, it replaces the object that would have read it.

Every test runs inside a transaction that is deliberately rolled back. The real
pilot rows are the fixture, and destroying them would destroy the evidence.
"""
from datetime import date

import psycopg
import pytest

from app.attendance.coverage import (
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    SOURCE_PARTIAL_OR_INVALID,
)
from app.attendance.recovery import (
    ATTENDANCE_SNAPSHOT_CREATED,
    NO_CHANGE,
    ATTENDANCE_SOURCE_RECOVERED,
    ENGAGEMENT_UPDATED,
    PERFECT_BECAME_ELIGIBLE,
    PERFECT_LEGACY_SYNC_AVAILABLE,
    PERFECT_REMAINS_NOT_ELIGIBLE,
    RENDER_REFRESHED,
    RENDER_REUSED,
    WAIT_FOR_ATTENDANCE_SOURCE,
    AttendanceRecoveryService,
)
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.attendance.service import SpeakerResolutionService
from app.config.settings import Settings
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.db.repositories.attendance_resolution import (
    AttendanceSnapshotRepository,
    SpeakerIdentityRepository,
    SpeakerInventoryReadRepository,
    SpeakerResolutionRunRepository,
    SpeakerRoleRepository,
)
from app.db.repositories.engagement import (
    EngagementInputRepository,
    EngagementRepository,
    EngagementRunRepository,
)
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_rendering import (
    LmsSnapshotRepository,
    LmsSourceRepository,
    RenderInputRepository,
    RenderRunRepository,
    RenderedOutputRepository,
)
from app.db.repositories.qa_shadow import (
    QaEvaluationRepository,
    QaInputRepository,
    QaRunRepository,
)
from app.db.repositories.qa_writer import (
    GenerationAttemptRepository,
    RenderedPayloadRepository,
)
from app.engagement.service import EngagementService
from app.qa.perfect import (
    ELIGIBLE,
    PENDING_ATTENDANCE_DATA,
    PERFECT_ELIGIBILITY_VERSION_V2,
)
from app.qa.service import QA_DETERMINISTIC_REFRESHED, QA_REUSED, ShadowQaService
from app.rendering.service import RENDERER_VERSION, QaRenderingService
from app.writer.modes import DRY_RUN, PERFECT_PENDING_ATTENDANCE_DATA
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


G2_KEITH = "de8c6c60-6e73-5b50-b74d-ef5806b9d1bb"
TARGET_DATE = date(2026, 9, 17)

# Real speaker labels from G2 Keith's canonical document. Matching them is what
# makes the synthetic roster behave like a real one through the resolver.
SPOKE = ["Emily Budgen", "Becka Badby", "Tom Lowes", "Annie Mannion"]
SILENT = ["Priya Raman", "Jonathan Ferris", "Marta Kowalczyk", "Ola Adeyemi",
          "Sam Whitfield", "Claire Beaumont", "Dev Patel", "Noor Haddad",
          "Ruth Feeney"]

PRODUCTION_TABLES = (
    "qa_doctors_sessions", "qa_doctors_checklist_items", "qa_perfect_lectures",
    "lecture_qa_legacy_writes", "lecture_perfect_lecture_legacy_writes",
)


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


class FakeAttendanceSource:
    """
    Stands in for the external gateway, never for the external table.

    It records every call so a test can prove the recovery asked the source
    exactly once, for exactly one (date, module).
    """

    def __init__(self, names):
        self.names = list(names)
        self.calls = []

    def load_present_rows(self, connection, session_date, module_normalized):
        self.calls.append((session_date, module_normalized))
        return [{"learner_id": 9000 + index,
                 "full_name": name,
                 "email": f"{name.split()[0].lower()}@example.invalid",
                 "attendance_flag": 1, "module": module_normalized,
                 "attendance_status": "Attended"}
                for index, name in enumerate(self.names)]

    def count_rows_any_status(self, connection, session_date, module_normalized):
        return {"source_rows_any_status": len(self.names),
                "source_rows_present": len(self.names)}


def _service(attendance_source, *, with_renderer=True):
    resolution = SpeakerResolutionService(
        inventory_repository=SpeakerInventoryReadRepository(),
        attendance_repository=attendance_source,
        snapshot_repository=AttendanceSnapshotRepository(),
        identity_repository=SpeakerIdentityRepository(),
        role_repository=SpeakerRoleRepository(),
        run_repository=SpeakerResolutionRunRepository(),
        legacy_diagnostic_repository=None)
    engagement = EngagementService(
        input_repository=EngagementInputRepository(),
        engagement_repository=EngagementRepository(),
        run_repository=EngagementRunRepository(),
        legacy_parity_repository=None,
        attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
        resolver_version=RESOLVER_VERSION,
        role_algorithm_version=ROLE_ALGORITHM_VERSION)
    qa = ShadowQaService(
        input_repository=QaInputRepository(),
        evaluation_repository=QaEvaluationRepository(),
        run_repository=QaRunRepository(),
        # No provider at all: attendance-only recovery must be structurally
        # incapable of buying a generation, not merely instructed not to.
        provider=None,
        attempt_repository=GenerationAttemptRepository(),
        resolver_version=RESOLVER_VERSION,
        role_algorithm_version=ROLE_ALGORITHM_VERSION,
        engagement_algorithm_version="legacy_qa_v8_engagement_v1",
        model_name="gpt-5.2")
    planner = PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(),
        coverage_repository=AttendanceCoverageRepository(),
        mode=DRY_RUN, persist_shadow_result=True)

    def _render_for(lecture_id):
        return QaRenderingService(
            input_repository=RenderInputRepository(),
            lms_source_repository=LmsSourceRepository(),
            lms_snapshot_repository=LmsSnapshotRepository(),
            output_repository=RenderedOutputRepository(),
            run_repository=RenderRunRepository(),
            legacy_repository=None, refresh_lms=False, lecture_ids=[lecture_id])

    return AttendanceRecoveryService(
        resolution_service=resolution, engagement_service=engagement,
        qa_service=qa, coverage_repository=AttendanceCoverageRepository(),
        perfect_planner=planner,
        payload_repository=RenderedPayloadRepository(),
        renderer_version=RENDERER_VERSION,
        rendering_service_factory=_render_for if with_renderer else None)


def _production_counts(connection):
    return {table: connection.execute(
        "SELECT count(*) FROM public." + table).fetchone()[0]
        for table in PRODUCTION_TABLES}


def _perfect(connection, lecture_id, version=PERFECT_ELIGIBILITY_VERSION_V2):
    return connection.execute("""
        SELECT is_perfect, reason FROM public.lecture_perfect_lecture_results
         WHERE lecture_id = %s AND eligibility_version = %s""",
        (lecture_id, version)).fetchone()


# --- case 3: the source is still silent --------------------------------------

def test_still_missing_returns_wait_and_writes_nothing():
    """The real state of G2 Keith today, exercised against the real source."""
    with _connection() as connection:
        before = _production_counts(connection)
        rows_before = connection.execute(
            "SELECT count(*) FROM public.lecture_attendance_snapshots "
            " WHERE lecture_id = %s", (G2_KEITH,)).fetchone()[0]

        # Note: the REAL attendance source repository, not a fake.
        from app.db.repositories.attendance_resolution import AttendanceSourceRepository
        summary = _service(AttendanceSourceRepository()).recover(
            connection, G2_KEITH, persist=True)

        assert summary["status"] == WAIT_FOR_ATTENDANCE_SOURCE
        assert summary["next_action"] == WAIT_FOR_ATTENDANCE_SOURCE
        assert summary["attendance_authoritative_after"] is False
        assert sum(summary["writes"].values()) == 0
        assert summary["openai_calls"] == 0 and summary["graph_calls"] == 0
        assert _production_counts(connection) == before
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_attendance_snapshots "
            " WHERE lecture_id = %s", (G2_KEITH,)).fetchone()[0] == rows_before
        connection.rollback()


def test_recovery_is_lecture_scoped():
    with _connection() as connection:
        from app.db.repositories.attendance_resolution import AttendanceSourceRepository
        summary = _service(AttendanceSourceRepository()).recover(
            connection, G2_KEITH, persist=False)
        assert summary["scope"] == "LECTURE"
        assert summary["lectures_in_scope"] == 1
        assert summary["scope_value"] == G2_KEITH
        connection.rollback()


def test_the_source_is_asked_once_for_one_lecture_only():
    with _connection() as connection:
        source = FakeAttendanceSource([])
        _service(source).recover(connection, G2_KEITH, persist=False)
        assert len(source.calls) == 1
        assert source.calls[0][0] == TARGET_DATE
        connection.rollback()


def test_the_absence_only_source_is_reported_as_partial_not_as_missing():
    """
    Found this phase: `kbc_attendance` DOES hold a row for G2 Keith's module,
    marked absent. The roster query filters `Attendance = 1`, so that was
    indistinguishable from silence. It no longer is - and it is still not a
    confirmed zero.
    """
    with _connection() as connection:
        from app.db.repositories.attendance_resolution import AttendanceSourceRepository
        summary = _service(AttendanceSourceRepository()).recover(
            connection, G2_KEITH, persist=False)
        assert summary["attendance_source_rows_any_status"] == 1
        assert summary["attendance_source_row_count"] == 0
        assert summary["attendance_coverage_observed_now"] == SOURCE_PARTIAL_OR_INVALID
        # Non-authoritative either way, so nothing downstream moves.
        assert summary["attendance_authoritative_after"] is False
        assert summary["status"] == WAIT_FOR_ATTENDANCE_SOURCE
        connection.rollback()


# --- case 1: attendance arrives and Item 7 stays Met -------------------------

def test_attendance_arrival_recovers_the_lecture_without_calling_the_model():
    with _connection() as connection:
        try:
            with connection.transaction():
                before = _production_counts(connection)
                summary = _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)

                assert ATTENDANCE_SOURCE_RECOVERED in summary["results"]
                assert ATTENDANCE_SNAPSHOT_CREATED in summary["results"]
                assert ENGAGEMENT_UPDATED in summary["results"]
                assert summary["attendance_coverage_after"] == \
                    SOURCE_AVAILABLE_WITH_MEMBERS
                assert summary["attendance_authoritative_after"] is True

                # Everyone in the roster spoke, so the deterministic override
                # applies and agrees with the model's Met.
                assert summary["attended_count"] == 4
                assert summary["spoke_count"] == 4
                assert summary["item7_override_applied"] is True
                assert summary["qa"]["final_item7_status"] == "Met"
                assert summary["qa"]["met_count"] == 11

                # The whole point: zero provider calls.
                assert summary["openai_calls"] == 0
                assert summary["graph_calls"] == 0
                assert summary["qa"]["refresh_status"] == QA_DETERMINISTIC_REFRESHED

                # And nothing in legacy production moved.
                assert _production_counts(connection) == before
                raise _Rollback
        except _Rollback:
            pass


def test_the_refreshed_evaluation_reuses_the_frozen_model_answer():
    with _connection() as connection:
        try:
            with connection.transaction():
                summary = _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)
                origin = summary["qa"]["origin_evaluation_id"]
                new_id = summary["qa"]["evaluation_id"]
                assert origin != new_id

                origin_output, new_output = connection.execute("""
                    SELECT (SELECT ai_raw_output FROM public.lecture_qa_evaluations
                             WHERE evaluation_id = %s),
                           (SELECT ai_raw_output FROM public.lecture_qa_evaluations
                             WHERE evaluation_id = %s)""",
                    (origin, new_id)).fetchone()
                assert origin_output == new_output

                provenance = connection.execute(
                    "SELECT metadata -> 'deterministic_refresh' "
                    "  FROM public.lecture_qa_evaluations WHERE evaluation_id = %s",
                    (new_id,)).fetchone()[0]
                assert provenance["openai_reused"] is True
                assert provenance["provider_calls"] == 0
                assert provenance["origin_evaluation_id"] == origin
                assert provenance["reason"] == "ATTENDANCE_EVIDENCE_ARRIVED"
                assert provenance["deterministic_refresh_version"] == \
                    "qa_deterministic_refresh_v1"
                raise _Rollback
        except _Rollback:
            pass


def test_the_previous_source_missing_history_is_preserved():
    with _connection() as connection:
        try:
            with connection.transaction():
                old_snapshot = connection.execute("""
                    SELECT snapshot_id, source_row_count, effective_member_count
                      FROM public.lecture_attendance_snapshots
                     WHERE lecture_id = %s ORDER BY created_at DESC LIMIT 1""",
                    (G2_KEITH,)).fetchone()
                old_engagement = connection.execute("""
                    SELECT engagement_id, attended_count, calculation_status
                      FROM public.lecture_engagement_metrics
                     WHERE lecture_id = %s ORDER BY updated_at DESC LIMIT 1""",
                    (G2_KEITH,)).fetchone()

                _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)

                # Both old rows must still be exactly what they were.
                assert connection.execute("""
                    SELECT snapshot_id, source_row_count, effective_member_count
                      FROM public.lecture_attendance_snapshots WHERE snapshot_id = %s""",
                    (old_snapshot[0],)).fetchone() == old_snapshot
                assert connection.execute("""
                    SELECT engagement_id, attended_count, calculation_status
                      FROM public.lecture_engagement_metrics WHERE engagement_id = %s""",
                    (old_engagement[0],)).fetchone() == old_engagement
                # And the lecture now has both.
                assert connection.execute(
                    "SELECT count(*) FROM public.lecture_attendance_snapshots "
                    " WHERE lecture_id = %s", (G2_KEITH,)).fetchone()[0] >= 2
                raise _Rollback
        except _Rollback:
            pass


def test_engagement_recalculates_one_lecture_and_no_sibling():
    with _connection() as connection:
        try:
            with connection.transaction():
                def siblings():
                    return {(str(r[0]), str(r[1])): r[2] for r in connection.execute("""
                        SELECT m.lecture_id, m.engagement_id, m.updated_at
                          FROM public.lecture_engagement_metrics m
                          JOIN public.lecture_sessions l ON l.lecture_id = m.lecture_id
                         WHERE l.session_date = %s AND m.lecture_id <> %s""",
                        (TARGET_DATE, G2_KEITH)).fetchall()}

                before = siblings()
                summary = _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)
                stage = next(item for item in summary["stages"]
                             if item["stage"] == "ENGAGEMENT")
                assert stage["scope"] == "LECTURE"
                assert stage["lectures_in_scope"] == 1
                assert siblings() == before
                raise _Rollback
        except _Rollback:
            pass


def test_perfect_moves_from_pending_to_eligible_and_offers_a_scoped_sync():
    with _connection() as connection:
        try:
            with connection.transaction():
                pending = _perfect(connection, G2_KEITH)
                assert pending == (False, PENDING_ATTENDANCE_DATA)

                summary = _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)

                assert RENDER_REFRESHED in summary["results"] or \
                    RENDER_REUSED in summary["results"]
                assert PERFECT_BECAME_ELIGIBLE in summary["results"]
                assert PERFECT_LEGACY_SYNC_AVAILABLE in summary["results"]
                assert summary["next_action"] == "SYNC_PERFECT"
                assert summary["perfect"]["eligibility_reason"] == ELIGIBLE
                assert summary["perfect"]["attendance_coverage_status"] == \
                    SOURCE_AVAILABLE_WITH_MEMBERS
                assert _perfect(connection, G2_KEITH) == (True, ELIGIBLE)
                raise _Rollback
        except _Rollback:
            pass


def test_becoming_eligible_still_writes_no_legacy_perfect_row_by_itself():
    """Recovery reports that the sync is available; it never performs it."""
    with _connection() as connection:
        try:
            with connection.transaction():
                before = _production_counts(connection)
                summary = _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)
                assert PERFECT_BECAME_ELIGIBLE in summary["results"]
                assert _production_counts(connection) == before
                assert summary["writes"]["perfect_legacy_rows_written"] == 0
                assert summary["writes"]["perfect_ownership_rows_written"] == 0
                raise _Rollback
        except _Rollback:
            pass


def test_a_second_recovery_with_no_new_source_change_is_a_noop():
    with _connection() as connection:
        try:
            with connection.transaction():
                source = FakeAttendanceSource(SPOKE)
                service = _service(source)
                service.recover(connection, G2_KEITH, persist=True)

                def rowcounts():
                    return {name: connection.execute(
                        "SELECT count(*) FROM public." + name +
                        " WHERE lecture_id = %s", (G2_KEITH,)).fetchone()[0]
                        for name in ("lecture_attendance_snapshots",
                                     "lecture_engagement_metrics",
                                     "lecture_qa_evaluations",
                                     "lecture_qa_rendered_sessions",
                                     "lecture_perfect_lecture_results",
                                     "lecture_qa_generation_attempts")}

                after_first = rowcounts()
                second = service.recover(connection, G2_KEITH, persist=True)
                assert rowcounts() == after_first
                assert second["openai_calls"] == 0
                # The source has not moved, so the snapshot id has not moved,
                # and the recovery stops before touching anything at all -
                # a stronger no-op than re-deriving the same answers.
                assert second["status"] == NO_CHANGE
                assert second["attendance_snapshot_changed"] is False
                assert sum(second["writes"].values()) == 0
                assert "qa" not in second
                raise _Rollback
        except _Rollback:
            pass


# --- case 2: attendance arrives and Item 7 turns Not Met ---------------------

def test_low_engagement_changes_item7_deterministically_without_the_model():
    with _connection() as connection:
        try:
            with connection.transaction():
                # One speaker out of thirteen attendees: 7.69%, below 50.
                summary = _service(FakeAttendanceSource(SPOKE[:1] + SILENT)).recover(
                    connection, G2_KEITH, persist=True)

                assert summary["attendance_authoritative_after"] is True
                assert summary["item7_override_applied"] is True
                assert summary["qa"]["previous_final_item7_status"] == "Met"
                assert summary["qa"]["final_item7_status"] == "Not Met"
                assert summary["qa"]["checklist_changed"] is True
                assert summary["qa"]["met_count"] == 10
                assert summary["qa"]["not_met_count"] == 1
                assert summary["openai_calls"] == 0
                raise _Rollback
        except _Rollback:
            pass


def test_a_changed_item7_makes_perfect_not_eligible_and_writes_no_perfect_row():
    with _connection() as connection:
        try:
            with connection.transaction():
                before = _production_counts(connection)
                summary = _service(FakeAttendanceSource(SPOKE[:1] + SILENT)).recover(
                    connection, G2_KEITH, persist=True)

                assert PERFECT_REMAINS_NOT_ELIGIBLE in summary["results"]
                assert summary["perfect"]["eligibility_reason"] == \
                    "NOT_ELIGIBLE_STATUS_NOT_ALL_MET"
                assert summary["perfect"]["is_perfect"] is False
                assert _perfect(connection, G2_KEITH) == (
                    False, "NOT_ELIGIBLE_STATUS_NOT_ALL_MET")
                assert _production_counts(connection) == before
                raise _Rollback
        except _Rollback:
            pass


def test_the_superseded_evaluation_remains_auditable():
    with _connection() as connection:
        try:
            with connection.transaction():
                original = connection.execute("""
                    SELECT evaluation_id, met_count, not_met_count, final_item7_status,
                           source_fingerprint
                      FROM public.lecture_qa_evaluations
                     WHERE lecture_id = %s AND qa_status = 'COMPLETED'
                     ORDER BY updated_at DESC LIMIT 1""", (G2_KEITH,)).fetchone()

                _service(FakeAttendanceSource(SPOKE[:1] + SILENT)).recover(
                    connection, G2_KEITH, persist=True)

                assert connection.execute("""
                    SELECT evaluation_id, met_count, not_met_count, final_item7_status,
                           source_fingerprint
                      FROM public.lecture_qa_evaluations WHERE evaluation_id = %s""",
                    (original[0],)).fetchone() == original
                raise _Rollback
        except _Rollback:
            pass


def test_a_changed_result_produces_its_own_render_rather_than_reusing_a_stale_one():
    with _connection() as connection:
        try:
            with connection.transaction():
                summary = _service(FakeAttendanceSource(SPOKE[:1] + SILENT)).recover(
                    connection, G2_KEITH, persist=True)
                assert RENDER_REFRESHED in summary["results"]
                evaluation_id = summary["qa"]["evaluation_id"]
                rendered = connection.execute("""
                    SELECT met_count, not_met_count FROM public.lecture_qa_rendered_sessions
                     WHERE lecture_id = %s AND evaluation_id = %s""",
                    (G2_KEITH, evaluation_id)).fetchone()
                assert rendered == (10, 1)
                # The render stage costs nothing at the provider either.
                stage = next(item for item in summary["stages"]
                             if item["stage"] == "PHASE_3B")
                assert stage["provider_calls"] == 0
                raise _Rollback
        except _Rollback:
            pass


def test_the_second_recovery_after_an_item7_change_is_also_a_noop():
    with _connection() as connection:
        try:
            with connection.transaction():
                source = FakeAttendanceSource(SPOKE[:1] + SILENT)
                service = _service(source)
                service.recover(connection, G2_KEITH, persist=True)
                counts = {name: connection.execute(
                    "SELECT count(*) FROM public." + name + " WHERE lecture_id = %s",
                    (G2_KEITH,)).fetchone()[0]
                    for name in ("lecture_attendance_snapshots",
                                 "lecture_engagement_metrics",
                                 "lecture_qa_evaluations",
                                 "lecture_qa_rendered_sessions",
                                 "lecture_perfect_lecture_results")}
                second = service.recover(connection, G2_KEITH, persist=True)
                assert {name: connection.execute(
                    "SELECT count(*) FROM public." + name + " WHERE lecture_id = %s",
                    (G2_KEITH,)).fetchone()[0] for name in counts} == counts
                assert second["status"] == NO_CHANGE
                assert sum(second["writes"].values()) == 0
                assert second["openai_calls"] == 0
                raise _Rollback
        except _Rollback:
            pass


# --- boundaries --------------------------------------------------------------

def test_recovery_never_writes_to_the_external_attendance_table():
    with _connection() as connection:
        try:
            with connection.transaction():
                before = connection.execute(
                    "SELECT count(*) FROM public.kbc_attendance").fetchone()[0]
                _service(FakeAttendanceSource(SPOKE)).recover(
                    connection, G2_KEITH, persist=True)
                assert connection.execute(
                    "SELECT count(*) FROM public.kbc_attendance").fetchone()[0] == before
                raise _Rollback
        except _Rollback:
            pass
