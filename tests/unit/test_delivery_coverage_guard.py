"""
delivery_coverage_guard_v1: a short transcript is not, on its own, proof that
a lecture was not delivered.

The production shape this exists for is "AI in Project Control 2026",
2026-09-04: scheduled 08:00-10:00 UTC, a Teams call from 07:58:37 to 10:50:16
UTC, and a single well-formed transcript whose cues run 00:01:22-00:14:14. The
legacy gate read 13 minutes and wrote the cancelled checklist. The call proves
the lecture ran; the transcript cannot support QA; the truthful answer is a
review. The fixtures below use those exact instants and no transcript text.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.qa.deterministic import DELIVERED, NON_DELIVERED
from app.qa.delivery import (
    CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT,
    DELIVERY_POLICY_VERSION,
    DURATION_BELOW_DELIVERY_MINIMUM,
    TRANSCRIPT_COVERAGE_INCOMPLETE,
    TRANSCRIPT_MEETS_DELIVERY_MINIMUM,
    classify_delivery,
)
from app.qa.inputs import qa_source_fingerprint
from tests.unit.test_shadow_qa import (
    TARGET,
    StubEvaluations,
    StubProvider,
    good_output,
    package,
    service,
)


UTC = timezone.utc

# AI in Project Control 2026, 2026-09-04 (real instants, no content).
AIPC_START = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
AIPC_END = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
AIPC_CALL_START = datetime(2026, 9, 4, 7, 58, 37, 216753, tzinfo=UTC)
AIPC_CALL_END = datetime(2026, 9, 4, 10, 50, 16, 296753, tzinfo=UTC)
AIPC_TRANSCRIPT_SECONDS = 772.389
# The same lecture's other call: 1m19s the previous evening, no transcript.
AIPC_CALL_A_START = datetime(2026, 9, 3, 17, 50, 24, tzinfo=UTC)
AIPC_CALL_A_END = datetime(2026, 9, 3, 17, 51, 43, tzinfo=UTC)

# Andrew-Scheduling Professional (SP) Jan 2026, 2026-09-16 (real instants).
ANDREW_START = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)
ANDREW_END = datetime(2026, 9, 16, 13, 0, tzinfo=UTC)
ANDREW_CALL_START = datetime(2026, 9, 16, 8, 13, 41, tzinfo=UTC)
ANDREW_CALL_END = datetime(2026, 9, 16, 13, 0, 27, tzinfo=UTC)


def aipc():
    return classify_delivery(
        duration_minutes=13, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_CALL_START, call_end=AIPC_CALL_END,
        transcript_span_seconds=AIPC_TRANSCRIPT_SECONDS)


# --- the policy -----------------------------------------------------------------

def test_1_normal_delivered_lecture_takes_the_normal_path():
    decision = classify_delivery(
        duration_minutes=120, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_START, call_end=AIPC_END)
    assert decision.classification == DELIVERED
    assert decision.reason == TRANSCRIPT_MEETS_DELIVERY_MINIMUM
    assert decision.departs_from_legacy is False


def test_2_real_non_delivery_stays_non_delivered():
    """Short transcript AND a short call: nothing says the lecture ran."""
    decision = classify_delivery(
        duration_minutes=5, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_START, call_end=AIPC_START + timedelta(minutes=6))
    assert decision.classification == NON_DELIVERED
    assert decision.reason == DURATION_BELOW_DELIVERY_MINIMUM


def test_2b_no_independent_call_evidence_stays_non_delivered():
    decision = classify_delivery(
        duration_minutes=5, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=None, call_end=None)
    assert decision.classification == NON_DELIVERED


def test_2c_a_long_call_entirely_outside_the_scheduled_window_proves_nothing():
    """A long call is evidence only where it overlaps the scheduled lecture."""
    decision = classify_delivery(
        duration_minutes=3, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_START - timedelta(hours=5),
        call_end=AIPC_START - timedelta(hours=1))
    assert decision.classification == NON_DELIVERED
    assert decision.diagnostics["call_overlap_with_schedule_seconds"] == 0


def test_2d_the_previous_evening_test_call_is_not_evidence_for_the_lecture():
    decision = classify_delivery(
        duration_minutes=1, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_CALL_A_START, call_end=AIPC_CALL_A_END)
    assert decision.classification == NON_DELIVERED


def test_3_ai_project_control_shape_is_coverage_incomplete():
    decision = aipc()
    assert decision.classification == TRANSCRIPT_COVERAGE_INCOMPLETE
    assert decision.reason == CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT
    assert decision.departs_from_legacy is True
    diagnostics = decision.diagnostics
    assert diagnostics["delivery_policy_version"] == DELIVERY_POLICY_VERSION
    assert diagnostics["scheduled_duration_seconds"] == 7200
    assert diagnostics["call_overlap_with_schedule_seconds"] == 7200
    assert diagnostics["call_schedule_coverage_ratio"] == 1.0
    assert diagnostics["call_duration_seconds"] == pytest.approx(10299.08, abs=0.01)
    assert diagnostics["transcript_span_seconds"] == AIPC_TRANSCRIPT_SECONDS
    assert diagnostics["transcript_schedule_coverage_ratio"] == pytest.approx(0.1073, abs=1e-4)
    assert diagnostics["delivery_classification_reason"] == \
        CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT


def test_3b_the_threshold_is_the_existing_delivery_minimum_not_a_new_one():
    """Exactly 20 minutes of call inside the window is enough; 19 is not."""
    def with_overlap(minutes):
        return classify_delivery(
            duration_minutes=2, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
            call_start=AIPC_END - timedelta(minutes=minutes),
            call_end=AIPC_END + timedelta(hours=1)).classification

    assert with_overlap(20) == TRANSCRIPT_COVERAGE_INCOMPLETE
    assert with_overlap(19) == NON_DELIVERED


def test_4_a_call_opened_hours_early_around_a_full_lecture_is_normal():
    decision = classify_delivery(
        duration_minutes=118, scheduled_start=AIPC_START, scheduled_end=AIPC_END,
        call_start=AIPC_START - timedelta(hours=4), call_end=AIPC_END)
    assert decision.classification == DELIVERED
    # Whole-call coverage would be 118/360; the scheduled window is the reference.
    assert decision.diagnostics["transcript_schedule_coverage_ratio"] == \
        pytest.approx(118 / 120, abs=1e-4)


def test_5_andrew_multipart_is_not_flagged_as_incomplete():
    """A 287-minute call around a 119-minute lecture is a valid lecture."""
    decision = classify_delivery(
        duration_minutes=119, scheduled_start=ANDREW_START, scheduled_end=ANDREW_END,
        call_start=ANDREW_CALL_START, call_end=ANDREW_CALL_END,
        transcript_span_seconds=7168.597)
    assert decision.classification == DELIVERED
    assert decision.departs_from_legacy is False


# --- the QA service -------------------------------------------------------------

def aipc_package(**overrides):
    """The QA package shape, moved onto the fixture's 09:00-11:00 schedule."""
    start = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    return package(duration_minutes=13, duration_seconds=AIPC_TRANSCRIPT_SECONDS,
                   actual_start=start - timedelta(seconds=83),
                   actual_end=start + timedelta(hours=2, minutes=50),
                   first_cue_start_ms=82_040, last_cue_end_ms=854_429, **overrides)


def test_3c_ai_project_control_shape_reaches_review_with_zero_model_calls():
    provider = StubProvider(good_output())
    evaluations = StubEvaluations()
    summary = service([aipc_package()], provider, evaluations).run_day(
        None, TARGET, execute=True)

    assert provider.calls == 0
    assert summary["provider_calls"] == 0
    assert summary["non_delivered_count"] == 0
    assert summary["coverage_incomplete_count"] == 1
    lecture = summary["lectures"][0]
    assert lecture["qa_status"] == "REVIEW_REQUIRED"
    assert lecture["review_reason"] == TRANSCRIPT_COVERAGE_INCOMPLETE

    (stored,) = evaluations.rows.values()
    evaluation = stored["evaluation"]
    assert evaluation["qa_status"] == "REVIEW_REQUIRED"
    assert evaluation["review_reason"] == TRANSCRIPT_COVERAGE_INCOMPLETE
    # The lecture ran; it is not cancelled and is not scored.
    assert evaluation["delivery_status"] == DELIVERED
    assert evaluation["cancelled_session"] is False
    assert evaluation["ai_called"] is False
    assert evaluation["met_count"] is None and evaluation["not_met_count"] is None
    assert stored["checklist"] == [] and stored["clips"] == []
    metadata = evaluation["metadata"]
    assert metadata["delivery_policy_version"] == DELIVERY_POLICY_VERSION
    assert metadata["delivery_classification_reason"] == \
        CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT
    for key in ("scheduled_duration_seconds", "call_duration_seconds",
                "call_overlap_with_schedule_seconds", "call_schedule_coverage_ratio",
                "transcript_span_seconds", "transcript_schedule_coverage_ratio"):
        assert key in metadata["delivery"], key
    # No transcript content anywhere in what was persisted.
    assert "combined_content" not in str(metadata)


def classified(row):
    """Attach the decision exactly as the service does, after punctuality."""
    qa = service([row])
    qa._apply_punctuality_source(row)
    qa._classify_delivery(row)
    return row


def test_3d_a_reclassified_lecture_gets_new_provenance_beside_the_old_answer():
    """
    The NON_DELIVERED row written under the legacy gate must stay on record, so
    the review cannot be upserted over it: its fingerprint must differ.
    """
    reclassified = classified(aipc_package())
    legacy = classified(aipc_package())
    legacy.pop("delivery")
    kwargs = {"model": "gpt-5.2"}
    assert "delivery" in reclassified
    assert qa_source_fingerprint(package=reclassified, **kwargs) != \
        qa_source_fingerprint(package=legacy, **kwargs)


def test_3e_legacy_equivalent_decisions_keep_their_exact_fingerprint():
    """Nothing already paid for is orphaned by the new policy."""
    delivered = classified(package())
    assert delivered["delivery"].classification == DELIVERED
    bare = classified(package())
    bare.pop("delivery")
    assert qa_source_fingerprint(package=delivered, model="gpt-5.2") == \
        qa_source_fingerprint(package=bare, model="gpt-5.2")


def test_1b_normal_delivered_lecture_calls_the_model_once():
    provider = StubProvider(good_output())
    summary = service([package()], provider, StubEvaluations()).run_day(
        None, TARGET, execute=True)
    assert provider.calls == 1
    assert summary["delivered_count"] == 1
    assert summary["lectures"][0]["qa_status"] == "COMPLETED"


def test_14_rerunning_the_review_reuses_it_and_buys_nothing():
    provider = StubProvider(good_output())
    evaluations = StubEvaluations()
    service([aipc_package()], provider, evaluations).run_day(None, TARGET, execute=True)
    writes = evaluations.writes
    again = service([aipc_package()], provider, evaluations).run_day(
        None, TARGET, execute=True)
    assert provider.calls == 0
    assert evaluations.writes == writes
    assert again["reused_evaluations"] == 1
    assert again["lectures"][0]["review_reason"] == TRANSCRIPT_COVERAGE_INCOMPLETE


def test_the_renderer_refuses_a_coverage_review():
    """No legacy row can be rendered from an evaluation that has no verdict."""
    from app.rendering.service import RENDERABLE_QA_STATUSES
    assert "REVIEW_REQUIRED" not in RENDERABLE_QA_STATUSES
