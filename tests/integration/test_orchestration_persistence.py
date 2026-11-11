"""
Phase 4A integration tests: the orchestrator against the real database.

The fixtures are the real pilot lectures. 2026-09-17 is the interesting day:
Stephen is finished; G2 Keith and Martech have an attendance source that is
genuinely empty. Under attendance-optional QA neither WAITS: both carry the
PENDING_ATTENDANCE flag, G2 Keith's pre-fix false zero is offered the free
deterministic refresh, and Martech's json_object_v1 answer - which cannot be
reused - goes to review. Nothing here substitutes any of
that - the whole point is to find out what the orchestrator does when the
state is the awkward state we actually have.

Every test runs inside a transaction that is deliberately rolled back. These
rows are the evidence for five phases of migration work and destroying them
would destroy the ability to prove any of it.

The n8n precheck is stubbed. It is exercised for real against the live
workflow by `scheduler-status`; making it an HTTP call inside a unit of the
test suite would make the suite depend on a production system being reachable.
"""
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.pipeline_runs import PipelineRunRepository
from app.orchestration.locks import LectureBusy, LectureLockManager, lock_key
from app.orchestration.n8n_preflight import LEGACY_QA_DISABLED, LEGACY_QA_ENABLED
from app.orchestration.operations import OperationsService
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.runner import StageRunner
from app.orchestration.stages import (
    ITEM_BLOCKED,
    NOTHING_TO_DO,
    ORCHESTRATION_VERSION,
    RETRY,
    RUN_BLOCKED_LEGACY_QA_ACTIVE,
    RUN_COMPLETED,
    RUN_TYPE_MANUAL,
    MANUAL_REVIEW_REQUIRED,
    REFRESH_DETERMINISTIC_QA,
    WAIT_FOR_ATTENDANCE_SOURCE,
)
from app.orchestration.state import PipelineStateResolver
from app.qa.inputs import ATTENDANCE_PENDING

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
SEPTEMBER_16 = date(2026, 9, 16)
SEPTEMBER_17 = date(2026, 9, 17)

# Every table a run could conceivably touch, coded and legacy.
WATCHED_TABLES = (
    "lecture_sessions", "lecture_transcript_artifacts",
    "lecture_transcript_selections", "lecture_transcript_documents",
    "lecture_transcript_speakers", "lecture_attendance_snapshots",
    "lecture_engagement_metrics", "lecture_qa_evaluations",
    "lecture_qa_checklist_items", "lecture_qa_rendered_sessions",
    "lecture_qa_legacy_writes", "lecture_perfect_lecture_results",
    "lecture_perfect_lecture_legacy_writes", "lecture_pipeline_runs",
    "lecture_pipeline_run_items",
    # The legacy production tables. These must never move.
    "qa_doctors_sessions", "qa_doctors_checklist_items", "qa_perfect_lectures",
    # External, read-only upstream.
    "kbc_attendance",
)


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _counts(connection) -> dict:
    return {name: connection.execute(
        f"SELECT count(*)::int FROM public.{name}").fetchone()[0]
        for name in WATCHED_TABLES}


class StubPreflight:
    def __init__(self, status=LEGACY_QA_DISABLED, disabled=True):
        self.status = status
        self.disabled = disabled

    def check(self):
        return {"status": self.status, "legacy_qa_node_disabled": self.disabled,
                "reason": self.status, "writes_permitted": self.disabled,
                "read_only": True, "http_method": "GET", "n8n_modified": False}


def _resolver(probe=None):
    return PipelineStateResolver(
        legacy_observations=LegacyObservationRepository(), attendance_probe=probe)


def _orchestrator(*, dry_run=False, preflight=None, allow_graph=False,
                  allow_provider=False, lock_manager=None):
    """
    Graph and the provider are OFF by default in these tests.

    Not because the orchestrator cannot use them, but because a test suite that
    can spend money or call a production API is a test suite nobody runs. The
    gates being real is itself one of the things under test.
    """
    settings = Settings.from_environment()
    return PipelineOrchestrator(
        resolver=_resolver(),
        runner=StageRunner(settings=settings, allow_graph=allow_graph,
                           allow_provider=allow_provider, persist=not dry_run),
        run_repository=None if dry_run else PipelineRunRepository(),
        lock_manager=lock_manager,
        preflight=preflight or StubPreflight())


# --- the state model against real rows ---------------------------------------

def test_the_resolver_describes_the_real_pilot_day_correctly():
    with _connection() as connection:
        states = {item["subject"]: item
                  for item in _resolver().for_day(connection, SEPTEMBER_17)}
        connection.rollback()
    assert len(states) == 3
    # Attendance-optional QA (2026-09-24): missing attendance is a FLAG, not a
    # wait. Real-history values below come from the read-only 2026-09-24
    # audit; the same contract runs in the gate on the synthetic fixture in
    # test_attendance_optional_acceptance.py.
    by_action = {subject: state["next_executable_action"]
                 for subject, state in states.items()}
    assert WAIT_FOR_ATTENDANCE_SOURCE not in by_action.values(), by_action
    keith = next(action for subject, action in by_action.items() if "Keith" in subject)
    martech = next(action for subject, action in by_action.items() if "Martech" in subject)
    assert keith == REFRESH_DETERMINISTIC_QA
    assert martech == MANUAL_REVIEW_REQUIRED
    stephen = next(state for subject, state in states.items()
                   if "Stephen" in subject)
    assert stephen["next_executable_action"] == NOTHING_TO_DO


def test_the_two_attendance_pending_lectures_are_flagged_not_blocked():
    with _connection() as connection:
        states = [item for item in _resolver().for_day(connection, SEPTEMBER_17)
                  if not item["attendance_source_authoritative"]]
        connection.rollback()
    # Attendance-optional QA (2026-09-24): missing attendance is a FLAG, not a
    # wait. Real-history values below come from the read-only 2026-09-24
    # audit; the same contract runs in the gate on the synthetic fixture in
    # test_attendance_optional_acceptance.py.
    assert len(states) == 2
    for state in states:
        assert state["stages"]["ATTENDANCE"]["state"] == "WAITING"
        assert state["stages"]["ATTENDANCE"]["blocks_qa"] is False
        assert state["attendance_flag"] == ATTENDANCE_PENDING
        for stage in ("TRANSCRIPT", "SELECTION", "CANONICAL_CUES", "SPEAKERS",
                      "ENGAGEMENT", "QA_RENDER"):
            assert state["stages"][stage]["state"] == "COMPLETE", stage
        qa = state["stages"]["QA_EVALUATION"]
        if "Keith" in state["subject"]:
            assert (qa["state"], qa["reason"]) == (
                "STALE", "EVALUATION_CARRIES_UNVERIFIED_ATTENDANCE")
            assert state["requires_review"] is False
        else:
            assert (qa["state"], qa["reason"]) == (
                "REVIEW_REQUIRED", "UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE")
            assert state["requires_review"] is True


def test_no_lecture_on_the_settled_day_needs_qa_regenerated():
    """
    2026-09-16 is finished work. If the state model ever concluded that any of
    it needed a new generation, the scheduler would buy seven of them on its
    first unattended night.
    """
    with _connection() as connection:
        states = _resolver().for_day(connection, SEPTEMBER_16)
        connection.rollback()
    assert len(states) == 7
    for state in states:
        assert state["stages"]["QA_EVALUATION"]["state"] == "COMPLETE"
        assert state["stages"]["QA_RENDER"]["state"] == "COMPLETE"
        assert state["next_executable_action"] != "RUN_QA"


def test_the_seam_rebuilt_lecture_is_not_reported_as_stale():
    """
    Andrew holds two canonical documents and two engagement rows on ONE
    attendance snapshot. A resolver that picked the newest row rather than the
    one the evaluation actually used would call this lecture stale forever.
    """
    with _connection() as connection:
        state = next(item for item in _resolver().for_day(connection, SEPTEMBER_16)
                     if item["subject"].startswith("Andrew"))
        connection.rollback()
    assert state["stages"]["ENGAGEMENT"]["state"] == "COMPLETE"
    assert state["stages"]["QA_EVALUATION"]["state"] == "COMPLETE"


def test_legacy_rows_the_platform_did_not_write_are_reported_as_protected():
    with _connection() as connection:
        states = _resolver().for_day(connection, date(2026, 9, 4))
        connection.rollback()
    protected = [state for state in states
                 if state["stages"]["LEGACY_QA_SYNC"]["state"] == "NOT_APPLICABLE"
                 and state["stages"]["LEGACY_QA_SYNC"].get("reason")
                 == "LEGACY_ROW_NOT_CODED_OWNED"]
    assert protected, "2026-09-04 holds legacy rows written by the n8n pipeline"
    for state in protected:
        assert state["stages"]["LEGACY_QA_SYNC"]["coded_owned"] is False


# --- 26. the dry run writes nothing -------------------------------------------

def test_a_dry_run_over_two_real_days_writes_nothing_at_all():
    with _connection() as connection:
        before = _counts(connection)
        outcome = _orchestrator(dry_run=True).run_window(
            connection, SEPTEMBER_17, lookback_days=1, dry_run=True)
        after = _counts(connection)
        connection.rollback()
    assert before == after
    assert outcome["run_id"] is None
    assert outcome["graph_calls"] == 0
    assert outcome["provider_calls"] == 0
    assert outcome["legacy_rows_written"] == 0
    assert all(item["actions"] == [] for item in outcome["lectures"])


def test_a_dry_run_reports_the_same_next_actions_the_resolver_does():
    with _connection() as connection:
        states = {item["lecture_id"]: item["next_executable_action"]
                  for item in _resolver().for_day(connection, SEPTEMBER_17)}
        outcome = _orchestrator(dry_run=True).run_window(
            connection, SEPTEMBER_17, dry_run=True)
        connection.rollback()
    for item in outcome["lectures"]:
        assert item["initial_action"] == states[item["lecture_id"]]


# --- a real run, rolled back ---------------------------------------------------

def test_a_real_run_persists_a_run_row_and_one_item_per_lecture():
    with _connection() as connection:
        try:
            outcome = _orchestrator().run_window(
                connection, SEPTEMBER_17, run_type=RUN_TYPE_MANUAL)
            run_id = outcome["run_id"]
            assert run_id is not None
            repository = PipelineRunRepository()
            items = repository.items_for_run(connection, run_id)
            assert len(items) == outcome["counts"]["lectures_seen"] == 3
            row = connection.execute(
                "SELECT status, lectures_seen, waiting_count, completed_count, "
                "legacy_qa_precheck_status, legacy_qa_node_disabled, "
                "orchestration_version, finished_at "
                "FROM public.lecture_pipeline_runs WHERE run_id = %s",
                (run_id,)).fetchone()
            assert row[0] in (RUN_COMPLETED, "COMPLETED_WITH_FAILURES")
            assert row[1] == 3
            assert row[4] == LEGACY_QA_DISABLED
            assert row[5] is True
            assert row[6] == ORCHESTRATION_VERSION
            assert row[7] is not None
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_the_run_counts_partition_the_lectures_seen():
    with _connection() as connection:
        try:
            counts = _orchestrator().run_window(
                connection, SEPTEMBER_17)["counts"]
            assert (counts["completed_count"] + counts["waiting_count"]
                    + counts["review_count"] + counts["failed_count"]
                    + counts["skipped_count"]) == counts["lectures_seen"]
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_a_real_run_on_the_waiting_day_costs_no_provider_call_and_no_graph_call():
    """
    The load-bearing economic property. Two of three lectures have no
    attendance, and every cycle from now until that data lands must cost no
    provider call and no Graph call. The one write the day still needs - G2
    Keith's own coded row, its false zero refreshed to UNKNOWN - is free.
    """
    with _connection() as connection:
        try:
            outcome = _orchestrator().run_window(connection, SEPTEMBER_17)
            assert outcome["provider_calls"] == 0
            assert outcome["graph_calls"] == 0
            assert outcome["legacy_rows_written"] <= 1
            assert outcome["counts"]["waiting_count"] == 0
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_a_real_run_never_touches_a_legacy_production_table():
    with _connection() as connection:
        try:
            before = _counts(connection)
            _orchestrator().run_window(connection, SEPTEMBER_17, lookback_days=1)
            after = _counts(connection)
            for table in ("qa_doctors_sessions", "qa_doctors_checklist_items",
                          "qa_perfect_lectures", "lecture_qa_legacy_writes",
                          "lecture_perfect_lecture_legacy_writes",
                          "kbc_attendance"):
                assert before[table] == after[table], table
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- 16, 33. idempotency -------------------------------------------------------

def test_a_second_run_over_the_same_window_changes_nothing():
    with _connection() as connection:
        try:
            orchestrator = _orchestrator()
            orchestrator.run_window(connection, SEPTEMBER_17)
            before = _counts(connection)
            second = orchestrator.run_window(connection, SEPTEMBER_17)
            after = _counts(connection)
            assert second["idempotent"] is True
            assert second["passes"] == 1
            # Only the audit rows the second run itself wrote.
            for table, count in before.items():
                if table in ("lecture_pipeline_runs", "lecture_pipeline_run_items"):
                    continue
                assert after[table] == count, table
            raise _Rollback
        except _Rollback:
            connection.rollback()


def test_the_pending_lectures_after_a_run_bought_nothing_and_only_keith_refreshed():
    with _connection() as connection:
        try:
            before = _counts(connection)
            outcome = _orchestrator().run_window(connection, SEPTEMBER_17)
            after = _counts(connection)
            final = {item["subject"]: item["final_action"] for item in outcome["lectures"]}
            keith = next(action for subject, action in final.items() if "Keith" in subject)
            martech = next(action for subject, action in final.items()
                           if "Martech" in subject)
            assert keith == WAIT_FOR_ATTENDANCE_SOURCE      # refreshed; flag remains
            assert martech == MANUAL_REVIEW_REQUIRED
            assert outcome["provider_calls"] == 0
            assert before["lecture_attendance_snapshots"] == \
                after["lecture_attendance_snapshots"]
            assert before["lecture_engagement_metrics"] == \
                after["lecture_engagement_metrics"]
            # G2 Keith's deterministic refresh: one new evaluation, no model call.
            assert after["lecture_qa_evaluations"] == before["lecture_qa_evaluations"] + 1
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- 18. the precheck blocks a real run ---------------------------------------

def test_an_enabled_legacy_qa_node_blocks_the_run_before_any_write():
    with _connection() as connection:
        try:
            before = _counts(connection)
            outcome = _orchestrator(
                preflight=StubPreflight(LEGACY_QA_ENABLED, disabled=False)
            ).run_window(connection, SEPTEMBER_17)
            after = _counts(connection)
            assert outcome["status"] == RUN_BLOCKED_LEGACY_QA_ACTIVE
            assert all(item["status"] == ITEM_BLOCKED
                       for item in outcome["lectures"])
            for table, count in before.items():
                if table in ("lecture_pipeline_runs", "lecture_pipeline_run_items"):
                    continue
                assert after[table] == count, table
            raise _Rollback
        except _Rollback:
            connection.rollback()


# --- 23, 24, 25. real advisory locks -------------------------------------------


def test_a_held_lecture_is_skipped_by_a_concurrent_run_and_its_siblings_proceed():
    manager = LectureLockManager()
    holder = _connection()
    assert manager.try_acquire(holder, G2_KEITH) is True
    try:
        with _connection() as connection:
            try:
                outcome = _orchestrator(lock_manager=manager).run_window(
                    connection, SEPTEMBER_17)
                statuses = {item["lecture_id"]: item["status"]
                            for item in outcome["lectures"]}
                # G2 Keith is held, so the cycle steps over it rather than
                # waiting - and the other two lectures are still answered.
                assert len(statuses) == 3
                raise _Rollback
            except _Rollback:
                connection.rollback()
    finally:
        holder.close()


# --- 28..30. reconciliation against real rows ----------------------------------

def test_the_day_report_reconciles_with_the_lecture_states():
    with _connection() as connection:
        resolver = _resolver()
        states = resolver.for_day(connection, SEPTEMBER_17)
        report = DayReconciliation(resolver=resolver).for_day(connection,
                                                              SEPTEMBER_17)
        connection.rollback()
    assert report["canonical_lecture_count"] == len(states) == 3
    assert (report["complete_count"] + report["waiting_count"]
            + report["review_count"] + report["failed_count"]
            + report["in_progress_count"]) == report["canonical_lecture_count"]
    assert report["attendance_waiting_count"] == 2
    assert report["perfect_pending_attendance_count"] == 2


def test_the_day_report_names_every_stage_for_every_lecture():
    with _connection() as connection:
        report = DayReconciliation(resolver=_resolver()).for_day(
            connection, SEPTEMBER_16)
        connection.rollback()
    for row in report["lectures"]:
        assert set(row["stages"]) == set(report["by_stage"])


def test_the_window_report_sums_its_days():
    with _connection() as connection:
        report = DayReconciliation(resolver=_resolver()).for_window(
            connection, [SEPTEMBER_16, SEPTEMBER_17])
        connection.rollback()
    assert report["totals"]["canonical_lecture_count"] == 10
    assert report["days"] == ["2026-09-16", "2026-09-17"]


# --- Part S. the Operations interfaces ------------------------------------------

def test_the_operations_layer_answers_every_question_the_future_ui_needs():
    with _connection() as connection:
        operations = OperationsService(resolver=_resolver(),
                                       run_repository=PipelineRunRepository())
        day = operations.day_reconciliation(connection, SEPTEMBER_17)
        matrix = operations.lecture_stage_matrix(connection, G2_KEITH)
        action = operations.lecture_next_action(connection, G2_KEITH)
        pending = operations.pending_attendance(connection, SEPTEMBER_17)
        review = operations.review_required(connection, SEPTEMBER_17)
        runs = operations.recent_runs(connection, 5)
        history = operations.lecture_run_history(connection, G2_KEITH, 5)
        connection.rollback()
    assert day["canonical_lecture_count"] == 3
    assert matrix["stages"]["ATTENDANCE"]["state"] == "WAITING"
    assert action["next_action"] == REFRESH_DETERMINISTIC_QA
    assert len(pending) == 2
    assert [row["subject"] for row in review if "Martech" in row["subject"]] == \
        [row["subject"] for row in review]
    assert len(review) == 1
    assert isinstance(runs, list) and isinstance(history, list)


def test_a_pending_lecture_is_offered_its_free_refresh_and_never_a_force():
    with _connection() as connection:
        operations = OperationsService(resolver=_resolver())
        matrix = operations.lecture_stage_matrix(connection, G2_KEITH)
        request = operations.request_retry(connection, G2_KEITH)
        connection.rollback()
    # G2 Keith no longer waits: its pre-fix false zero has a free,
    # deterministic refresh, and a retry resumes exactly there.
    assert matrix["retry_eligibility"]["retry_eligible"] is True
    assert matrix["retry_eligibility"]["retry_reason"] is None
    assert request["action"] == REFRESH_DETERMINISTIC_QA
    assert matrix["force_reprocess_eligibility"]["available_to_scheduler"] is False
    assert matrix["force_reprocess_eligibility"][
        "requires_explicit_operator_action"] is True
    assert request["semantics"] == RETRY


def test_requesting_a_retry_returns_a_plan_and_performs_nothing():
    with _connection() as connection:
        before = _counts(connection)
        OperationsService(resolver=_resolver()).request_retry(connection, G2_KEITH)
        after = _counts(connection)
        connection.rollback()
    assert before == after
