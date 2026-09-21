"""
Phase 3C3B unit tests: the versioned Item 2 punctuality source.

The pilot showed Item 2 measures the Teams CALL, not the lecture. Starting
early always passes, so the two early-opening calls on 2026-09-16 were scored
correctly - but the mirror case is a call that opens early and hides a
genuinely late lecture. These tests pin the new source's behaviour on exactly
those shapes, with no database and no model.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.qa.deterministic import MET, NOT_MET, item2_status
from app.qa.punctuality import (
    CANONICAL_CUE_BOUNDS,
    DEFAULT_PUNCTUALITY_SOURCE,
    LEGACY_CALL_BOUNDS,
    PUNCTUALITY_SOURCE_VERSIONS,
    PunctualitySourceError,
    derive_bounds,
    difference_minutes,
    js_round,
    punctuality_provenance,
)


SCHEDULED_START = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
SCHEDULED_END = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
MINUTE = 60_000


def _statuses(*, call_start, call_end, first_ms, last_ms):
    """Both sources' Item 2 verdicts for one shape."""
    out = {}
    for source in PUNCTUALITY_SOURCE_VERSIONS:
        start, end = derive_bounds(call_start=call_start, call_end=call_end,
                                   first_cue_start_ms=first_ms, last_cue_end_ms=last_ms,
                                   source_version=source)
        out[source] = (difference_minutes(start, SCHEDULED_START),
                       difference_minutes(end, SCHEDULED_END),
                       item2_status(difference_minutes(start, SCHEDULED_START),
                                    difference_minutes(end, SCHEDULED_END)))
    return out


# --------------------------------------------------------------------------
# 14-16: origins
# --------------------------------------------------------------------------

def test_a_normal_call_origin_agrees_between_both_sources():
    """Call opens a minute before, speech starts immediately: no disagreement."""
    out = _statuses(call_start=SCHEDULED_START - timedelta(minutes=1),
                    call_end=SCHEDULED_END,
                    first_ms=1 * MINUTE, last_ms=120 * MINUTE)
    assert out[LEGACY_CALL_BOUNDS][2] == MET
    assert out[CANONICAL_CUE_BOUNDS][2] == MET
    assert out[CANONICAL_CUE_BOUNDS][0] == 0


def test_a_long_running_reused_call_is_corrected_by_the_cue_source():
    """
    Andrew's real shape: the call had been open for 2h46m before the lecture.
    The call source reports -166; the cue source reports the truth, +1.
    """
    out = _statuses(call_start=SCHEDULED_START - timedelta(minutes=166),
                    call_end=SCHEDULED_END,
                    first_ms=167 * MINUTE, last_ms=286 * MINUTE)
    assert out[LEGACY_CALL_BOUNDS][0] == -166
    assert out[CANONICAL_CUE_BOUNDS][0] == 1
    # Both Met - the correction matters for provenance, not this outcome.
    assert out[LEGACY_CALL_BOUNDS][2] == MET
    assert out[CANONICAL_CUE_BOUNDS][2] == MET


def test_a_non_zero_canonical_origin_is_honoured_not_rebased():
    """The cue origin is added to the call start; it is never subtracted away."""
    call_start = SCHEDULED_START - timedelta(minutes=166)
    start, end = derive_bounds(call_start=call_start, call_end=SCHEDULED_END,
                               first_cue_start_ms=10_026_775, last_cue_end_ms=17_195_372,
                               source_version=CANONICAL_CUE_BOUNDS)
    assert start == call_start + timedelta(milliseconds=10_026_775)
    assert end == call_start + timedelta(milliseconds=17_195_372)


# --------------------------------------------------------------------------
# 17-19: the cases that change an outcome
# --------------------------------------------------------------------------

def test_a_lecture_starting_more_than_twenty_minutes_late_is_not_met():
    out = _statuses(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                    first_ms=25 * MINUTE, last_ms=120 * MINUTE)
    assert out[CANONICAL_CUE_BOUNDS][0] == 25
    assert out[CANONICAL_CUE_BOUNDS][2] == NOT_MET


def test_a_lecture_ending_more_than_twenty_minutes_early_is_not_met():
    out = _statuses(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                    first_ms=0, last_ms=90 * MINUTE)
    assert out[CANONICAL_CUE_BOUNDS][1] == -30
    assert out[CANONICAL_CUE_BOUNDS][2] == NOT_MET


def test_an_early_call_masking_a_late_lecture_is_exactly_what_this_fixes():
    """
    The defect, in one test. The call opened 40 minutes early and teaching
    began 35 minutes late. The call source sees -40 and passes; the cue source
    sees +35 and correctly fails.
    """
    out = _statuses(call_start=SCHEDULED_START - timedelta(minutes=40),
                    call_end=SCHEDULED_END + timedelta(minutes=10),
                    first_ms=75 * MINUTE, last_ms=160 * MINUTE)
    assert out[LEGACY_CALL_BOUNDS][0] == -40
    assert out[LEGACY_CALL_BOUNDS][2] == MET, "the old source masks it"
    assert out[CANONICAL_CUE_BOUNDS][0] == 35
    assert out[CANONICAL_CUE_BOUNDS][2] == NOT_MET, "the new source catches it"


def test_a_call_left_open_after_teaching_ends_no_longer_hides_an_early_finish():
    out = _statuses(call_start=SCHEDULED_START, call_end=SCHEDULED_END + timedelta(minutes=50),
                    first_ms=0, last_ms=85 * MINUTE)
    assert out[LEGACY_CALL_BOUNDS][1] == 50
    assert out[LEGACY_CALL_BOUNDS][2] == MET
    assert out[CANONICAL_CUE_BOUNDS][1] == -35
    assert out[CANONICAL_CUE_BOUNDS][2] == NOT_MET


def test_a_cue_starting_before_the_scheduled_start_is_simply_early():
    out = _statuses(call_start=SCHEDULED_START - timedelta(minutes=10),
                    call_end=SCHEDULED_END, first_ms=2 * MINUTE, last_ms=125 * MINUTE)
    assert out[CANONICAL_CUE_BOUNDS][0] == -8
    assert out[CANONICAL_CUE_BOUNDS][2] == MET


def test_initial_silence_is_measured_as_a_late_start_not_ignored():
    """
    Deliberate: the first surviving cue IS the start. A long silent lead-in
    reads as a late start, which is what a learner experienced. No heuristic
    tries to guess whether the silence was "really" the lecture.
    """
    out = _statuses(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                    first_ms=22 * MINUTE, last_ms=120 * MINUTE)
    assert out[CANONICAL_CUE_BOUNDS][2] == NOT_MET


def test_a_very_short_administrative_first_cue_still_counts_as_the_start():
    """No minimum-duration filter: filtering would be a guess, and guesses need AI."""
    out = _statuses(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                    first_ms=60_000, last_ms=118 * MINUTE)
    assert out[CANONICAL_CUE_BOUNDS][0] == 1
    assert out[CANONICAL_CUE_BOUNDS][2] == MET


# --------------------------------------------------------------------------
# 20: multi-part
# --------------------------------------------------------------------------

def test_multi_part_v2_bounds_use_the_first_parts_call_start():
    """
    Andrew's v2 document: part-two cues already carry their offset against part
    one's call, and `actual_start` is min(part.start). So the same conversion
    holds with no special case.
    """
    call_start = SCHEDULED_START - timedelta(minutes=166)
    start, end = derive_bounds(call_start=call_start, call_end=SCHEDULED_END,
                               first_cue_start_ms=10_026_775,
                               last_cue_end_ms=17_195_372,
                               source_version=CANONICAL_CUE_BOUNDS)
    assert difference_minutes(start, SCHEDULED_START) == 1
    # 17,195,372 ms after a call that opened 166 minutes early lands 35 seconds
    # past this synthetic two-hour schedule, which rounds to +1.
    assert difference_minutes(end, SCHEDULED_END) == 1
    assert end - start == timedelta(milliseconds=17_195_372 - 10_026_775)


# --------------------------------------------------------------------------
# 21-22: provenance, refusals and non-rewriting
# --------------------------------------------------------------------------

def test_missing_cue_data_is_refused_not_silently_downgraded():
    with pytest.raises(PunctualitySourceError):
        derive_bounds(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                      first_cue_start_ms=None, last_cue_end_ms=100,
                      source_version=CANONICAL_CUE_BOUNDS)
    with pytest.raises(PunctualitySourceError):
        derive_bounds(call_start=None, call_end=SCHEDULED_END,
                      first_cue_start_ms=0, last_cue_end_ms=100,
                      source_version=CANONICAL_CUE_BOUNDS)
    with pytest.raises(PunctualitySourceError):
        derive_bounds(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                      first_cue_start_ms=500, last_cue_end_ms=100,
                      source_version=CANONICAL_CUE_BOUNDS)


def test_an_unknown_source_version_is_refused():
    with pytest.raises(PunctualitySourceError):
        derive_bounds(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                      source_version="something_invented")


def test_the_legacy_source_is_preserved_by_name_for_reproducibility():
    assert LEGACY_CALL_BOUNDS == "legacy_call_bounds_v1"
    assert DEFAULT_PUNCTUALITY_SOURCE == LEGACY_CALL_BOUNDS
    start, end = derive_bounds(call_start=SCHEDULED_START, call_end=SCHEDULED_END,
                               first_cue_start_ms=99 * MINUTE, last_cue_end_ms=99 * MINUTE,
                               source_version=LEGACY_CALL_BOUNDS)
    # Cue data present but deliberately ignored by the legacy source.
    assert (start, end) == (SCHEDULED_START, SCHEDULED_END)


def test_the_provenance_record_carries_everything_needed_to_re_derive():
    call_start = SCHEDULED_START - timedelta(minutes=40)
    start, end = derive_bounds(call_start=call_start, call_end=SCHEDULED_END,
                               first_cue_start_ms=75 * MINUTE, last_cue_end_ms=160 * MINUTE,
                               source_version=CANONICAL_CUE_BOUNDS)
    record = punctuality_provenance(
        source_version=CANONICAL_CUE_BOUNDS, scheduled_start=SCHEDULED_START,
        scheduled_end=SCHEDULED_END, actual_start=start, actual_end=end)
    assert record["punctuality_source_version"] == CANONICAL_CUE_BOUNDS
    for field in ("scheduled_start", "scheduled_end", "derived_actual_start",
                  "derived_actual_end", "start_difference_minutes",
                  "end_difference_minutes"):
        assert record[field] is not None, field
    assert record["start_difference_minutes"] == 35


def test_rounding_matches_the_legacy_node():
    assert js_round(0.5) == 1
    assert js_round(-0.5) == 0
    assert js_round(1.5) == 2
    assert js_round(2.5) == 3          # Python's round() would give 2
    assert difference_minutes(None, SCHEDULED_START) is None


def test_the_qa_service_binds_the_new_source_and_refuses_an_unknown_one():
    from app.qa.service import ShadowQaService
    from app.qa.inputs import REQUIRED_ATTENDANCE_ROSTER_VERSION

    def build(**overrides):
        kwargs = dict(
            input_repository=None, evaluation_repository=None, run_repository=None,
            attendance_roster_version=REQUIRED_ATTENDANCE_ROSTER_VERSION,
            resolver_version="r", role_algorithm_version="ro",
            engagement_algorithm_version="e", model_name="gpt-5.2")
        kwargs.update(overrides)
        return ShadowQaService(**kwargs)

    assert build().punctuality_source_version == CANONICAL_CUE_BOUNDS
    assert build(punctuality_source_version=LEGACY_CALL_BOUNDS
                 ).punctuality_source_version == LEGACY_CALL_BOUNDS
    from app.qa.service import QaInputError
    with pytest.raises(QaInputError):
        build(punctuality_source_version="not_a_source")


def test_the_source_version_participates_in_the_qa_source_fingerprint():
    from app.qa.inputs import qa_source_fingerprint
    package = {
        "lecture_id": "l", "selection_id": "s", "combined_source_fingerprint": "c",
        "combined_content_sha256": "h", "document_id": "d",
        "document_source_fingerprint": "df", "scheduled_start": "a", "scheduled_end": "b",
        "actual_start": "x", "actual_end": "y", "start_difference_minutes": 1,
        "end_difference_minutes": 2, "duration_minutes": 120,
        "canonical_trainer_speaker_id": "t", "attendance_snapshot_id": "sn",
        "engagement_id": "e", "engagement_source_fingerprint": "ef",
    }
    legacy = qa_source_fingerprint(
        package={**package, "punctuality_source_version": LEGACY_CALL_BOUNDS},
        model="gpt-5.2")
    cue = qa_source_fingerprint(
        package={**package, "punctuality_source_version": CANONICAL_CUE_BOUNDS},
        model="gpt-5.2")
    assert legacy != cue
