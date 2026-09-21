"""
Phase 3C3D unit tests: the attendance-aware Perfect Lecture policy.

Martech on 2026-09-17 was written as an 11/11 Perfect Lecture while the
platform held no attendance record for it at all. Its Item 7 was Met from the
MODEL, because the deterministic override cannot apply with no attendance -
and legacy behaves identically, so this is a policy gap, not a code defect.

v1 must keep behaving exactly as it did, because rows were written under it.
v2 adds one condition and nothing else. No database, no model.
"""
import pytest

from app.attendance.coverage import (
    SOURCE_AVAILABLE_CONFIRMED_ZERO,
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    SOURCE_PARTIAL_OR_INVALID,
    SOURCE_UNKNOWN,
)
from app.qa.checklist import CHECKLIST_ITEMS, MET
from app.qa.perfect import (
    ELIGIBLE,
    NOT_ELIGIBLE_STATUS_NOT_ALL_MET,
    PENDING_ATTENDANCE_DATA,
    PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION_V2,
    apply_attendance_policy,
    attendance_policy_required,
    evaluate,
)
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PERFECT_SUPERSEDED_NOT_PERFECT,
    PERFECT_WOULD_INSERT,
    plan_perfect_decision,
)


def rendered(**overrides):
    base = {"session_id": "SESSION-1", "meeting_id": "MEETING-1",
            "met_count": 11, "partial_count": 0, "not_met_count": 0,
            "cancelled_session": False, "render_status": "RENDERED",
            "qa_status": "COMPLETED"}
    base.update(overrides)
    return base


def items(statuses=None):
    statuses = statuses or [MET] * 11
    return [{"checklist_order": index + 1, "item": CHECKLIST_ITEMS[index],
             "status": statuses[index]} for index in range(11)]


def _judge(coverage, *, version=PERFECT_ELIGIBILITY_VERSION_V2, **overrides):
    return apply_attendance_policy(
        evaluate(rendered(**overrides), items(overrides.pop("statuses", None))),
        coverage_status=coverage, eligibility_version=version)


# --- 1. v1 is frozen ---------------------------------------------------------

@pytest.mark.parametrize("coverage", [SOURCE_MISSING, SOURCE_UNKNOWN,
                                      SOURCE_AVAILABLE_WITH_MEMBERS,
                                      SOURCE_PARTIAL_OR_INVALID])
def test_v1_ignores_attendance_coverage_entirely(coverage):
    """Every row already in production was written under this rule."""
    facts = _judge(coverage, version=PERFECT_ELIGIBILITY_VERSION)
    assert facts["is_perfect"] is True
    assert facts["reason"] == ELIGIBLE
    assert facts["attendance_pending"] is False


def test_v1_reproduces_the_martech_outcome_exactly():
    """11/11 with no attendance source: v1 said perfect, and still does."""
    facts = _judge(SOURCE_MISSING, version=PERFECT_ELIGIBILITY_VERSION)
    assert (facts["is_perfect"], facts["reason"]) == (True, ELIGIBLE)


def test_the_two_versions_are_distinct_names():
    assert PERFECT_ELIGIBILITY_VERSION == "legacy_qa_v8_perfect_v1"
    assert PERFECT_ELIGIBILITY_VERSION_V2 == "kbc_perfect_v2_attendance_required"
    assert attendance_policy_required(PERFECT_ELIGIBILITY_VERSION) is False
    assert attendance_policy_required(PERFECT_ELIGIBILITY_VERSION_V2) is True


# --- 2. v2 adds exactly one condition ---------------------------------------

def test_v2_is_eligible_when_attendance_evidence_exists():
    facts = _judge(SOURCE_AVAILABLE_WITH_MEMBERS)
    assert (facts["is_perfect"], facts["reason"]) == (True, ELIGIBLE)
    assert facts["attendance_pending"] is False


def test_v2_is_eligible_on_an_authoritative_zero():
    """A source that says "nobody came" has answered; the rule then applies."""
    facts = _judge(SOURCE_AVAILABLE_CONFIRMED_ZERO)
    assert (facts["is_perfect"], facts["reason"]) == (True, ELIGIBLE)


@pytest.mark.parametrize("coverage", [SOURCE_MISSING, SOURCE_UNKNOWN,
                                      SOURCE_PARTIAL_OR_INVALID])
def test_v2_holds_a_perfect_lecture_when_the_source_never_answered(coverage):
    facts = _judge(coverage)
    assert facts["is_perfect"] is False
    assert facts["reason"] == PENDING_ATTENDANCE_DATA
    assert facts["attendance_pending"] is True


def test_pending_is_non_final_and_keeps_the_v1_answer_visible():
    """"Cannot decide yet" must never be mistaken for "decided: no"."""
    facts = _judge(SOURCE_MISSING)
    assert facts["base_is_perfect"] is True
    assert facts["base_reason"] == ELIGIBLE
    assert facts["eligibility_version"] == PERFECT_ELIGIBILITY_VERSION_V2


def test_a_non_perfect_lecture_keeps_its_ordinary_reason_under_v2():
    """Missing attendance is not an extra way to FAIL, only a way to WAIT."""
    statuses = [MET] * 10 + ["Partially Met"]
    facts = apply_attendance_policy(
        evaluate(rendered(met_count=10, partial_count=1), items(statuses)),
        coverage_status=SOURCE_MISSING,
        eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
    assert facts["reason"] == NOT_ELIGIBLE_STATUS_NOT_ALL_MET
    assert facts["attendance_pending"] is False


def test_attendance_recovery_flips_pending_to_eligible_with_no_recomputation():
    """The same frozen render, judged again once the source arrives."""
    frozen = evaluate(rendered(), items())
    before = apply_attendance_policy(
        frozen, coverage_status=SOURCE_MISSING,
        eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
    after = apply_attendance_policy(
        frozen, coverage_status=SOURCE_AVAILABLE_WITH_MEMBERS,
        eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
    assert before["reason"] == PENDING_ATTENDANCE_DATA
    assert after["reason"] == ELIGIBLE
    # And nothing about the underlying QA answer changed.
    assert before["met_count"] == after["met_count"] == 11
    assert frozen["reason"] == ELIGIBLE


def test_apply_attendance_policy_does_not_mutate_its_input():
    frozen = evaluate(rendered(), items())
    snapshot = dict(frozen)
    apply_attendance_policy(frozen, coverage_status=SOURCE_MISSING,
                            eligibility_version=PERFECT_ELIGIBILITY_VERSION_V2)
    assert frozen == snapshot


# --- 3. the writer decision that follows ------------------------------------

def _decide(**overrides):
    base = dict(mode=DRY_RUN, render_status="RENDERED", qa_status="COMPLETED",
                payload_valid=True, is_perfect=False, target_exists=False,
                coded_owned=False, foreign_session_on_key=False,
                fingerprint_matches=False, attendance_pending=True)
    base.update(overrides)
    return plan_perfect_decision(**base)


def test_pending_never_inserts_a_legacy_row():
    assert _decide(mode=CANARY_NEW_ONLY, is_perfect=True) == \
        PERFECT_PENDING_ATTENDANCE_DATA


def test_pending_never_supersedes_a_row_this_platform_owns():
    """
    The dangerous branch. `not is_perfect` + owned means SUPERSEDED, which
    marks the ownership record. A pending state must not do that: the answer
    is unknown, not negative.
    """
    assert _decide(target_exists=True, coded_owned=True) == \
        PERFECT_PENDING_ATTENDANCE_DATA
    # Without the pending flag, the same inputs do supersede.
    assert _decide(target_exists=True, coded_owned=True,
                   attendance_pending=False) == PERFECT_SUPERSEDED_NOT_PERFECT


def test_pending_is_not_an_actionable_decision():
    from app.writer.modes import PERFECT_ACTIONABLE_DECISIONS, PERFECT_DECISIONS
    assert PERFECT_PENDING_ATTENDANCE_DATA not in PERFECT_ACTIONABLE_DECISIONS
    assert PERFECT_PENDING_ATTENDANCE_DATA in PERFECT_DECISIONS


def test_without_the_pending_flag_nothing_about_the_decision_changed():
    assert _decide(is_perfect=True, attendance_pending=False) == PERFECT_WOULD_INSERT
    assert _decide(is_perfect=False, attendance_pending=False) == PERFECT_NOT_ELIGIBLE


def test_a_blocking_condition_still_outranks_pending():
    """Not-ready and collisions are refusals; pending is a wait."""
    from app.writer.modes import (PERFECT_BLOCKED_KEY_COLLISION,
                                  PERFECT_BLOCKED_NOT_READY)
    assert _decide(qa_status="REVIEW_REQUIRED") == PERFECT_BLOCKED_NOT_READY
    assert _decide(foreign_session_on_key=True) == PERFECT_BLOCKED_KEY_COLLISION
