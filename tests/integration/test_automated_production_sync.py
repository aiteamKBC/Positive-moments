"""
Phase 4B integration tests: the automated production sync, against real rows.

2026-09-18 is the fixture. Five lectures were carried from discovery to a
legacy QA row by the scheduler alone, and one of them to a Perfect row - the
first ever written under the attendance-aware policy. Two of those five were
recovered from `INVALID_EVIDENCE` for zero provider calls.

These tests assert what is now true of that day, and - more importantly - that
running the machinery again changes none of it.

Everything runs inside a transaction that is rolled back.
"""
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.orchestration.locks import (
    CYCLE_LOCK_KEY,
    SchedulerCycleBusy,
    SchedulerCycleLock,
)
from app.orchestration.operations import OperationsService
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.runner import ManualReviewRequired, StageRunner
from app.orchestration.stages import NOTHING_TO_DO, SYNC_LEGACY_QA
from app.orchestration.state import PipelineStateResolver
from app.orchestration.sync_safety import classify_plan
from app.qa.evidence_policy import EVIDENCE_POLICY_V1, EVIDENCE_POLICY_V2
from app.writer.mapping import SESSION_COLUMNS
from app.writer.modes import DRY_RUN, WOULD_INSERT, WOULD_SKIP_IDENTICAL
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    FOREIGN_OWNED_COLUMNS,
    RECORDING_OWNED_COLUMNS,
)


SEPTEMBER_18 = date(2026, 9, 18)

# The two lectures recovered from INVALID_EVIDENCE without paying anything.
G2_JULIANE = "5575068e-04b2-5807-9fe2-46aa1cc2b12b"
RISK_MANAGEMENT = "708211ef-53c4-5fbd-9588-ebb0e96745fd"
# Synced straight through.
MARTECH_FRI = "5c84a940-ca3a-5901-9be4-c6db4d199ac4"
# Duplicate calendar booking; no online meeting.
RAY_DUPLICATE = "26e74d25-ea3f-5e3d-ab08-19ab949eb75f"
# Real, and genuinely waiting on the attendance source.
RAY_WAITING = "ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6"

SYNCED = (G2_JULIANE, RISK_MANAGEMENT, MARTECH_FRI,
          "5ea537d2-0e0d-5852-adc6-99672a857cd1",
          "a98a1edf-8475-564c-9f7e-c0f99fbf76e3")


class _Rollback(Exception):
    """Unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _runner(**kwargs):
    return StageRunner(settings=Settings.from_environment(), **kwargs)


def _resolver():
    return PipelineStateResolver(legacy_observations=LegacyObservationRepository())


# --- 6. the writes are verified ------------------------------------------------

def test_every_automated_qa_write_produced_a_complete_verified_row():
    with _connection() as connection:
        rows = connection.execute("""
        SELECT l.subject, w.write_status, w.legacy_session_created,
               w.checklist_rows_created, w.write_mode, w.post_write_digest,
               w.pre_write_digest
          FROM public.lecture_qa_legacy_writes w
          JOIN public.lecture_sessions l USING (lecture_id)
         WHERE l.session_date = %s ORDER BY 1
        """, (SEPTEMBER_18,)).fetchall()
        connection.rollback()
    assert len(rows) == 5
    for subject, status, created, checklist, mode, post, pre in rows:
        assert status == "WRITTEN", subject
        assert created is True, subject
        # 11 checklist rows. The writer re-reads all 22 session columns and
        # every checklist column before committing, and rolls back on any
        # mismatch, so a row that exists is a row that verified.
        assert checklist == 11, subject
        assert mode == "PRODUCTION_NEW_ONLY", subject
        assert post and post != pre, subject


def test_each_synced_lecture_has_exactly_eleven_distinct_checklist_rows():
    with _connection() as connection:
        rows = connection.execute("""
        SELECT q.session_id, count(*), count(DISTINCT ci.checklist_order)
          FROM public.qa_doctors_sessions q
          JOIN public.qa_doctors_checklist_items ci USING (session_id)
         WHERE q.date = %s GROUP BY 1
        """, (SEPTEMBER_18,)).fetchall()
        connection.rollback()
    assert len(rows) == 5
    for session_id, total, distinct in rows:
        assert total == 11 and distinct == 11, session_id


def test_the_session_mapping_covers_twenty_two_columns():
    assert len(SESSION_COLUMNS) == 22


# --- 12, 13. Perfect: foreign fields and the mapped digest ----------------------

def test_the_automated_perfect_row_preserved_every_foreign_owned_field():
    with _connection() as connection:
        row = connection.execute("""
        SELECT p.recording_url, p.recap_url, p.excel_synced_at, p.detected_at,
               p.id, p.attended_count, p.met_count
          FROM public.qa_perfect_lectures p
          JOIN public.lecture_perfect_lecture_legacy_writes w
            ON w.legacy_lecture_key = p.lecture_key
         WHERE w.lecture_id = %s
        """, (G2_JULIANE,)).fetchone()
        connection.rollback()
    assert row is not None, "the scheduler wrote a Perfect row for G2-Juliane"
    recording_url, recap_url, excel_synced_at, detected_at, row_id, attended, met = row
    # Owned by the recording branch and the Excel Sync workflow. The coded
    # writer passes NULL, so whatever they put there survives - and until the
    # recording branch fills recording_url, Excel Sync's WHERE selects nothing.
    assert recording_url is None
    assert recap_url is None
    assert excel_synced_at is None
    # Owned by the database, not by us.
    assert detected_at is not None and row_id is not None
    # Ours, and real.
    assert met == 11
    assert attended is not None


def test_the_perfect_write_recorded_the_policy_and_mapping_it_used():
    with _connection() as connection:
        row = connection.execute("""
        SELECT write_status, eligibility_version, writer_version, metadata
          FROM public.lecture_perfect_lecture_legacy_writes
         WHERE lecture_id = %s
        """, (G2_JULIANE,)).fetchone()
        connection.rollback()
    assert row[0] == "WRITTEN"
    assert row[1] == "kbc_perfect_v2_attendance_required"
    assert row[2] == "legacy_qa_v8_perfect_writer_v1"


def test_the_perfect_mapping_still_owns_ten_columns_and_disowns_the_rest():
    assert len(CODED_OWNED_COLUMNS) == 10
    assert set(FOREIGN_OWNED_COLUMNS) == {"excel_synced_at", "detected_at", "id"}
    assert set(RECORDING_OWNED_COLUMNS) == {"recording_url", "recap_url"}


# --- 5, 7, 14. running it again does nothing ------------------------------------

@pytest.mark.parametrize("lecture_id", SYNCED)
def test_a_second_plan_for_a_synced_lecture_is_skip_identical(lecture_id):
    """
    The idempotency that matters most: the writer's own planner, asked again,
    says there is nothing to do. Not "we remembered not to" - "there is no
    difference".
    """
    with _connection() as connection:
        plan = _runner(persist=True)._writer(lecture_id, mode=DRY_RUN).plan_day(
            connection, SEPTEMBER_18)
        row = _runner(persist=True)._only_lecture(plan, lecture_id)
        connection.rollback()
    verdict = classify_plan(row)
    assert row["decision"] == WOULD_SKIP_IDENTICAL
    assert verdict["qa"]["action"] == "NOOP"
    assert verdict["may_write"] is False
    assert verdict["requires_manual_review"] is False


def test_running_the_sync_again_writes_nothing_and_reports_a_noop():
    with _connection() as connection:
        try:
            before = _legacy_counts(connection)
            outcome = _runner(persist=True)._sync_legacy_qa(
                connection, MARTECH_FRI, SEPTEMBER_18)
            after = _legacy_counts(connection)
            assert outcome["status"] == "NOOP"
            assert outcome["legacy_rows_written"] == 0
            assert before == after
            raise _Rollback
        except _Rollback:
            connection.rollback()


@pytest.mark.parametrize("lecture_id", SYNCED)
def test_every_synced_lecture_now_needs_nothing(lecture_id):
    with _connection() as connection:
        state = _resolver().for_lecture(connection, lecture_id)
        connection.rollback()
    assert state["next_executable_action"] == NOTHING_TO_DO
    assert state["stages"]["LEGACY_QA_SYNC"]["state"] == "COMPLETE"
    assert state["stages"]["LEGACY_QA_SYNC"]["coded_owned"] is True


# --- 3. an unowned legacy row blocks ---------------------------------------------

def test_a_legacy_row_written_by_n8n_is_refused_not_adopted():
    """
    2026-09-04 holds six sessions the n8n pipeline wrote and this platform does
    not own. The scheduler must refuse them in every mode, for ever.
    """
    with _connection() as connection:
        states = _resolver().for_day(connection, date(2026, 9, 4))
        protected = [state for state in states
                     if state["stages"]["LEGACY_QA_SYNC"].get("reason")
                     == "LEGACY_ROW_NOT_CODED_OWNED"]
        connection.rollback()
    assert protected
    for state in protected:
        assert state["stages"]["LEGACY_QA_SYNC"]["coded_owned"] is False
        # Never offered as work.
        assert state["next_executable_action"] != SYNC_LEGACY_QA


# --- 18, 19, 20, 21. the evidence recovery cost nothing ---------------------------

@pytest.mark.parametrize("lecture_id", [G2_JULIANE, RISK_MANAGEMENT])
def test_the_recovered_lectures_spent_exactly_one_generation_ever(lecture_id):
    """
    No blind paid retry. Both lectures failed on generation 1 and were then
    RE-JUDGED, not re-generated - so the attempt history still shows exactly
    one attempt, and two of the three remain unspent.
    """
    with _connection() as connection:
        attempts = connection.execute("""
        SELECT generation_number, outcome FROM public.lecture_qa_generation_attempts
         WHERE lecture_id = %s ORDER BY generation_number
        """, (lecture_id,)).fetchall()
        evaluation = connection.execute("""
        SELECT qa_status, provider_attempts, metadata -> 'evidence_revalidation'
          FROM public.lecture_qa_evaluations WHERE lecture_id = %s
        """, (lecture_id,)).fetchone()
        connection.rollback()
    assert len(attempts) == 1, attempts
    assert attempts[0] == (1, "INVALID_EVIDENCE")
    assert evaluation[0] == "COMPLETED"
    assert evaluation[1] == 1
    revalidation = evaluation[2]
    assert revalidation is not None
    assert revalidation["provider_calls"] == 0
    assert revalidation["generation_attempts_recorded"] == 0
    assert revalidation["previous_qa_status"] == "INVALID_EVIDENCE"
    assert revalidation["previous_evidence_policy_version"] == EVIDENCE_POLICY_V1
    assert revalidation["evidence_policy_version"] == EVIDENCE_POLICY_V2


@pytest.mark.parametrize("lecture_id", [G2_JULIANE, RISK_MANAGEMENT])
def test_the_generation_history_was_not_rewritten(lecture_id):
    """Generation 1 returned INVALID_EVIDENCE, and it always will have."""
    with _connection() as connection:
        row = connection.execute("""
        SELECT outcome, invalid_evidence_clip_count, structured_output_error_count
          FROM public.lecture_qa_generation_attempts
         WHERE lecture_id = %s AND generation_number = 1
        """, (lecture_id,)).fetchone()
        connection.rollback()
    assert row[0] == "INVALID_EVIDENCE"
    assert row[1] > 0
    assert row[2] == 0


@pytest.mark.parametrize("lecture_id", [G2_JULIANE, RISK_MANAGEMENT])
def test_the_degenerate_clips_kept_their_real_status_and_never_rendered(lecture_id):
    """
    Excluded is not repaired. Every clip still carries the verdict
    `validate_clip` gave it, and only VALID clips reached the render.
    """
    with _connection() as connection:
        clips = connection.execute("""
        SELECT c.validation_status, count(*)
          FROM public.lecture_qa_evidence_clips c
          JOIN public.lecture_qa_evaluations e USING (evaluation_id)
         WHERE e.lecture_id = %s GROUP BY 1
        """, (lecture_id,)).fetchall()
        rendered = connection.execute("""
        SELECT render_status FROM public.lecture_qa_rendered_sessions rs
          JOIN public.lecture_qa_evaluations e
            ON e.evaluation_id = rs.evaluation_id
         WHERE e.lecture_id = %s
        """, (lecture_id,)).fetchall()
        connection.rollback()
    statuses = dict(clips)
    assert statuses.get("VALID", 0) > 0
    invalid = {key: value for key, value in statuses.items() if key != "VALID"}
    assert invalid, "the degenerate clips are still on record as invalid"
    assert set(invalid) <= {"END_NOT_AFTER_START", "SHORTER_THAN_MINIMUM"}
    assert any(status == "RENDERED" for (status,) in rendered)


@pytest.mark.parametrize("lecture_id", [G2_JULIANE, RISK_MANAGEMENT])
def test_recovery_left_every_upstream_stage_untouched(lecture_id):
    """
    Resume from QA_EVALUATION only. Discovery, transcript, selection, cues,
    speakers, attendance and engagement are all still COMPLETE and were never
    re-run - the source fingerprint did not move, because the question the
    provider was asked did not change.
    """
    with _connection() as connection:
        state = _resolver().for_lecture(connection, lecture_id)
        connection.rollback()
    for stage in ("DISCOVERY", "MEETING", "TRANSCRIPT", "SELECTION",
                  "CANONICAL_CUES", "SPEAKERS", "ATTENDANCE", "ENGAGEMENT"):
        assert state["stages"][stage]["state"] == "COMPLETE", stage


def test_revalidating_an_already_current_answer_is_refused_as_unchanged():
    with _connection() as connection:
        outcome = _runner(persist=False)._qa_service(
            provider=None).revalidate_evidence(
                connection, G2_JULIANE, persist=False)
        connection.rollback()
    assert outcome["revalidation_status"] == "EVIDENCE_POLICY_UNCHANGED"
    assert outcome["provider_calls"] == 0


def test_the_revalidation_service_has_no_provider_at_all():
    """Structurally incapable of buying a generation, not merely told not to."""
    service = _runner(persist=True)._qa_service(provider=None)
    assert service.provider is None


# --- 25. cycle-level concurrency ---------------------------------------------------

def test_two_scheduler_cycles_cannot_run_at_once():
    lock = SchedulerCycleLock()
    first, second = _connection(), _connection()
    try:
        assert lock.try_acquire(first) is True
        assert lock.try_acquire(second) is False
    finally:
        first.close()
        second.close()


def test_a_crashed_cycle_releases_the_cycle_lock():
    lock = SchedulerCycleLock()
    crashed = _connection()
    assert lock.try_acquire(crashed) is True
    crashed.close()
    survivor = _connection()
    try:
        assert lock.try_acquire(survivor) is True
    finally:
        survivor.close()


def test_the_cycle_lock_raises_rather_than_queueing():
    lock = SchedulerCycleLock()
    holder, waiter = _connection(), _connection()
    try:
        with lock.hold(holder):
            with pytest.raises(SchedulerCycleBusy):
                with lock.hold(waiter):
                    pass
    finally:
        holder.close()
        waiter.close()


def test_the_cycle_key_cannot_collide_with_any_lecture_key():
    from app.orchestration.locks import lock_key
    with _connection() as connection:
        ids = [str(row[0]) for row in connection.execute(
            "SELECT lecture_id FROM public.lecture_sessions").fetchall()]
        connection.rollback()
    assert ids
    assert CYCLE_LOCK_KEY not in {lock_key(value) for value in ids}


# --- Part R: Operations reflects the new states --------------------------------------

def test_a_safe_lecture_no_longer_stops_at_operator_action_required():
    """
    The Phase 4A symptom this phase removes. A finished, rendered, verified
    lecture used to sit waiting for a human for ever.
    """
    with _connection() as connection:
        operations = OperationsService(resolver=_resolver())
        matrix = operations.lecture_stage_matrix(connection, MARTECH_FRI)
        retry = operations.request_retry(connection, MARTECH_FRI)
        connection.rollback()
    assert matrix["next_executable_action"] == NOTHING_TO_DO
    assert matrix["stages"]["LEGACY_QA_SYNC"]["state"] == "COMPLETE"
    assert retry["retry_eligible"] is False
    assert retry["retry_reason"] == "NOTHING_TO_RETRY"


def test_the_day_report_now_counts_five_synced_lectures():
    with _connection() as connection:
        report = DayReconciliation(resolver=_resolver()).for_day(
            connection, SEPTEMBER_18)
        connection.rollback()
    assert report["canonical_lecture_count"] == 7
    assert report["legacy_synced_count"] == 5
    assert report["complete_count"] == 5
    # Phase 4B left the duplicate booking as a manual review. Phase 4C1
    # retired it deterministically, so the day now reads: seven calendar
    # events, six lectures, one of which is still waiting on attendance.
    assert report["review_count"] == 0
    assert report["suppressed_duplicate_count"] == 1
    assert report["business_lecture_count"] == 6
    assert report["waiting_count"] == 1
    assert report["failed_count"] == 0
    assert (report["complete_count"] + report["waiting_count"]
            + report["review_count"] + report["failed_count"]
            + report["in_progress_count"]
            + report["suppressed_duplicate_count"]) == 7


def test_the_duplicate_booking_is_now_retired_rather_than_merely_explained():
    """
    Phase 4B could only NAME this: it reported
    DUPLICATE_CALENDAR_EVENT_SIBLING_RESOLVED and pointed at the sibling,
    which left a manual review nobody could ever clear. Phase 4C1 retires it,
    and the annotation still points at the same sibling.
    """
    with _connection() as connection:
        state = _resolver().for_lecture(connection, RAY_DUPLICATE)
        connection.rollback()
    assert state["is_suppressed_duplicate"] is True
    assert state["duplicate_resolution"]["winner_lecture_id"] == RAY_WAITING
    assert state["stages"]["MEETING"]["reason"] == "DUPLICATE_EVENT_SUPPRESSED"
    assert state["next_executable_action"] == "NOTHING_TO_DO"


def test_the_attendance_waiting_lecture_still_waits_and_buys_nothing():
    with _connection() as connection:
        state = _resolver().for_lecture(connection, RAY_WAITING)
        connection.rollback()
    assert state["stages"]["ATTENDANCE"]["state"] == "WAITING"
    assert state["next_executable_action"] == "WAIT_FOR_ATTENDANCE_SOURCE"
    assert state["attendance_source_authoritative"] is False


def _legacy_counts(connection) -> dict:
    return {name: connection.execute(
        f"SELECT count(*)::int FROM public.{name}").fetchone()[0]
        for name in ("qa_doctors_sessions", "qa_doctors_checklist_items",
                     "qa_perfect_lectures", "lecture_qa_legacy_writes",
                     "lecture_perfect_lecture_legacy_writes")}
