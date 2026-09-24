"""
Attendance-optional QA (attendance_optional_qa_v1).

The platform's job is lecture QUALITY ASSURANCE, and its evidence is the
transcript. Attendance is an external enrichment the platform does not
control. So a missing attendance source is a FLAG on the lecture, never a wall
in front of QA:

  * QA runs from the transcript; Item 7 stays the model's own answer;
  * every value only attendance can supply is UNKNOWN (NULL) - never the empty
    snapshot's "0 attended, score 1", because UNKNOWN != ZERO;
  * the result renders and reaches qa_doctors_sessions;
  * Perfect still requires authoritative attendance and stays pending;
  * when attendance arrives, the lecture is refreshed DETERMINISTICALLY - the
    stored model answer is reused, no provider call - and the same coded-owned
    legacy row is updated in place.

Resolver and orchestrator are the real ones; only the database and the stage
services are stand-ins here. tests/integration/test_attendance_optional_qa_persistence.py
runs the same journey against real PostgreSQL.
"""
from datetime import timedelta

import pytest

from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.stages import (
    ATTENDANCE,
    CALCULATE_ENGAGEMENT,
    EVALUATE_PERFECT,
    LEGACY_QA_SYNC,
    NOTHING_TO_DO,
    PERFECT_ELIGIBILITY,
    QA_EVALUATION,
    QA_RENDER,
    RECOVER_ATTENDANCE,
    REFRESH_DETERMINISTIC_QA,
    RENDER_QA,
    RESOLVE_ATTENDANCE,
    REVIEW_REQUIRED,
    RUN_QA,
    RUN_TYPE_BACKFILL,
    RUN_TYPE_SCHEDULED,
    STALE,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
    WAIT_FOR_ATTENDANCE_SOURCE,
    WAITING,
)
from app.orchestration.state import PipelineStateResolver
from app.qa.evidence_policy import DEFAULT_EVIDENCE_POLICY
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    ELIGIBLE,
    NOT_ELIGIBLE_STATUS_NOT_ALL_MET,
    PENDING_ATTENDANCE_DATA,
)
from app.qa.service import COMPLETED, INVALID_EVIDENCE
from app.rendering.evidence import RENDERER_VERSION
from app.writer.mapping import WRITER_VERSION
from test_orchestration import StubPreflight
from test_pipeline_state import (
    ATTENDANCE_RESOLUTION_VERSION,
    DOCUMENT_ID,
    ENGAGEMENT_ID,
    EVALUATION_ID,
    FINGERPRINT,
    LECTURE_ID,
    LEGACY_LECTURE_KEY,
    LEGACY_SESSION_ID,
    NOW,
    RENDER_ID,
    SESSION_DATE,
    SNAPSHOT_ID,
    WRITE_ID,
    StubConnection,
    StubObservations,
    pending_attendance_rows,
    resolve,
)
from tests.unit.test_shadow_qa import (
    MET,
    NOT_MET,
    PARTIALLY_MET,
    StubProvider,
    good_output,
    package,
    run_one,
)


LATE_SNAPSHOT_ID = "22222222-2222-5222-8222-2222222222bb"
LATE_ENGAGEMENT_ID = "55555555-5555-5555-8555-5555555555bb"
REFRESHED_EVALUATION_ID = "66666666-6666-5666-8666-6666666666bb"
REFRESHED_RENDER_ID = "77777777-7777-5777-8777-7777777777bb"
SOURCE_MISSING_ENGAGEMENT = {
    # What Phase 2C4 really writes for an empty snapshot: legacy's encoding of
    # "nobody attended" - 0 attended, 0.00%, score 1 - flagged by its detail.
    "engagement_lineage": [(ENGAGEMENT_ID, DOCUMENT_ID, SNAPSHOT_ID,
                            "NO_ATTENDED_LEARNERS", 0, 0, False, 1,
                            "ATTENDANCE_SOURCE_MISSING", False, NOW)],
    "engagement_recovery": [(ENGAGEMENT_ID, "NO_ATTENDED_LEARNERS", 0, 0, 1, False,
                             "SOURCE_MISSING", "ATTENDANCE_SOURCE_MISSING",
                             SNAPSHOT_ID, False, "attendance_coverage_v1")],
}


def evaluation(*, evaluation_id=EVALUATION_ID, fingerprint=FINGERPRINT,
               status="COMPLETED", engagement_id=ENGAGEMENT_ID,
               snapshot_id=SNAPSHOT_ID, carries_attendance=False, updated_at=NOW):
    """One row of the recovery EVALUATIONS query, in column order."""
    counts = (11, 0, 0) if status == "COMPLETED" else (None, None, None)
    return (evaluation_id, fingerprint, status, None, 1, True, *counts,
            "strict_json_schema_v1", "canonical_cue_bounds_v1", updated_at,
            engagement_id, snapshot_id, "c" * 64, None, DOCUMENT_ID,
            DEFAULT_EVIDENCE_POLICY, carries_attendance)


def perfect_result(reason, *, is_perfect=False, coverage="SOURCE_MISSING"):
    return (DEFAULT_PERFECT_ELIGIBILITY_VERSION, is_perfect, reason, coverage,
            ELIGIBLE if reason == PENDING_ATTENDANCE_DATA else reason, NOW)


def fresh_rows(**overrides):
    """Transcript, selection, cues and speakers ready; attendance never answered;
    nothing downstream of engagement has happened yet."""
    rows = pending_attendance_rows(
        evaluations=[], attempts=[], rendered=[], qa_writes=[],
        legacy_writes_state=[], perfect_results=[], perfect_writes=[],
        perfect_writes_state=[], keys=[], **SOURCE_MISSING_ENGAGEMENT)
    rows.update(overrides)
    return rows


class Observations(StubObservations):
    """No legacy row exists until OUR sync writes one."""

    def __init__(self):
        super().__init__(recording_url=None, legacy_row=False, perfect_row=False)


def resolve_fresh(**overrides):
    return resolve(fresh_rows(**overrides), observations=Observations())


# --- 1. SOURCE_MISSING does not block RUN_QA -----------------------------------

def test_1_missing_attendance_does_not_block_run_qa():
    state = resolve_fresh()
    assert state["stages"][ATTENDANCE]["state"] == WAITING
    assert state["stages"][ATTENDANCE]["blocks_qa"] is False
    assert state["next_action"] == RUN_QA
    assert state["next_executable_action"] == RUN_QA
    assert state["blocking_stage"] == QA_EVALUATION
    assert state["is_waiting"] is False
    # The flag, stated as data - not a QA outcome.
    assert state["attendance_coverage_status"] == "SOURCE_MISSING"
    assert state["attendance_source_authoritative"] is False
    assert state["attendance_flag"] == "PENDING_ATTENDANCE"
    assert state["attendance_flag_label"] == "Attendance pending"


def test_1_attendance_is_still_resolved_and_engagement_still_calculated_first():
    """The empty snapshot and its engagement row are the lineage a late answer
    is detected against, so they are still produced - they just never block."""
    unresolved = resolve_fresh(coverage=[], engagement_lineage=[],
                               engagement_recovery=[])
    assert unresolved["next_executable_action"] == RESOLVE_ATTENDANCE
    uncalculated = resolve_fresh(engagement_lineage=[], engagement_recovery=[])
    assert uncalculated["next_executable_action"] == CALCULATE_ENGAGEMENT


def test_1_every_non_authoritative_status_is_stepped_over():
    for coverage in ([(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 3)],
                     [(SNAPSHOT_ID, 4, 4, 0, ATTENDANCE_RESOLUTION_VERSION, 4)]):
        state = resolve_fresh(coverage=coverage)
        assert state["attendance_coverage_status"] == "SOURCE_PARTIAL_OR_INVALID"
        assert state["next_executable_action"] == RUN_QA


# --- 2-4. the QA answer itself ----------------------------------------------------

def test_2_3_qa_completes_with_the_model_item7_and_no_override():
    """
    The fixture's engagement says Item 7 = Not Met with an override; on an
    empty snapshot that deterministic answer does not exist, so the model's
    Partially Met stands.
    """
    ai = good_output()
    ai["checklist_evaluation"][6]["status"] = PARTIALLY_MET
    provider = StubProvider(ai)
    result, stored, _ = run_one(
        package(attendance_source_row_count=0, attendance_present_row_count=0,
                attendance_effective_member_count=0,
                learner_engagement_status=NOT_MET, item7_override_applied=True),
        provider)
    assert result["qa_status"] == COMPLETED and provider.calls == 1
    evaluation_row = stored["evaluation"]
    assert evaluation_row["item7_override_applied"] is False
    assert evaluation_row["ai_item7_status"] == PARTIALLY_MET
    assert evaluation_row["final_item7_status"] == PARTIALLY_MET
    item7 = [row for row in stored["checklist"] if row["checklist_order"] == 7]
    assert item7[0]["status"] == PARTIALLY_MET


def test_4_missing_attendance_never_becomes_zero_attendance():
    result, stored, _ = run_one(package(
        attendance_source_row_count=0, attendance_present_row_count=0,
        attendance_effective_member_count=0, attended_count=0, spoke_count=0,
        engagement_percentage="0.00", engagement_score=1))
    evaluation_row = stored["evaluation"]
    for field in ("attended_count", "spoke_count", "engagement_percentage",
                  "engagement_score"):
        assert evaluation_row[field] is None, field
    assert evaluation_row["metadata"]["attendance_source_authoritative"] is False
    assert evaluation_row["metadata"]["attendance_flag"] == "PENDING_ATTENDANCE"
    assert result["attendance_flag"] == "PENDING_ATTENDANCE"


def test_4_authoritative_attendance_is_untouched():
    """Everything that was true with attendance is still exactly true."""
    result, stored, _ = run_one(package())
    evaluation_row = stored["evaluation"]
    assert result["attendance_flag"] is None
    assert evaluation_row["attended_count"] == 10
    assert evaluation_row["engagement_score"] == 5
    assert evaluation_row["item7_override_applied"] is True
    assert evaluation_row["final_item7_status"] == MET


def test_4_an_authoritative_confirmed_zero_is_still_a_real_zero():
    _, stored, _ = run_one(package(attendance_source_row_count=5,
                                   attendance_present_row_count=0,
                                   attendance_effective_member_count=0,
                                   attended_count=0, spoke_count=0,
                                   engagement_percentage="0.00", engagement_score=1,
                                   learner_engagement_status=None,
                                   item7_override_applied=False))
    assert stored["evaluation"]["attended_count"] == 0
    assert stored["evaluation"]["engagement_score"] == 1


def test_4_the_attendance_pending_answer_has_its_own_fingerprint():
    """An answer built without attendance never reuses one that claimed zero."""
    missing = run_one(package(attendance_source_row_count=0,
                              attendance_present_row_count=0,
                              attendance_effective_member_count=0))[0]
    present = run_one(package())[0]
    assert missing["source_fingerprint"] != present["source_fingerprint"]
    # Same question to the provider, so a later refresh may reuse the answer.
    assert missing["attendance_policy_version"] == "attendance_optional_qa_v1"


# --- 5, 6, 8, 9. render, legacy sync, Perfect --------------------------------------

def test_5_the_render_is_offered_without_authoritative_attendance():
    state = resolve_fresh(evaluations=[evaluation()])
    assert state["stages"][QA_EVALUATION]["state"] == "COMPLETE"
    assert state["next_executable_action"] == RENDER_QA
    assert state["executable_stage"] == QA_RENDER


def test_6_8_the_legacy_sync_is_not_held_back_by_a_pending_perfect_answer():
    state = resolve_fresh(
        evaluations=[evaluation()],
        rendered=[(RENDER_ID, "RENDERED", RENDERER_VERSION, FINGERPRINT, 11, 0, 0,
                   EVALUATION_ID, LEGACY_SESSION_ID)],
        perfect_results=[perfect_result(PENDING_ATTENDANCE_DATA)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)])
    assert state["stages"][PERFECT_ELIGIBILITY]["state"] == WAITING
    assert state["stages"][PERFECT_ELIGIBILITY]["reason"] == PENDING_ATTENDANCE_DATA
    assert state["next_executable_action"] == SYNC_LEGACY_QA
    assert state["executable_stage"] == LEGACY_QA_SYNC


def test_8_9_perfect_is_never_offered_while_attendance_is_pending():
    state = resolve_fresh(
        evaluations=[evaluation()],
        rendered=[(RENDER_ID, "RENDERED", RENDERER_VERSION, FINGERPRINT, 11, 0, 0,
                   EVALUATION_ID, LEGACY_SESSION_ID)],
        perfect_results=[perfect_result(PENDING_ATTENDANCE_DATA)],
        qa_writes=[(WRITE_ID, "WRITTEN", WRITER_VERSION, LEGACY_SESSION_ID)],
        legacy_writes_state=[(WRITE_ID, RENDER_ID, EVALUATION_ID, "WRITTEN",
                              WRITER_VERSION, LEGACY_SESSION_ID, NOW)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)])
    assert state["stages"][LEGACY_QA_SYNC]["state"] == "COMPLETE"
    assert state["stages"]["PERFECT_SYNC"]["state"] == "NOT_APPLICABLE"
    # Nothing left but the attendance source itself.
    assert state["next_executable_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    assert state["executable_stage"] == ATTENDANCE
    assert state["is_waiting"] is True
    assert state["requires_review"] is False


def test_the_policy_that_gates_perfect_is_unchanged():
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION == "kbc_perfect_v2_attendance_required"


# --- 11. late attendance is a deterministic refresh, never a regeneration --------

def test_11_late_attendance_refreshes_deterministically_and_never_reruns_qa():
    state = resolve(fresh_rows(
        coverage=[(LATE_SNAPSHOT_ID, 3, 3, 3, ATTENDANCE_RESOLUTION_VERSION, 3)],
        engagement_lineage=[(LATE_ENGAGEMENT_ID, DOCUMENT_ID, LATE_SNAPSHOT_ID,
                             "CALCULATED", 3, 1, True, 2, "CALCULATED", False, NOW)],
        engagement_recovery=[(LATE_ENGAGEMENT_ID, "CALCULATED", 3, 1, 2, True,
                              "SOURCE_AVAILABLE_WITH_MEMBERS", "CALCULATED",
                              LATE_SNAPSHOT_ID, False, "attendance_coverage_v1")],
        evaluations=[evaluation()]), observations=Observations())
    qa = state["stages"][QA_EVALUATION]
    assert qa["state"] == STALE
    assert qa["action"] == REFRESH_DETERMINISTIC_QA
    assert qa["reason"] == "EVALUATION_PREDATES_CURRENT_ENGAGEMENT"
    assert state["next_executable_action"] == REFRESH_DETERMINISTIC_QA
    assert state["attendance_flag"] is None


def test_11_an_old_answer_that_claimed_zero_attendance_is_refreshed_for_free():
    """Built before attendance was optional: 0 attended / score 1 on an empty
    snapshot. The deterministic refresh re-derives those as UNKNOWN."""
    state = resolve_fresh(evaluations=[evaluation(carries_attendance=True)])
    qa = state["stages"][QA_EVALUATION]
    assert qa["action"] == REFRESH_DETERMINISTIC_QA
    assert qa["reason"] == "EVALUATION_CARRIES_UNVERIFIED_ATTENDANCE"
    assert state["next_executable_action"] != RUN_QA


# --- 16. a real QA failure is still a real QA failure --------------------------------

def test_16_invalid_evidence_still_blocks_as_real_qa_review():
    state = resolve_fresh(
        evaluations=[evaluation(status=INVALID_EVIDENCE)],
        attempts=[(FINGERPRINT, 1, 1, NOW, NOW, [INVALID_EVIDENCE], [""], ["true"],
                   ["1"], None)])
    qa = state["stages"][QA_EVALUATION]
    assert qa["state"] == REVIEW_REQUIRED
    assert state["requires_review"] is True
    # Nothing downstream of a failed answer is offered: no render, no sync.
    assert state["stages"][QA_RENDER]["action"] == RUN_QA
    assert state["next_executable_action"] == RUN_QA
    assert qa.get("reason") != "PENDING_ATTENDANCE"


# --- 10-14 through the real orchestrator: the Andrew journey ------------------------

class World:
    """
    Stand-in for the stage services: each action writes what the real service
    writes for this lecture. The RESOLVER and ORCHESTRATOR are real.
    """

    def __init__(self):
        self.connection = StubConnection(fresh_rows(
            keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)]))
        self.rows = self.connection.rows
        self.observations = Observations()
        self.calls = []
        self.provider_calls = 0
        self.attendance_arrives = False

    # the runner protocol
    def scope_of(self, action):
        return "LECTURE"

    def can_run(self, action):
        return True

    def refusal_reason(self, action):
        return "REFUSED"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append(action)
        provider_calls = getattr(self, f"_{action.lower()}")()
        return {"graph_calls": 0, "provider_calls": provider_calls or 0,
                "legacy_rows_written": 1 if action == SYNC_LEGACY_QA else 0,
                "summary": {}}

    # the stage effects
    def _run_qa(self):
        self.provider_calls += 1
        self.rows["evaluations"] = [evaluation()]
        self.rows["attempts"] = [(FINGERPRINT, 1, 1, NOW, NOW, [COMPLETED], [""],
                                  ["true"], ["1"], None)]
        return 1

    def _render_qa(self):
        current = self.rows["evaluations"][0]
        render_id = (REFRESHED_RENDER_ID if current[0] == REFRESHED_EVALUATION_ID
                     else RENDER_ID)
        self.rows["rendered"] = [(render_id, "RENDERED", RENDERER_VERSION, current[1],
                                  11, 0, 0, current[0], LEGACY_SESSION_ID)]

    def _evaluate_perfect(self):
        authoritative = self.rows["coverage"][0][0] == LATE_SNAPSHOT_ID
        self.rows["perfect_results"] = [
            perfect_result(NOT_ELIGIBLE_STATUS_NOT_ALL_MET,
                           coverage="SOURCE_AVAILABLE_WITH_MEMBERS")
            if authoritative else perfect_result(PENDING_ATTENDANCE_DATA)]

    def _sync_legacy_qa(self):
        render = self.rows["rendered"][0]
        status = "UPDATED" if self.rows["qa_writes"] else "WRITTEN"
        self.rows["qa_writes"] = [(WRITE_ID, status, WRITER_VERSION, LEGACY_SESSION_ID)]
        self.rows["legacy_writes_state"] = [(WRITE_ID, render[0], render[7], status,
                                             WRITER_VERSION, LEGACY_SESSION_ID, NOW)]
        self.observations.legacy_row = True

    def _recover_attendance(self):
        if not self.attendance_arrives:
            return  # the free probe: the source is still silent, nothing moves
        self.rows["coverage"] = [(LATE_SNAPSHOT_ID, 3, 3, 3,
                                  ATTENDANCE_RESOLUTION_VERSION, 3)]
        self.rows["engagement_lineage"] = [
            (LATE_ENGAGEMENT_ID, DOCUMENT_ID, LATE_SNAPSHOT_ID, "CALCULATED", 3, 1,
             True, 2, "CALCULATED", False, NOW)] + self.rows["engagement_lineage"]
        self.rows["engagement_recovery"] = [
            (LATE_ENGAGEMENT_ID, "CALCULATED", 3, 1, 2, True,
             "SOURCE_AVAILABLE_WITH_MEMBERS", "CALCULATED", LATE_SNAPSHOT_ID, False,
             "attendance_coverage_v1")]
        self.rows["perfect_results"] = []

    def _refresh_deterministic_qa(self):
        # Reuses the stored model answer: no provider call, same question.
        self.rows["evaluations"] = [
            evaluation(evaluation_id=REFRESHED_EVALUATION_ID, fingerprint="f" * 64,
                       engagement_id=LATE_ENGAGEMENT_ID, snapshot_id=LATE_SNAPSHOT_ID,
                       carries_attendance=True, updated_at=NOW + timedelta(hours=1)),
            *self.rows["evaluations"]]
        return 0


def run_cycle(world, run_type=RUN_TYPE_SCHEDULED):
    resolver = PipelineStateResolver(legacy_observations=world.observations)
    orchestrator = PipelineOrchestrator(resolver=resolver, runner=world,
                                        preflight=StubPreflight(), max_passes=20)
    before = len(world.calls)
    summary = orchestrator.run_window(world.connection, SESSION_DATE,
                                      run_type=run_type)
    return summary, world.calls[before:]


@pytest.mark.parametrize("run_type", [RUN_TYPE_SCHEDULED, RUN_TYPE_BACKFILL])
def test_10_to_14_attendance_missing_then_late_through_the_real_orchestrator(run_type):
    world = World()

    # Cycle 1: no attendance. QA, render, Perfect (pending) and the legacy
    # sync all happen; the only thing left is the free attendance probe.
    summary, actions = run_cycle(world, run_type)
    assert actions == [RUN_QA, RENDER_QA, EVALUATE_PERFECT, SYNC_LEGACY_QA,
                       RECOVER_ATTENDANCE]
    assert SYNC_PERFECT not in actions
    assert world.provider_calls == 1
    (lecture,) = summary["lectures"]
    assert lecture["final_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    assert world.rows["qa_writes"][0][1] == "WRITTEN"

    # Cycle 2: still no attendance. Nothing is bought, nothing is rewritten.
    _, actions = run_cycle(world, run_type)
    assert actions == [RECOVER_ATTENDANCE]
    assert world.provider_calls == 1

    # Cycle 3: attendance arrives. Deterministic refresh, re-render, update in
    # place - and not a single new generation.
    world.attendance_arrives = True
    summary, actions = run_cycle(world, run_type)
    assert actions == [RECOVER_ATTENDANCE, REFRESH_DETERMINISTIC_QA, RENDER_QA,
                       EVALUATE_PERFECT, SYNC_LEGACY_QA]
    assert RUN_QA not in actions
    assert world.provider_calls == 1
    assert world.rows["qa_writes"] == [(WRITE_ID, "UPDATED", WRITER_VERSION,
                                        LEGACY_SESSION_ID)]
    assert world.rows["legacy_writes_state"][0][1] == REFRESHED_RENDER_ID
    (lecture,) = summary["lectures"]
    assert lecture["final_action"] == NOTHING_TO_DO


def test_the_attendance_probe_still_runs_when_the_recording_link_is_missing_too():
    """A missing recording link is also a wait; it must never hide the
    attendance wait that keeps the late-attendance probe running."""
    world = World()
    run_cycle(world)
    state = PipelineStateResolver(legacy_observations=world.observations).for_lecture(
        world.connection, LECTURE_ID)
    assert state["stages"]["RECORDING_LINK"]["action"] == "WAIT_FOR_RECORDING"
    assert state["next_executable_action"] == WAIT_FOR_ATTENDANCE_SOURCE



# --- the day report: attendance is a flag, not a bucket ------------------------------

def _published_pending_state():
    return resolve_fresh(
        evaluations=[evaluation()],
        rendered=[(RENDER_ID, "RENDERED", RENDERER_VERSION, FINGERPRINT, 11, 0, 0,
                   EVALUATION_ID, LEGACY_SESSION_ID)],
        perfect_results=[perfect_result(PENDING_ATTENDANCE_DATA)],
        qa_writes=[(WRITE_ID, "WRITTEN", WRITER_VERSION, LEGACY_SESSION_ID)],
        legacy_writes_state=[(WRITE_ID, RENDER_ID, EVALUATION_ID, "WRITTEN",
                              WRITER_VERSION, LEGACY_SESSION_ID, NOW)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)])


def test_a_published_lecture_with_pending_attendance_counts_as_complete_and_flagged():
    from app.orchestration.reconciliation import DayReconciliation

    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, [_published_pending_state()])
    assert report["complete_count"] == 1
    assert report["waiting_count"] == 0 and report["review_count"] == 0
    assert report["legacy_synced_count"] == 1
    assert report["attendance_waiting_count"] == 1
    assert report["perfect_pending_attendance_count"] == 1
    (row,) = report["lectures"]
    assert row["attendance_flag"] == "PENDING_ATTENDANCE"


def test_before_its_qa_runs_an_attendance_pending_lecture_is_in_progress_not_waiting():
    from app.orchestration.reconciliation import DayReconciliation

    report = DayReconciliation(resolver=None).from_states(SESSION_DATE, [resolve_fresh()])
    assert report["in_progress_count"] == 1
    assert report["waiting_count"] == 0 and report["complete_count"] == 0


# --- an old answer that cannot be refreshed goes to a human, never a loop -------------

def test_an_unrefreshable_old_answer_is_review_not_a_refresh_loop():
    """
    The real 2026-09-17 Martech - Thur shape: COMPLETED under json_object_v1,
    carrying the empty snapshot's zero. Its answer is not reusable under the
    current contract, so the free refresh is impossible and a regeneration
    would be paid AND rewrite a published row: a human decides.
    """
    row = list(evaluation(carries_attendance=True))
    row[9] = "json_object_v1"
    state = resolve_fresh(evaluations=[tuple(row)])
    qa = state["stages"][QA_EVALUATION]
    assert qa["state"] == REVIEW_REQUIRED
    assert qa["reason"] == "UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE"
    assert state["next_executable_action"] not in (RUN_QA, REFRESH_DETERMINISTIC_QA)
    assert state["requires_review"] is True


def test_a_refresh_that_finds_no_reusable_answer_is_reported_for_review():
    from app.orchestration.runner import ManualReviewRequired, StageRunner

    class Service:
        def refresh_deterministic(self, connection, lecture_id, *, persist):
            return {"refresh_status": "NO_REUSABLE_MODEL_OUTPUT", "provider_calls": 0}

    class Runner(StageRunner):
        def _qa_service(self, *, provider, lecture_ids=None):
            assert provider is None          # a refresh can never buy a generation
            return Service()

    with pytest.raises(ManualReviewRequired, match="NO_REUSABLE_MODEL_OUTPUT"):
        Runner(settings=None).execute(None, REFRESH_DETERMINISTIC_QA,
                                      session_date=SESSION_DATE, lecture_id=LECTURE_ID)
