"""
Phase 3C3D unit tests: attendance SOURCE coverage is a separate fact from the
attendance COUNT.

The shape these pin down came from real data. On 2026-09-17 three real
lectures produced three different situations and only one of them was a
finding:

    Stephen     4 present rows          -> the source answered, with members
    G2 Keith    no rows at all          -> the source did not answer
    Martech     no rows at all          -> the source did not answer

All three used to arrive downstream as a number, and two of those numbers were
zero. No database, no external source read.
"""
import pytest

from app.attendance.coverage import (
    ATTENDANCE_COVERAGE_VERSION,
    ATTENDANCE_SOURCE_MISSING,
    AUTHORITATIVE_STATUSES,
    COVERAGE_STATUSES,
    SOURCE_AVAILABLE_CONFIRMED_ZERO,
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    SOURCE_PARTIAL_OR_INVALID,
    SOURCE_UNKNOWN,
    classify,
    confirms_zero_attendance,
    is_authoritative,
    provenance,
)


# --- 1. the four situations the snapshot counts can describe -----------------

def test_rows_with_surviving_members_is_the_ordinary_case():
    assert classify(source_row_count=9, present_row_count=7,
                    effective_member_count=7) == SOURCE_AVAILABLE_WITH_MEMBERS


def test_no_source_rows_at_all_is_missing_not_zero():
    """G2 Keith and Martech, 2026-09-17."""
    assert classify(source_row_count=0, present_row_count=0,
                    effective_member_count=0) == SOURCE_MISSING


def test_rows_that_all_say_absent_is_the_only_authoritative_zero():
    """The source described the lecture and recorded nobody present."""
    assert classify(source_row_count=6, present_row_count=0,
                    effective_member_count=0) == SOURCE_AVAILABLE_CONFIRMED_ZERO


def test_rows_present_but_all_filtered_away_is_partial_not_confirmed():
    """Every present row was a bot or nameless: the roster cannot be trusted."""
    assert classify(source_row_count=4, present_row_count=4,
                    effective_member_count=0) == SOURCE_PARTIAL_OR_INVALID


def test_a_missing_snapshot_is_unknown_and_never_a_zero():
    assert classify(source_row_count=None, present_row_count=None,
                    effective_member_count=None) == SOURCE_UNKNOWN


# --- 2. only a real answer is authoritative ---------------------------------

@pytest.mark.parametrize("status", [SOURCE_AVAILABLE_WITH_MEMBERS,
                                    SOURCE_AVAILABLE_CONFIRMED_ZERO])
def test_the_source_answered(status):
    assert is_authoritative(status) is True


@pytest.mark.parametrize("status", [SOURCE_MISSING, SOURCE_PARTIAL_OR_INVALID,
                                    SOURCE_UNKNOWN])
def test_the_source_did_not_answer(status):
    assert is_authoritative(status) is False


def test_authoritative_set_is_exactly_the_two_answered_states():
    assert AUTHORITATIVE_STATUSES == frozenset(
        {SOURCE_AVAILABLE_WITH_MEMBERS, SOURCE_AVAILABLE_CONFIRMED_ZERO})
    assert set(AUTHORITATIVE_STATUSES) < set(COVERAGE_STATUSES)


# --- 3. the predicate downstream must use -----------------------------------

def test_zero_from_a_missing_source_is_not_a_confirmed_zero():
    """The whole ambiguity, in one assertion."""
    assert confirms_zero_attendance(SOURCE_MISSING, 0) is False


def test_zero_from_an_answering_source_is_a_confirmed_zero():
    assert confirms_zero_attendance(SOURCE_AVAILABLE_CONFIRMED_ZERO, 0) is True


def test_a_nonzero_count_is_never_a_confirmed_zero():
    assert confirms_zero_attendance(SOURCE_AVAILABLE_WITH_MEMBERS, 4) is False


def test_partial_coverage_cannot_confirm_zero_either():
    assert confirms_zero_attendance(SOURCE_PARTIAL_OR_INVALID, 0) is False
    assert confirms_zero_attendance(SOURCE_UNKNOWN, 0) is False


def test_the_two_facts_are_genuinely_independent():
    """Same count, opposite meanings - which is why the count cannot carry it."""
    assert (confirms_zero_attendance(SOURCE_AVAILABLE_CONFIRMED_ZERO, 0)
            != confirms_zero_attendance(SOURCE_MISSING, 0))


# --- 4. provenance is printable, versioned and free of personal data --------

def test_provenance_carries_the_counts_the_status_was_derived_from():
    record = provenance(status=SOURCE_MISSING, source_row_count=0,
                        present_row_count=0, effective_member_count=0,
                        snapshot_id="11111111-1111-1111-1111-111111111111")
    assert record["attendance_coverage_status"] == SOURCE_MISSING
    assert record["attendance_coverage_version"] == ATTENDANCE_COVERAGE_VERSION
    assert record["attendance_source_authoritative"] is False
    assert record["attendance_source_row_count"] == 0
    assert record["attendance_snapshot_id"].startswith("1111")


def test_provenance_is_json_safe_and_carries_no_learner_identity():
    import json
    record = provenance(status=SOURCE_AVAILABLE_WITH_MEMBERS, source_row_count=9,
                        present_row_count=7, effective_member_count=7)
    printed = json.dumps(record)
    assert "@" not in printed
    assert record["attendance_snapshot_id"] is None


def test_the_coverage_version_is_pinned():
    assert ATTENDANCE_COVERAGE_VERSION == "attendance_coverage_v1"
    assert ATTENDANCE_SOURCE_MISSING == "ATTENDANCE_SOURCE_MISSING"
