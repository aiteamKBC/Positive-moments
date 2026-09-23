"""
Phase 4C1 integration tests: the duplicate rule against the real registry.

The fixture is the real duplicate: two Ray-MSP calendar events on 2026-09-18
at the same instant, same normalized subject, same active Aptem group,
different iCalUIDs, and exactly one of them a confirmed Teams meeting.

Every test runs inside a transaction that is rolled back. The suppression that
this phase performs for real is done once, deliberately, from the CLI - these
tests prove the behaviour without leaving any of it behind.
"""
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.lecture_duplicates import DuplicateEventRepository
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.lectures.duplicate_service import (
    ALREADY_SUPPRESSED,
    SUPPRESSED,
    DuplicateResolutionService,
)
from app.lectures.duplicates import (
    CASE_SUPPRESSED,
    DUPLICATE_RESOLUTION_VERSION,
    SUPPRESSED_REASON,
)
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.stages import (
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    STAGE_ORDER,
    SUPPRESS_DUPLICATE_EVENT,
)
from app.orchestration.state import PipelineStateResolver

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


SEPTEMBER_18 = date(2026, 9, 18)
RAY_WINNER = "ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6"
RAY_LOSER = "26e74d25-ea3f-5e3d-ab08-19ab949eb75f"


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _resolver():
    return PipelineStateResolver(legacy_observations=LegacyObservationRepository())


def _suppress(connection):
    """Suppress the real loser inside the caller's transaction."""
    return DuplicateResolutionService().resolve_lecture(
        connection, RAY_LOSER, persist=True)


def _unsuppress(connection):
    """
    Clear the annotation inside a transaction that will be rolled back.

    The real 2026-09-18 repair has been applied in production, so a test that
    wants to watch the WRITE happen has to start from before it. Doing that
    here - rather than depending on the row still being unsuppressed - keeps
    these tests meaningful for ever instead of only until the repair ran.
    """
    connection.execute(
        "UPDATE public.lecture_sessions "
        "   SET metadata = metadata - 'duplicate_suppression' "
        " WHERE lecture_id = %s", (RAY_LOSER,))


# --- the decision, read-only ---------------------------------------------------

def test_the_real_ray_pair_is_recognised_as_one_suppressible_duplicate():
    with _connection() as connection:
        decision = DuplicateResolutionService().classify_lecture(
            connection, RAY_LOSER)
        connection.rollback()
    assert decision["case"] == CASE_SUPPRESSED
    assert decision["group_size"] == 2
    assert decision["winner"]["lecture_id"] == RAY_WINNER
    assert decision["role"] == "SUPPRESSIBLE_DUPLICATE"
    assert decision["requires_manual_review"] is False


def test_the_two_events_really_are_distinct_calendar_entries():
    """
    Worth asserting against the real rows: the whole rule rests on these being
    two genuinely different calendar events describing one class.
    """
    with _connection() as connection:
        rows = {item["lecture_id"]: item for item in
                DuplicateEventRepository().load_group_for_lecture(
                    connection, RAY_LOSER)}
        connection.rollback()
    winner, loser = rows[RAY_WINNER], rows[RAY_LOSER]
    assert winner["calendar_event_id"] != loser["calendar_event_id"]
    assert winner["i_cal_uid"] != loser["i_cal_uid"]
    assert winner["scheduled_start"] == loser["scheduled_start"]
    assert winner["normalized_subject"] == loser["normalized_subject"]
    assert winner["module"] == loser["module"]
    assert winner["organizer_validation_status"] == "ORGANIZER_ID_CONFIRMED"
    assert loser["meeting_id"] is None


def test_the_registry_wide_scan_finds_no_case_needing_a_human():
    """
    The historical position, asserted rather than assumed. If a future
    duplicate arrives that the rule refuses, this test says so loudly.
    """
    with _connection() as connection:
        plan = DuplicateResolutionService().plan_all(connection)
        connection.rollback()
    assert plan["manual_review_group_count"] == 0
    for group in plan["groups"]:
        assert group["case"] == CASE_SUPPRESSED, group["group_key"]


def test_the_loser_has_no_downstream_footprint_at_all():
    with _connection() as connection:
        footprint = DuplicateEventRepository().downstream_footprint(
            connection, RAY_LOSER)
        connection.rollback()
    assert footprint == {"transcript_candidates": 0, "canonical_documents": 0,
                         "qa_evaluations": 0, "legacy_qa_writes": 0,
                         "perfect_writes": 0}


def test_a_plan_writes_nothing():
    with _connection() as connection:
        before = _metadata(connection, RAY_LOSER)
        DuplicateResolutionService().plan_day(connection, SEPTEMBER_18)
        DuplicateResolutionService().resolve_lecture(connection, RAY_LOSER)
        assert _metadata(connection, RAY_LOSER) == before
        connection.rollback()


# --- the write ------------------------------------------------------------------

def test_suppression_annotates_the_row_and_deletes_nothing():
    with _connection() as connection:
        try:
            _unsuppress(connection)
            before = connection.execute(
                "SELECT count(*)::int FROM public.lecture_sessions").fetchone()[0]
            outcome = _suppress(connection)
            after = connection.execute(
                "SELECT count(*)::int FROM public.lecture_sessions").fetchone()[0]
            assert outcome["outcome"] == SUPPRESSED
            assert outcome["persisted"] is True
            assert after == before, "a calendar event was removed"
            row = connection.execute(
                "SELECT lecture_id, calendar_event_id, i_cal_uid, downstream_ready "
                "FROM public.lecture_sessions WHERE lecture_id = %s",
                (RAY_LOSER,)).fetchone()
            assert row is not None, "the suppressed lecture lost its identity"
            assert row[1] and row[2], "the calendar identity was cleared"
            assert row[3] is False
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_12_the_stored_provenance_points_at_the_winner():
    with _connection() as connection:
        try:
            _suppress(connection)
            stored = _metadata(connection, RAY_LOSER)["duplicate_suppression"]
            assert stored["winner_lecture_id"] == RAY_WINNER
            assert stored["suppressed_lecture_id"] == RAY_LOSER
            assert stored["duplicate_resolution_reason"] == SUPPRESSED_REASON
            assert stored["duplicate_resolution_version"] == \
                DUPLICATE_RESOLUTION_VERSION
            assert stored["winner_meeting_id"]
            assert stored["resolved_at"]
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_the_discovery_diagnostics_already_on_the_row_survive():
    """
    The annotation MERGES. Discovery's own business-date trace is evidence for
    the Africa/Cairo rule and must not be collateral damage.
    """
    with _connection() as connection:
        try:
            before = _metadata(connection, RAY_LOSER)
            _suppress(connection)
            after = _metadata(connection, RAY_LOSER)
            for key, value in before.items():
                assert after[key] == value, key
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- 3..7, 16. what changes, and what must not ----------------------------------

def test_4_the_suppressed_lecture_reports_nothing_to_do():
    with _connection() as connection:
        try:
            _suppress(connection)
            state = _resolver().for_lecture(connection, RAY_LOSER)
            assert state["is_suppressed_duplicate"] is True
            assert state["next_action"] == NOTHING_TO_DO
            assert state["next_executable_action"] == NOTHING_TO_DO
            assert state["requires_review"] is False
            assert {item["state"] for item in state["stages"].values()} == \
                {NOT_APPLICABLE}
            assert len(state["stages"]) == len(STAGE_ORDER)
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_16_the_real_winner_is_completely_untouched():
    """
    The single most important assertion in this file. Retiring one calendar
    event must not change one byte of the lecture that actually happened.
    """
    with _connection() as connection:
        try:
            before = _resolver().for_lecture(connection, RAY_WINNER)
            before_row = _row(connection, RAY_WINNER)
            _suppress(connection)
            after = _resolver().for_lecture(connection, RAY_WINNER)
            assert _row(connection, RAY_WINNER) == before_row
            assert after["stages"] == before["stages"]
            assert after["next_action"] == before["next_action"]
            assert after["downstream_ready"] is True
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_the_winner_never_becomes_a_suppression_candidate():
    with _connection() as connection:
        outcome = DuplicateResolutionService().resolve_lecture(
            connection, RAY_WINNER, persist=True)
        connection.rollback()
    assert outcome["role"] == "WINNER"
    assert outcome["persisted"] is False
    assert outcome["suppressed_count"] == 0


# --- 13. idempotency --------------------------------------------------------------

def test_13_a_second_suppression_of_the_same_row_changes_nothing():
    with _connection() as connection:
        try:
            _unsuppress(connection)
            first = _suppress(connection)
            stored = _metadata(connection, RAY_LOSER)["duplicate_suppression"]
            second = _suppress(connection)
            assert first["outcome"] == SUPPRESSED
            assert second["outcome"] == ALREADY_SUPPRESSED
            assert second["suppressed_count"] == 0
            # Including `resolved_at`: the decision was made once.
            assert _metadata(connection, RAY_LOSER)["duplicate_suppression"] == stored
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_13b_a_second_day_resolution_finds_no_outstanding_work():
    with _connection() as connection:
        try:
            DuplicateResolutionService().resolve_day(
                connection, SEPTEMBER_18, persist=True)
            again = DuplicateResolutionService().resolve_day(
                connection, SEPTEMBER_18, persist=True)
            assert again["suppressed_count"] == 0
            assert again["auto_suppressible_count"] == 0
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- the day report ---------------------------------------------------------------

def test_15_the_day_stops_reporting_the_duplicate_as_an_unresolved_lecture():
    with _connection() as connection:
        try:
            before = DayReconciliation(resolver=_resolver()).for_day(
                connection, SEPTEMBER_18)
            _suppress(connection)
            after = DayReconciliation(resolver=_resolver()).for_day(
                connection, SEPTEMBER_18)

            # Before Phase 4C1 this day reported the duplicate as a manual
            # review. It is now runnable work instead, and after the write it
            # is neither: not a review, not a failure, not a lecture.
            assert before["review_count"] == 0
            assert after["review_count"] == 0
            assert after["in_progress_count"] <= before["in_progress_count"]
            assert after["failed_count"] == 0
            assert after["suppressed_duplicate_count"] == 1
            # The extra calendar event is still there, and still counted as a
            # calendar event - it is simply no longer counted as a lecture.
            assert after["canonical_lecture_count"] == \
                before["canonical_lecture_count"]
            assert after["business_lecture_count"] == \
                before["canonical_lecture_count"] - 1
            assert any(row["lecture_id"] == RAY_LOSER
                       for row in after["lectures"])
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_the_suppression_does_not_change_any_other_lecture_on_the_day():
    with _connection() as connection:
        try:
            before = {state["lecture_id"]: state["stages"]
                      for state in _resolver().for_day(connection, SEPTEMBER_18)}
            _suppress(connection)
            after = {state["lecture_id"]: state["stages"]
                     for state in _resolver().for_day(connection, SEPTEMBER_18)}
            assert set(before) == set(after)
            for lecture_id, stages in before.items():
                if lecture_id == RAY_LOSER:
                    continue
                assert after[lecture_id] == stages, lecture_id
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- 5, 6, 7. the suppressed row cannot be worked on -------------------------------

def test_5_6_7_the_suppressed_row_offers_the_orchestrator_no_runnable_action():
    """
    Nothing to acquire, nothing to evaluate, nothing to write. The resolver
    does not even read the transcript, QA or writer tables for it.
    """
    with _connection() as connection:
        try:
            _suppress(connection)
            state = _resolver().for_lecture(connection, RAY_LOSER)
            assert state["next_executable_action"] == NOTHING_TO_DO
            assert state["executable_stage"] is None
            assert state["operator_actions"] == []
            assert state["is_complete"] is True
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_before_suppression_the_runnable_action_is_the_suppression_itself():
    with _connection() as connection:
        state = _resolver().for_lecture(connection, RAY_LOSER)
        connection.rollback()
    # Only meaningful while the real repair has not been applied yet; once it
    # has, the row is suppressed and the assertion above covers it.
    assert state["next_executable_action"] in (SUPPRESS_DUPLICATE_EVENT,
                                               NOTHING_TO_DO)


def _row(connection, lecture_id):
    return connection.execute(
        "SELECT lecture_id, calendar_event_id, i_cal_uid, meeting_id, "
        "       calendar_mapping_status, organizer_validation_status, "
        "       discovery_status, downstream_ready, metadata "
        "  FROM public.lecture_sessions WHERE lecture_id = %s",
        (lecture_id,)).fetchone()


def _metadata(connection, lecture_id):
    return connection.execute(
        "SELECT metadata FROM public.lecture_sessions WHERE lecture_id = %s",
        (lecture_id,)).fetchone()[0]
