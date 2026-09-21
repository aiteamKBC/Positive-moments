"""
Phase 4B unit tests: which legacy writes a scheduler may perform alone.

The gate's whole job is to be narrow, so most of these tests are about what it
REFUSES. A safety list that only gets tested on its happy path is a safety list
nobody has checked.
"""
import inspect

import pytest

from app.orchestration.runner import (
    LEGACY_WRITE_ACTIONS,
    ManualReviewRequired,
    StageRunner,
)
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    OPERATOR_ONLY_ACTIONS,
    PRODUCTION_WRITE_ACTIONS,
    STAGE_ORDER,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
)
from app.orchestration.sync_safety import (
    AUTO_SYNC_POLICY_VERSION,
    PERFECT_AUTO_APPROVED,
    QA_AUTO_APPROVED,
    UNKNOWN_DECISION,
    classify_perfect,
    classify_plan,
    classify_qa,
)
from app.writer.modes import (
    BLOCKED_BACKFILL_NOT_AUTHORISED,
    BLOCKED_INVALID_PAYLOAD,
    BLOCKED_NOT_READY,
    PERFECT_BLOCKED_KEY_COLLISION,
    PERFECT_BLOCKED_NOT_READY,
    PERFECT_DECISIONS,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_SUPERSEDED_NOT_PERFECT,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_WOULD_UPDATE,
    PROTECTED_EXISTING_LEGACY_ROW,
    REVIEW_REQUIRED,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
)


def _plan(decision, perfect=None):
    plan = {"decision": decision}
    if perfect is not None:
        plan["perfect_lecture"] = {"perfect_decision": perfect}
    return plan


# --- 1, 5. the safe QA set ----------------------------------------------------

def test_a_clean_insert_is_the_one_qa_decision_that_writes():
    verdict = classify_qa(WOULD_INSERT)
    assert verdict["auto_approved"] is True
    assert verdict["action"] == "WRITE"
    assert verdict["reason_code"] is None


def test_an_identical_coded_owned_target_is_a_noop_not_a_write():
    verdict = classify_qa(WOULD_SKIP_IDENTICAL)
    assert verdict["auto_approved"] is True
    assert verdict["action"] == "NOOP"
    assert classify_plan(_plan(WOULD_SKIP_IDENTICAL))["may_write"] is False


def test_the_safe_qa_set_is_exactly_two_decisions():
    assert QA_AUTO_APPROVED == {WOULD_INSERT, WOULD_SKIP_IDENTICAL}


# --- 3, 4. the refusals --------------------------------------------------------

@pytest.mark.parametrize("decision,reason", [
    (WOULD_UPDATE, "LEGACY_ROW_WOULD_BE_UPDATED"),
    (PROTECTED_EXISTING_LEGACY_ROW, "LEGACY_ROW_NOT_CODED_OWNED"),
    (BLOCKED_NOT_READY, "SOURCE_NOT_READY"),
    (BLOCKED_INVALID_PAYLOAD, "PAYLOAD_INVARIANT_FAILED"),
    (BLOCKED_BACKFILL_NOT_AUTHORISED, "BACKFILL_NOT_AUTHORISED"),
    (REVIEW_REQUIRED, "WRITER_REQUIRES_REVIEW"),
])
def test_every_other_qa_decision_reaches_a_human_with_the_writers_own_reason(
        decision, reason):
    verdict = classify_qa(decision)
    assert verdict["auto_approved"] is False
    assert verdict["action"] == "MANUAL_REVIEW_REQUIRED"
    assert verdict["reason_code"] == reason
    assert classify_plan(_plan(decision))["may_write"] is False


def test_an_unrecognised_qa_decision_is_never_auto_approved():
    verdict = classify_qa("SOMETHING_NOBODY_HAS_CLASSIFIED_YET")
    assert verdict["auto_approved"] is False
    assert verdict["reason_code"] == UNKNOWN_DECISION


def test_a_missing_decision_is_not_permission():
    assert classify_qa(None)["auto_approved"] is False


# --- 8, 9, 10, 11. Perfect ------------------------------------------------------

def test_an_eligible_lecture_with_no_legacy_row_may_be_synced():
    verdict = classify_perfect(PERFECT_WOULD_INSERT)
    assert verdict["auto_approved"] is True
    assert verdict["action"] == "WRITE"


def test_pending_attendance_waits_and_writes_nothing():
    verdict = classify_perfect(PERFECT_PENDING_ATTENDANCE_DATA)
    assert verdict["auto_approved"] is True
    assert verdict["action"] == "WAITING"
    assert classify_plan(_plan(WOULD_SKIP_IDENTICAL,
                              PERFECT_PENDING_ATTENDANCE_DATA)
                         )["may_write_perfect"] is False


def test_a_lecture_that_is_not_perfect_syncs_nothing_and_needs_no_review():
    verdict = classify_perfect(PERFECT_NOT_ELIGIBLE)
    assert verdict["auto_approved"] is True
    assert verdict["action"] == "NOTHING_TO_SYNC"
    assert classify_plan(_plan(WOULD_INSERT, PERFECT_NOT_ELIGIBLE)
                         )["requires_manual_review"] is False


@pytest.mark.parametrize("decision,reason", [
    (PERFECT_WOULD_UPDATE, "PERFECT_ROW_WOULD_BE_UPDATED"),
    (PERFECT_BLOCKED_KEY_COLLISION, "PERFECT_LECTURE_KEY_COLLISION"),
    (PERFECT_PROTECTED_EXISTING_LEGACY_ROW, "PERFECT_ROW_NOT_CODED_OWNED"),
    (PERFECT_SUPERSEDED_NOT_PERFECT, "PERFECT_ROW_SUPERSEDED_NOT_PERFECT"),
    (PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS, "PERFECT_ROW_EXISTS_BUT_NOT_ELIGIBLE"),
    (PERFECT_BLOCKED_NOT_READY, "PERFECT_SOURCE_NOT_READY"),
])
def test_every_ambiguous_perfect_decision_reaches_a_human(decision, reason):
    verdict = classify_perfect(decision)
    assert verdict["auto_approved"] is False
    assert verdict["reason_code"] == reason


def test_every_writer_perfect_decision_is_classified_one_way_or_the_other():
    """No decision may fall through the gate unexamined."""
    for decision in PERFECT_DECISIONS:
        verdict = classify_perfect(decision)
        assert verdict["reason_code"] != UNKNOWN_DECISION, decision


def test_an_unsafe_perfect_does_not_block_a_safe_qa_insert():
    """
    Two legacy targets with separate lifecycles - which is exactly why Phase
    3C2.3D planned them separately. A Perfect key collision is a real problem
    and it is reported, but the QA row is correct and independent.
    """
    verdict = classify_plan(_plan(WOULD_INSERT, PERFECT_BLOCKED_KEY_COLLISION))
    assert verdict["may_write"] is True
    assert verdict["may_write_qa"] is True
    assert verdict["requires_manual_review"] is True
    assert verdict["reason_codes"] == ["PERFECT_LECTURE_KEY_COLLISION"]


def test_an_unsafe_perfect_is_kept_out_of_the_write_pass_entirely():
    """
    `PERFECT_WOULD_UPDATE` is ACTIONABLE to the writer and would be performed
    under PRODUCTION_NEW_ONLY. Leaving the planner out of the write pass is
    what actually prevents it - not trusting the writer to decline something it
    is entitled to do.
    """
    unsafe = classify_plan(_plan(WOULD_INSERT, PERFECT_WOULD_UPDATE))
    assert unsafe["include_perfect_planner"] is False
    safe = classify_plan(_plan(WOULD_INSERT, PERFECT_WOULD_INSERT))
    assert safe["include_perfect_planner"] is True


def test_a_perfect_only_sync_still_runs_when_qa_is_already_identical():
    verdict = classify_plan(_plan(WOULD_SKIP_IDENTICAL, PERFECT_WOULD_INSERT))
    assert verdict["may_write_qa"] is False
    assert verdict["may_write_perfect"] is True
    assert verdict["may_write"] is True


def test_nothing_to_do_at_all_is_not_a_write():
    verdict = classify_plan(_plan(WOULD_SKIP_IDENTICAL,
                                  PERFECT_WOULD_SKIP_IDENTICAL))
    assert verdict["may_write"] is False
    assert verdict["requires_manual_review"] is False


def test_the_gate_names_its_own_version():
    assert classify_plan(_plan(WOULD_INSERT))["auto_sync_policy_version"] == \
        AUTO_SYNC_POLICY_VERSION == "automated_legacy_sync_v1"


def test_the_safe_perfect_set_contains_no_update_and_no_protected_state():
    assert PERFECT_WOULD_UPDATE not in PERFECT_AUTO_APPROVED
    assert PERFECT_PROTECTED_EXISTING_LEGACY_ROW not in PERFECT_AUTO_APPROVED
    assert PERFECT_BLOCKED_KEY_COLLISION not in PERFECT_AUTO_APPROVED


# --- 2. the dry run is not optional ---------------------------------------------

def test_the_runner_always_plans_under_dry_run_before_writing():
    """
    Structural: the write pass is unreachable without a DRY_RUN plan and a
    classification of it first.
    """
    source = inspect.getsource(StageRunner._sync_legacy)
    plan_at = source.index("mode=DRY_RUN")
    classify_at = source.index("classify_plan")
    write_at = source.index("PRODUCTION_NEW_ONLY")
    assert plan_at < classify_at < write_at


def test_the_runner_never_authorises_an_update():
    """`allow_update_existing` is False on every writer the scheduler builds."""
    source = inspect.getsource(StageRunner._writer)
    assert "allow_update_existing=False" in source
    assert "EXPLICIT_BACKFILL" not in source


def test_the_writer_the_scheduler_builds_is_always_lecture_scoped():
    source = inspect.getsource(StageRunner._writer)
    assert "lecture_ids=[str(lecture_id)]" in source
    # Two of them: the QA writer and the Perfect planner.
    assert source.count("lecture_ids=[str(lecture_id)]") == 2


def test_no_date_wide_legacy_writer_exists_anywhere_in_orchestration():
    import pathlib
    for path in sorted(pathlib.Path("app/orchestration").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "LegacyQaWriter(" in source:
            assert "lecture_ids=[str(lecture_id)]" in source, path.name


# --- the action model ------------------------------------------------------------

def test_the_two_syncs_are_automatable_and_still_flagged_as_production_writes():
    assert PRODUCTION_WRITE_ACTIONS == {SYNC_LEGACY_QA, SYNC_PERFECT}
    assert PRODUCTION_WRITE_ACTIONS <= AUTOMATABLE_ACTIONS
    assert PRODUCTION_WRITE_ACTIONS == LEGACY_WRITE_ACTIONS
    assert OPERATOR_ONLY_ACTIONS == frozenset()


def test_perfect_eligibility_is_decided_before_both_writes_and_sync_after_qa():
    assert (STAGE_ORDER.index("PERFECT_ELIGIBILITY")
            < STAGE_ORDER.index("LEGACY_QA_SYNC")
            < STAGE_ORDER.index("PERFECT_SYNC"))


def test_manual_review_is_a_distinct_outcome_from_a_failure():
    assert issubclass(ManualReviewRequired, RuntimeError)
    assert ManualReviewRequired is not Exception
