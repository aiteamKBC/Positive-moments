"""
RELEASE GATE: the attendance-optional QA contract on a SYNTHETIC,
PRODUCTION-SHAPED acceptance fixture.

This module is the executable home of the `production_data` assertions that
the attendance-optional contract invalidated (test_orchestration_persistence
and test_automated_production_sync). Those modules assert KBC's real history
and cannot run in the gate; the SAME contract statements run here, on a
deterministic, versioned fixture that encodes every shape the read-only
2026-09-01..24 September audit found - with no production data, no personal
data and nothing that can drift:

  stephen      authoritative attendance, learners spoke      -> complete
  juliane      authoritative attendance, nobody spoke        -> a REAL zero,
                                                                kept as zero
  andrew       attendance never answered, no QA yet          -> QA, render,
               (Ray MSP 09-18, Andrew 09-23)                    coded row with
                                                                NULL engagement
  g2_keith_sp  attendance never answered; published BEFORE    -> free
               the fix under strict_json_schema_v1 with the     deterministic
               empty snapshot's 0 / score 1 on a CODED row      refresh, same
               (G2 Keith Strategy & Planning 09-17)             row -> NULLs
  martech      the same, but bought under json_object_v1      -> review, row
               (Martech - Thur 09-17)                            untouched
  g2_keith_ci  attendance never answered; a FOREIGN (n8n)     -> QA runs, the
               row with 0 / score 1 at the lecture's id          foreign row is
               (G2 Keith Commercial Intelligence 09-02)          never touched

Everything runs through the real orchestrator (see
test_late_attendance_orchestrator for what is and is not stubbed); the
historical shapes are then applied exactly as they exist in production.

Superseded production_data assertion -> its contract statement here:
  resolver_describes_the_real_pilot_day ("2 waiting")      -> test_no_lecture_waits...
  two_waiting_lectures_are_waiting_on_attendance...       -> test_no_lecture_waits...
  real_run_on_the_waiting_day_costs_no_provider_call      -> test_a_cycle_over_the_day...
  waiting_lectures_are_still_waiting_after_a_run...       -> test_a_cycle_over_the_day...
  day_report... attendance_waiting / waiting_count         -> test_the_day_report...
  operations_layer... next_action / review == []           -> test_the_operations_layer...
  waiting_lecture_is_not_offered_as_a_retry               -> test_the_operations_layer...
  attendance_waiting_lecture_still_waits_and_buys_nothing -> test_no_lecture_waits...

Every connection is rolled back.
"""
from __future__ import annotations

import pytest

from app.orchestration.operations import OperationsService
from app.orchestration.reconciliation import DayReconciliation, bucket_for
from app.orchestration.stages import (
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    REFRESH_DETERMINISTIC_QA,
    RUN_QA,
    WAIT_FOR_ATTENDANCE_SOURCE,
)
from app.qa.inputs import ATTENDANCE_PENDING
from tests.integration.seeding import DAY, seed_attendance_source, seed_legacy_row
from tests.integration.test_late_attendance_orchestrator import (  # noqa: F401 - fixtures
    ATTENDEES,
    CountingProvider,
    _checklist,
    _commit_point,
    _discovered_lecture,
    _evaluations,
    _orchestrator,
    _sessions,
    db,
    provider,
)

SHAPES = ("stephen", "juliane", "andrew", "g2_keith_sp", "martech", "g2_keith_ci")
PENDING = {"andrew", "g2_keith_sp", "martech", "g2_keith_ci"}


def _foreign_row(db, session_id):
    return db.execute('SELECT "Engagement", engagement_score, met_count, trainer, updated_at'
                      "  FROM public.qa_doctors_sessions WHERE session_id = %s",
                      (session_id,)).fetchone()


def _age_into_prefix_false_zero(db, lecture, contract):
    """
    What a pre-fix run left in production: the empty snapshot's zeros stated
    as fact on the evaluation AND on the published coded row, under the
    pre-fix source fingerprint - which had no attendance-policy line, so the
    fix's deterministic refresh derives a NEW fingerprint and a new evaluation
    from it, exactly as the read-only dry-run did on the real G2 Keith row.
    Attempt history moves with the fingerprint it is keyed on.
    """
    ((session_id, *_),) = _sessions(db, lecture)
    for table in ("lecture_qa_generation_attempts", "lecture_qa_evaluations"):
        db.execute(f"UPDATE public.{table}"
                   "   SET source_fingerprint = encode(sha256(convert_to("
                   "       'pre-attendance-optional:' || source_fingerprint, 'UTF8')), 'hex')"
                   " WHERE lecture_id = %s", (lecture["lecture_id"],))
    db.execute("""
        UPDATE public.lecture_qa_evaluations
           SET attended_count = 0, engagement_percentage = 0.00, engagement_score = 1,
               metadata = (metadata - 'attendance_flag')
                          || jsonb_build_object('provider_contract_version', %s::text)
         WHERE lecture_id = %s""", (contract, lecture["lecture_id"]))
    db.execute('UPDATE public.qa_doctors_sessions SET "Engagement" = 0, engagement_score = 1'
               " WHERE session_id = %s", (session_id,))
    return session_id


@pytest.fixture
def september(db, provider):
    """The acceptance day, after its first cycle and the historical ageing."""
    resolver, orchestrator = _orchestrator()
    day = {}
    for n, shape in enumerate(SHAPES, start=40):
        speakers = () if shape == "juliane" else ATTENDEES[:2]
        lecture = _discovered_lecture(db, n, learner_speakers=speakers)
        subject = f"Acceptance {shape}"
        db.execute("UPDATE public.lecture_sessions SET subject = %s, module = %s,"
                   " normalized_subject = %s WHERE lecture_id = %s",
                   (subject, subject, subject.lower(), lecture["lecture_id"]))
        day[shape] = {**lecture, "subject": subject}
    seed_attendance_source(db, day["stephen"], present=3)
    seed_attendance_source(db, day["juliane"], present=3)
    foreign_id = day["g2_keith_ci"]["transcript_id"]
    seed_legacy_row(db, foreign_id, meeting_id=day["g2_keith_ci"]["meeting_id"],
                    Engagement=0, engagement_score=1)

    calls_before = provider.calls_total
    summary = orchestrator.run_window(db, DAY, lecture_ids=[
        str(lecture["lecture_id"]) for lecture in day.values()])
    assert provider.calls_total - calls_before == len(SHAPES)       # one answer each
    assert not [item for item in summary["lectures"] if item["error_code"]]

    sessions = {"g2_keith_sp": _age_into_prefix_false_zero(
                    db, day["g2_keith_sp"], "strict_json_schema_v1"),
                "martech": _age_into_prefix_false_zero(
                    db, day["martech"], "json_object_v1")}
    _commit_point(db)
    foreign_before = _foreign_row(db, foreign_id)
    assert foreign_before[:3] == (0, 1, 9)        # n8n's false zero, untouched by cycle 1
    return {"db": db, "resolver": resolver, "orchestrator": orchestrator, "day": day,
            "sessions": sessions, "foreign_id": foreign_id,
            "foreign_before": foreign_before}


def _states(september):
    resolver, db = september["resolver"], september["db"]
    return {shape: resolver.for_lecture(db, str(lecture["lecture_id"]))
            for shape, lecture in september["day"].items()}


# ===========================================================================

def test_no_lecture_waits_on_attendance_and_each_pending_one_is_flagged(september):
    db, day = september["db"], september["day"]
    states = _states(september)
    actions = {shape: state["next_executable_action"] for shape, state in states.items()}
    assert actions == {
        "stephen": NOTHING_TO_DO,
        "juliane": NOTHING_TO_DO,
        # QA done and published; the lecture only watches for the enrichment.
        "andrew": WAIT_FOR_ATTENDANCE_SOURCE,
        "g2_keith_ci": WAIT_FOR_ATTENDANCE_SOURCE,
        "g2_keith_sp": REFRESH_DETERMINISTIC_QA,
        "martech": MANUAL_REVIEW_REQUIRED,
    }, actions
    buckets = {shape: bucket_for(state) for shape, state in states.items()}
    assert "waiting" not in buckets.values()
    assert buckets == {"stephen": "complete", "juliane": "complete", "andrew": "complete",
                       "g2_keith_ci": "complete", "g2_keith_sp": "in_progress",
                       "martech": "review"}

    for shape, state in states.items():
        if shape in PENDING:
            assert state["attendance_flag"] == ATTENDANCE_PENDING, shape
            assert state["attendance_source_authoritative"] is False
            assert state["stages"]["ATTENDANCE"]["state"] == "WAITING"
            assert state["stages"]["ATTENDANCE"]["blocks_qa"] is False
            for stage in ("TRANSCRIPT", "SELECTION", "CANONICAL_CUES", "SPEAKERS",
                          "ENGAGEMENT", "QA_RENDER"):
                assert state["stages"][stage]["state"] == "COMPLETE", (shape, stage)
            assert state["stages"]["PERFECT_ELIGIBILITY"]["reason"] == \
                "PENDING_ATTENDANCE_DATA"
        else:
            assert state["attendance_flag"] is None, shape
            assert state["attendance_source_authoritative"] is True

    # UNKNOWN is not ZERO - and a real zero is not UNKNOWN.
    assert _sessions(db, day["andrew"])[0][1:3] == (None, None)
    assert _sessions(db, day["juliane"])[0][1:3] == (0, 1)
    assert _sessions(db, day["stephen"])[0][1] > 0


def test_a_cycle_over_the_day_buys_nothing_and_touches_only_the_stale_coded_row(september):
    db, day, sessions = september["db"], september["day"], september["sessions"]
    before = {shape: _sessions(db, lecture) for shape, lecture in day.items()}
    checklists = {shape: _checklist(db, before[shape][0][0])
                  for shape in ("g2_keith_sp", "martech")}
    evaluations = {shape: len(_evaluations(db, lecture)) for shape, lecture in day.items()}
    calls_before = CountingProvider.calls_total

    summary = september["orchestrator"].run_window(db, DAY, lecture_ids=[
        str(lecture["lecture_id"]) for lecture in day.values()])
    assert CountingProvider.calls_total == calls_before              # 0 provider calls
    assert summary["provider_calls"] == 0 and summary["graph_calls"] == 0
    items = {next(shape for shape, lecture in day.items()
                  if str(lecture["lecture_id"]) == item["lecture_id"]): item
             for item in summary["lectures"]}
    assert not [item for item in items.values() if RUN_QA in item["actions"]]

    # G2 Keith S&P: the false zero becomes UNKNOWN on the SAME row, free.
    assert REFRESH_DETERMINISTIC_QA in items["g2_keith_sp"]["actions"]
    after = _sessions(db, day["g2_keith_sp"])
    assert len(after) == 1 and after[0][0] == sessions["g2_keith_sp"]
    assert after[0][1:3] == (None, None)
    assert after[0][3:] == before["g2_keith_sp"][0][3:]                  # 11 / 0 / 0
    assert _checklist(db, sessions["g2_keith_sp"]) == checklists["g2_keith_sp"]
    assert evaluations["g2_keith_sp"] + 1 == len(_evaluations(db, day["g2_keith_sp"]))

    # Martech: not refreshable, not regenerated, not rewritten.
    assert items["martech"]["final_action"] == MANUAL_REVIEW_REQUIRED
    assert _sessions(db, day["martech"]) == before["martech"]
    assert _checklist(db, sessions["martech"]) == checklists["martech"]

    # Everything else: unchanged, and the foreign row byte-for-byte.
    for shape in ("stephen", "juliane", "andrew", "g2_keith_ci"):
        assert _sessions(db, day[shape]) == before[shape], shape
        assert len(_evaluations(db, day[shape])) == evaluations[shape], shape
    assert _foreign_row(db, september["foreign_id"]) == september["foreign_before"]

    states = _states(september)
    assert bucket_for(states["g2_keith_sp"]) == "complete"
    assert states["g2_keith_sp"]["attendance_flag"] == ATTENDANCE_PENDING


def test_the_day_report_counts_flags_not_waits(september):
    report = DayReconciliation(resolver=september["resolver"]).for_day(
        september["db"], DAY)
    assert report["canonical_lecture_count"] == len(SHAPES)
    assert report["waiting_count"] == 0
    assert report["complete_count"] == 4
    assert report["in_progress_count"] == 1                       # G2 Keith S&P refresh
    assert report["review_count"] == 1                            # Martech
    assert report["failed_count"] == 0
    assert (report["complete_count"] + report["waiting_count"] + report["review_count"]
            + report["failed_count"] + report["in_progress_count"]) == len(SHAPES)
    # The attendance stage itself still says what is true of the source.
    assert report["attendance_waiting_count"] == len(PENDING)
    assert report["perfect_pending_attendance_count"] == len(PENDING)
    flagged = {row["subject"] for row in report["lectures"]
               if row["attendance_flag"] == ATTENDANCE_PENDING}
    assert flagged == {f"Acceptance {shape}" for shape in PENDING}


def test_the_operations_layer_offers_the_refresh_and_reviews_the_old_contract(september):
    db, day = september["db"], september["day"]
    operations = OperationsService(resolver=september["resolver"])
    pending = operations.pending_attendance(db, DAY)
    review = operations.review_required(db, DAY)
    assert {row["subject"] for row in pending} == {f"Acceptance {s}" for s in PENDING}
    assert [row["subject"] for row in review] == ["Acceptance martech"]

    keith = operations.lecture_stage_matrix(db, str(day["g2_keith_sp"]["lecture_id"]))
    assert keith["stages"]["ATTENDANCE"]["state"] == "WAITING"
    assert keith["next_executable_action"] == REFRESH_DETERMINISTIC_QA
    assert keith["retry_eligibility"]["retry_eligible"] is True
    assert keith["force_reprocess_eligibility"]["available_to_scheduler"] is False

    martech = operations.lecture_stage_matrix(db, str(day["martech"]["lecture_id"]))
    assert martech["retry_eligibility"]["retry_eligible"] is False
    assert martech["retry_eligibility"]["retry_reason"] == \
        "UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE"
    assert martech["force_reprocess_eligibility"]["available_to_scheduler"] is False

    andrew = operations.lecture_stage_matrix(db, str(day["andrew"]["lecture_id"]))
    assert andrew["retry_eligibility"]["retry_eligible"] is False
    assert andrew["retry_eligibility"]["retry_reason"] == "WAITING_ON_EXTERNAL_SOURCE"
