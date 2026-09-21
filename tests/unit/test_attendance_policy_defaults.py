"""
Phase 3C3E unit tests: the finalized attendance policy and its defaults.

Two things are being pinned. First, that the attendance-aware Perfect policy is
now what NEW work gets without asking - a default nobody has to remember is the
only kind that survives unattended operation. Second, that the `Attendance = 1`
blind spot found this phase is represented honestly: the source having spoken
only in absences is neither silence nor a confirmed zero.

No database, no provider.
"""
import pytest

from app.attendance.coverage import (
    ALL_PRESENT_ROWS_FILTERED,
    ALL_SOURCE_ROWS_MARKED_ABSENT,
    SOURCE_AVAILABLE_CONFIRMED_ZERO,
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    SOURCE_PARTIAL_OR_INVALID,
    SOURCE_UNKNOWN,
    classify,
    confirms_zero_attendance,
    is_authoritative,
    partial_reason,
    provenance,
)
from app.qa.inputs import qa_model_input_fingerprint, qa_source_fingerprint
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION_V2,
    attendance_policy_required,
)
from app.qa.structured_output import STRICT_JSON_SCHEMA
from app.qa.punctuality import CANONICAL_CUE_BOUNDS
from app.writer.modes import DRY_RUN
from app.writer.perfect_service import PerfectLecturePlanner

from tests.unit.test_shadow_qa import package


# --- 1. the default processing contract --------------------------------------

def test_the_default_perfect_policy_is_the_attendance_aware_one():
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION == PERFECT_ELIGIBILITY_VERSION_V2
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION == "kbc_perfect_v2_attendance_required"


def test_a_planner_built_without_asking_gets_the_attendance_aware_policy():
    planner = PerfectLecturePlanner(result_repository=object(),
                                    ownership_repository=object(),
                                    legacy_repository=object(), mode=DRY_RUN)
    assert planner.eligibility_version == PERFECT_ELIGIBILITY_VERSION_V2
    assert attendance_policy_required(planner.eligibility_version) is True


def test_the_default_policy_always_has_a_coverage_reader():
    """
    A policy that needs evidence must never be constructible without the means
    to read it. Omitting the reader supplies one rather than downgrading the
    policy - the only way to decide without attendance evidence is to ask for
    v1 by name.
    """
    planner = PerfectLecturePlanner(result_repository=object(),
                                    ownership_repository=object(),
                                    legacy_repository=object(), mode=DRY_RUN)
    assert planner.coverage_repository is not None
    assert hasattr(planner.coverage_repository, "for_lecture")


def test_the_legacy_policy_is_still_selectable_for_historical_reproduction():
    planner = PerfectLecturePlanner(
        result_repository=object(), ownership_repository=object(),
        legacy_repository=object(), mode=DRY_RUN,
        eligibility_version=PERFECT_ELIGIBILITY_VERSION)
    assert planner.eligibility_version == "legacy_qa_v8_perfect_v1"
    assert attendance_policy_required(planner.eligibility_version) is False


def test_the_cli_default_matches_the_service_default():
    """A default that is true in one layer and not the other is not a default."""
    from app.cli.main import build_parser
    parser = build_parser()
    for command in ("write-legacy-qa", "evaluate-perfect-lecture"):
        action = next(item for item in parser._subparsers._group_actions[0]
                      .choices[command]._actions
                      if item.dest == "perfect_policy")
        assert action.default == DEFAULT_PERFECT_ELIGIBILITY_VERSION
        assert PERFECT_ELIGIBILITY_VERSION in action.choices


def test_the_rest_of_the_default_processing_contract_is_unchanged():
    from app.qa.service import ShadowQaService
    from app.attendance.coverage import ATTENDANCE_COVERAGE_VERSION
    from app.qa.structured_output import DEFAULT_PROVIDER_CONTRACT
    from app.qa.punctuality import DEFAULT_PUNCTUALITY_SOURCE
    assert DEFAULT_PROVIDER_CONTRACT == STRICT_JSON_SCHEMA
    assert ATTENDANCE_COVERAGE_VERSION == "attendance_coverage_v1"
    # The QA service defaults, which are what NEW processing actually gets.
    import inspect
    defaults = inspect.signature(ShadowQaService.__init__).parameters
    assert defaults["punctuality_source_version"].default == CANONICAL_CUE_BOUNDS
    assert defaults["provider_contract_version"].default == STRICT_JSON_SCHEMA
    # The legacy source keeps its name and stays selectable.
    assert DEFAULT_PUNCTUALITY_SOURCE == "legacy_call_bounds_v1"


# --- 2. the `Attendance = 1` blind spot --------------------------------------

def test_silence_and_recorded_absence_are_different_facts():
    silent = classify(source_row_count=0, present_row_count=0,
                      effective_member_count=0, source_rows_any_status=0)
    absences = classify(source_row_count=0, present_row_count=0,
                        effective_member_count=0, source_rows_any_status=1)
    assert silent == SOURCE_MISSING
    assert absences == SOURCE_PARTIAL_OR_INVALID
    assert silent != absences


def test_recorded_absences_are_still_not_a_confirmed_zero():
    """
    The G2 Keith case. One row, marked absent. That only becomes "nobody
    attended" if the rows are known to cover everyone who could have attended,
    and knowing that means cross-referencing enrolment - inference, not
    evidence.
    """
    status = classify(source_row_count=0, present_row_count=0,
                      effective_member_count=0, source_rows_any_status=1)
    assert status != SOURCE_AVAILABLE_CONFIRMED_ZERO
    assert is_authoritative(status) is False
    assert confirms_zero_attendance(status, 0) is False


def test_the_two_partial_causes_stay_distinguishable():
    assert partial_reason(source_row_count=0, present_row_count=0,
                          source_rows_any_status=3) == ALL_SOURCE_ROWS_MARKED_ABSENT
    assert partial_reason(source_row_count=4, present_row_count=4,
                          source_rows_any_status=4) == ALL_PRESENT_ROWS_FILTERED


def test_an_old_snapshot_without_the_unfiltered_count_stays_missing():
    """
    Under-claiming what the source said is safe; over-claiming is not. A
    snapshot frozen before this phase carries no unfiltered count, so it keeps
    reading as SOURCE_MISSING rather than being reinterpreted.
    """
    assert classify(source_row_count=0, present_row_count=0,
                    effective_member_count=0) == SOURCE_MISSING
    assert classify(source_row_count=0, present_row_count=0,
                    effective_member_count=0,
                    source_rows_any_status=None) == SOURCE_MISSING


def test_the_unfiltered_count_never_upgrades_a_real_roster():
    assert classify(source_row_count=7, present_row_count=7,
                    effective_member_count=7,
                    source_rows_any_status=9) == SOURCE_AVAILABLE_WITH_MEMBERS


def test_provenance_carries_the_unfiltered_count_and_the_reason():
    record = provenance(status=SOURCE_PARTIAL_OR_INVALID, source_row_count=0,
                        present_row_count=0, effective_member_count=0,
                        source_rows_any_status=1)
    assert record["attendance_source_rows_any_status"] == 1
    assert record["attendance_coverage_partial_reason"] == ALL_SOURCE_ROWS_MARKED_ABSENT
    assert record["attendance_source_authoritative"] is False


@pytest.mark.parametrize("status", [SOURCE_MISSING, SOURCE_PARTIAL_OR_INVALID,
                                    SOURCE_UNKNOWN])
def test_no_non_authoritative_state_can_confirm_zero_attendance(status):
    assert confirms_zero_attendance(status, 0) is False


# --- 3. attendance never reaches the provider --------------------------------

def test_attendance_is_absent_from_the_model_input_fingerprint():
    """
    The fact that licenses free recovery. If attendance could change what the
    provider was asked, the frozen answer would not be reusable and every
    arrival would cost a generation.
    """
    before = package()
    after = package(attended_count=13, spoke_count=1,
                    engagement_percentage="7.69", engagement_score=1,
                    learner_engagement_status="Not Met",
                    item7_override_applied=True,
                    engagement_id="different", attendance_snapshot_id="different",
                    engagement_source_fingerprint="f" * 64)
    assert (qa_model_input_fingerprint(package=before, model="gpt-5.2")
            == qa_model_input_fingerprint(package=after, model="gpt-5.2"))


def test_attendance_does_change_the_full_qa_source_fingerprint():
    """The evaluation is still new provenance; only the model answer is reused."""
    before = package()
    after = package(engagement_id="different", attendance_snapshot_id="different",
                    engagement_source_fingerprint="f" * 64)
    assert (qa_source_fingerprint(package=before, model="gpt-5.2")
            != qa_source_fingerprint(package=after, model="gpt-5.2"))


@pytest.mark.parametrize("field,value", [
    ("duration_minutes", 90),
    ("combined_content_sha256", "e" * 64),
    ("document_source_fingerprint", "e" * 64),
    ("start_difference_minutes", 42),
    ("subject", "Something Else"),
])
def test_anything_the_provider_actually_saw_does_move_the_model_fingerprint(field, value):
    base = package()
    changed = package(**{field: value})
    assert (qa_model_input_fingerprint(package=base, model="gpt-5.2")
            != qa_model_input_fingerprint(package=changed, model="gpt-5.2"))


def test_the_provider_contract_is_part_of_the_model_fingerprint():
    base = package()
    assert (qa_model_input_fingerprint(package=base, model="gpt-5.2",
                                       provider_contract_version="json_object_v1")
            != qa_model_input_fingerprint(package=base, model="gpt-5.2",
                                          provider_contract_version=STRICT_JSON_SCHEMA))
