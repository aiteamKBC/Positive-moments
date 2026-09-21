"""
Phase 6B: Positive Clip planning.

The measured shapes are real. `FEMI` is the Phase 6B pilot lecture, a
single-part July recording whose legacy timestamps were proven to be media
timestamps by audio alignment. `ANDREW` is the multipart September case, kept
here so a clip planner can never quietly acquire the assumption that a lecture
lives in one file.
"""
import pytest

from app.media.coordinates import MEDIA_COORDINATE_VERSION, MediaTimeline, RecordingPart
from app.media.positive_clips import (
    CROSSES_RECORDING_BOUNDARY,
    INVALID_MOMENT,
    MOMENT_OUTSIDE_MEDIA,
    MOMENT_TOO_LONG,
    POSITIVE_CLIP_PLANNER_VERSION,
    ClipPlanRefused,
    ClipPolicy,
    PositiveMoment,
    clip_key,
    job_key,
    plan_clip,
    plan_lecture,
)


def part(index, offset, duration):
    return RecordingPart(part_index=index, canonical_offset_seconds=offset,
                         media_duration_seconds=duration,
                         recording_id=f"rec-{index}", user_id="user",
                         meeting_id="meeting")


# G3 - Femi - Customer Journey Optimisation: one recording, 8783.0s of media,
# transcript 234.4s..8236.1s, first moment at 00:04:14.320.
FEMI = MediaTimeline([part(1, 0.0, 8783.0)])
FEMI_SESSION = "session-femi"

# Andrew-Scheduling: two recordings, overlapping.
ANDREW = MediaTimeline([part(1, 0.0, 14400.064), part(2, 14336.082, 2870.336)])


def moment(index=1, start=254.32, end=275.0, start_cue=41, end_cue=45):
    return PositiveMoment(clip_index=index, start_seconds=start, end_seconds=end,
                          start_cue=start_cue, end_cue=end_cue,
                          speaker="Kelly Chan", category="learning_experience",
                          quote="It was good.")


# --- the identity case, which must stay the identity ------------------------

def test_a_july_single_part_lecture_maps_straight_through():
    """
    The pilot. Its timeline is one part at offset zero, so the validated
    transform reduces to the identity ON ITS OWN. If this ever stops being
    true, a second coordinate model has been introduced somewhere.
    """
    plan = plan_clip(FEMI, FEMI_SESSION, moment())
    assert plan.media_start_seconds == pytest.approx(plan.canonical_start_seconds)
    assert plan.media_end_seconds == pytest.approx(plan.canonical_end_seconds)
    assert plan.part_index == 1
    assert plan.recording_id == "rec-1"


def test_the_established_padding_is_applied():
    plan = plan_clip(FEMI, FEMI_SESSION, moment())
    assert plan.media_start_seconds == pytest.approx(254.32 - 60)
    assert plan.media_end_seconds == pytest.approx(275.0 + 60)
    assert plan.applied_padding_before_seconds == pytest.approx(60.0)
    assert plan.applied_padding_after_seconds == pytest.approx(60.0)
    assert plan.duration_seconds == pytest.approx(140.68)


def test_precise_is_the_default_cut_mode():
    """
    A stream copy lands on the nearest keyframe. For a clip built around one
    sentence that can move the boundary by seconds and cut the sentence in
    half, so the default re-encodes.
    """
    assert plan_clip(FEMI, FEMI_SESSION, moment()).cut_mode == "precise"


# --- padding is bounded at BOTH ends ----------------------------------------

def test_padding_is_clamped_at_the_start_of_the_recording():
    plan = plan_clip(FEMI, FEMI_SESSION, moment(start=20.0, end=40.0))
    assert plan.media_start_seconds == pytest.approx(0.0)
    assert plan.applied_padding_before_seconds == pytest.approx(20.0)
    # The moment itself is untouched; only the courtesy padding shrank.
    assert plan.media_end_seconds == pytest.approx(100.0)


def test_padding_is_clamped_at_the_end_of_the_recording():
    """
    The bug the measured duration fixes. The previous SQL producer added 60s to
    the end unconditionally, because it had no recording length to clamp
    against, and asked FFmpeg for media past the end of the file.
    """
    plan = plan_clip(FEMI, FEMI_SESSION, moment(start=8700.0, end=8750.0))
    assert plan.media_end_seconds == pytest.approx(8783.0)
    assert plan.applied_padding_after_seconds == pytest.approx(33.0)
    assert plan.media_end_seconds <= 8783.0


def test_a_clip_never_asks_for_media_that_does_not_exist():
    for start in (100.0, 4000.0, 8700.0, 8780.0):
        plan = plan_clip(FEMI, FEMI_SESSION, moment(start=start, end=start + 15))
        assert plan.media_start_seconds >= 0.0
        assert plan.media_end_seconds <= 8783.0 + 0.001


# --- refusals ---------------------------------------------------------------

def test_a_moment_past_the_end_of_the_media_is_refused():
    with pytest.raises(ClipPlanRefused) as caught:
        plan_clip(FEMI, FEMI_SESSION, moment(start=9000.0, end=9020.0))
    assert caught.value.code == MOMENT_OUTSIDE_MEDIA


def test_an_inverted_moment_is_refused():
    with pytest.raises(ClipPlanRefused) as caught:
        plan_clip(FEMI, FEMI_SESSION, moment(start=500.0, end=400.0))
    assert caught.value.code == INVALID_MOMENT


def test_a_moment_without_cue_identity_is_refused():
    """
    Without cues there is no stable clip key, and without a stable key a
    re-run produces a second clip of the same moment.
    """
    with pytest.raises(ClipPlanRefused) as caught:
        plan_clip(FEMI, FEMI_SESSION, moment(start_cue=None))
    assert caught.value.code == INVALID_MOMENT


def test_an_absurdly_long_moment_is_refused():
    with pytest.raises(ClipPlanRefused) as caught:
        plan_clip(FEMI, FEMI_SESSION, moment(start=100.0, end=2000.0))
    assert caught.value.code == MOMENT_TOO_LONG


def test_a_clip_spanning_two_recordings_is_refused_not_truncated():
    """
    The media worker takes one source URL and one range. Cutting only the first
    segment would deliver a clip that stops mid-sentence and looks fine.
    """
    with pytest.raises(ClipPlanRefused) as caught:
        plan_clip(ANDREW, "session-andrew", moment(start=14330.0, end=14345.0))
    assert caught.value.code == CROSSES_RECORDING_BOUNDARY


# --- the multipart case -----------------------------------------------------

def test_a_moment_in_the_second_recording_is_planned_against_that_recording():
    plan = plan_clip(ANDREW, "session-andrew", moment(start=16000.0, end=16020.0))
    assert plan.part_index == 2
    assert plan.recording_id == "rec-2"
    assert plan.media_start_seconds == pytest.approx(16000.0 - 60 - 14336.082)


def test_a_moment_in_the_overlap_belongs_to_the_later_recording():
    plan = plan_clip(ANDREW, "session-andrew", moment(start=14500.0, end=14520.0))
    assert plan.part_index == 2


# --- identity and determinism -----------------------------------------------

def test_the_clip_and_job_keys_are_the_existing_ones():
    """
    Unchanged from the SQL producer, so a replanned moment reuses the row a
    previous run created instead of queueing a duplicate.
    """
    assert clip_key("abc", moment(start_cue=41, end_cue=45)) == "abc:41:45"
    assert job_key("abc", moment(start_cue=41, end_cue=45)).startswith("positive:")
    assert job_key("abc", moment(start_cue=41, end_cue=45)).endswith(":41:45")


def test_keys_depend_on_the_cue_pair_not_the_timestamps():
    """
    Re-running the analysis and getting a millisecond-different boundary must
    not create a second clip for the same moment.
    """
    first = moment(start=254.320, end=275.0)
    second = moment(start=254.327, end=275.004)
    assert clip_key("abc", first) == clip_key("abc", second)
    assert job_key("abc", first) == job_key("abc", second)


def test_planning_is_deterministic():
    one = plan_clip(FEMI, FEMI_SESSION, moment())
    two = plan_clip(FEMI, FEMI_SESSION, moment())
    assert one == two


# --- provenance -------------------------------------------------------------

def test_the_provenance_names_both_versions_and_the_recording():
    plan = plan_clip(FEMI, FEMI_SESSION, moment())
    record = plan.provenance(FEMI)
    assert record["planner_version"] == POSITIVE_CLIP_PLANNER_VERSION
    assert record["media_coordinate_version"] == MEDIA_COORDINATE_VERSION
    assert record["recording_id"] == "rec-1"
    assert record["recording_media_duration_seconds"] == pytest.approx(8783.0)


def test_the_provenance_carries_no_url_or_token():
    record = plan_clip(FEMI, FEMI_SESSION, moment()).provenance(FEMI)
    flattened = repr(record).lower()
    for forbidden in ("http", "bearer", "token", "sig=", "secret", "sharepoint"):
        assert forbidden not in flattened


# --- a whole lecture --------------------------------------------------------

def test_one_unplannable_moment_does_not_discard_the_others():
    moments = [moment(1, 254.32, 275.0, 41, 45),
               moment(2, 364.80, 380.0, 60, 64),
               moment(3, 9500.0, 9520.0, 900, 904)]     # past the end
    plans, refusals = plan_lecture(FEMI, FEMI_SESSION, moments)
    assert [plan.clip_index for plan in plans] == [1, 2]
    assert [refusal["clip_index"] for refusal in refusals] == [3]
    assert refusals[0]["code"] == MOMENT_OUTSIDE_MEDIA


def test_a_custom_policy_is_respected():
    policy = ClipPolicy(padding_before_seconds=10.0, padding_after_seconds=5.0)
    plan = plan_clip(FEMI, FEMI_SESSION, moment(), policy)
    assert plan.media_start_seconds == pytest.approx(254.32 - 10)
    assert plan.media_end_seconds == pytest.approx(275.0 + 5)
