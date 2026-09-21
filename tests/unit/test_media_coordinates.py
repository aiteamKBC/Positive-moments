"""
Phase 6A: the media coordinate transform.

The fixtures below are not invented. They are the shapes measured from the real
recordings in Phase 6A - FFprobe container durations and
`lecture_transcript_selection_parts.part_offset_ms` - so a regression here is a
regression against what the media actually is.

    Andrew-Scheduling   part 1 offset      0.000s  media 14400.064s
                        part 2 offset  14336.082s  media  2870.336s
                        cues 10026.8s .. 17194.5s
    Risk Management     part 1 offset      0.000s  media 14400.064s
                        part 2 offset  14338.368s  media   924.544s
                        cues  6787.4s .. 14404.4s
    PPC                 part 1 offset      0.000s  media 12619.841s
                        cues     8.6s .. 12594.7s
"""
import pytest

from app.media.coordinates import (
    INVALID_RANGE,
    INVALID_TIMELINE,
    MEDIA_COORDINATE_VERSION,
    NO_RECORDED_MEDIA,
    OUTSIDE_RECORDED_MEDIA,
    MediaCoordinateError,
    MediaTimeline,
    RecordingPart,
)


def part(index, offset, duration, recording="rec"):
    return RecordingPart(
        part_index=index, canonical_offset_seconds=offset,
        media_duration_seconds=duration, recording_id=f"{recording}-{index}",
        user_id="user", meeting_id="meeting")


ANDREW = [part(1, 0.0, 14400.064), part(2, 14336.082, 2870.336)]
RISK = [part(1, 0.0, 14400.064), part(2, 14338.368, 924.544)]
PPC = [part(1, 0.0, 12619.841)]


# --- the simple case has to stay simple -------------------------------------

def test_a_single_part_lecture_maps_canonical_time_to_itself():
    """
    The legacy assumption was not wrong, it was incomplete. Where it held, the
    new transform must agree with it exactly, or every clip ever cut moves.
    """
    timeline = MediaTimeline(PPC)
    for canonical in (8.6, 600.0, 6000.0, 12594.7):
        point = timeline.locate(canonical)
        assert point.part_index == 1
        assert point.media_seconds == pytest.approx(canonical, abs=1e-6)


def test_the_version_is_stable():
    assert MEDIA_COORDINATE_VERSION == "call_relative_part_media_v1"
    assert MediaTimeline(PPC).version == MEDIA_COORDINATE_VERSION


# --- the case the legacy assumption gets wrong ------------------------------

def test_a_large_non_zero_cue_origin_is_still_inside_the_first_recording():
    """
    Andrew's first cue is at 10026.8s because the call ran for nearly three
    hours before the lecture began. That is a real position in a four-hour
    recording, not an impossible one - which is why the fix is NOT to subtract
    the first cue.
    """
    point = MediaTimeline(ANDREW).locate(10026.8)
    assert point.part_index == 1
    assert point.media_seconds == pytest.approx(10026.8)


def test_subtracting_the_first_cue_would_land_somewhere_else_entirely():
    """
    The refuted candidate, stated as a number. Phase 6A measured the recording
    at this position and found silence: the meeting had not started yet.
    """
    timeline = MediaTimeline(ANDREW)
    assert timeline.locate(10026.8).media_seconds == pytest.approx(10026.8)
    assert timeline.locate(10026.8).media_seconds != pytest.approx(0.0)


def test_a_cue_past_the_first_recording_resolves_into_the_second():
    """
    Andrew's last cue is at 17194.5s, which does not exist in a 14400s file.
    Under the identity mapping this request fails; under the transform it is
    2858.4s into the second recording.
    """
    point = MediaTimeline(ANDREW).locate(17194.5)
    assert point.part_index == 2
    assert point.recording_id == "rec-2"
    assert point.media_seconds == pytest.approx(17194.5 - 14336.082)
    assert point.media_seconds < 2870.336


def test_risk_managements_final_cue_is_just_past_the_first_recording():
    """
    The subtle one. 14404.4s is only 4.3s past the end of part 1's media, so an
    off-by-a-recording error here produces a request that looks almost right
    and cuts the wrong lecture segment.
    """
    point = MediaTimeline(RISK).locate(14404.4)
    assert point.part_index == 2
    assert point.media_seconds == pytest.approx(14404.4 - 14338.368)


# --- overlapping recordings -------------------------------------------------

def test_the_later_recording_wins_inside_the_overlap():
    """
    Consecutive recordings overlap by about a minute, so some canonical
    instants exist in two files. The mapping has to be a function: the later
    part owns the overlap, and both answers are never returned.
    """
    timeline = MediaTimeline(ANDREW)
    inside_overlap = 14350.0            # in part 1's media AND part 2's
    assert 14336.082 < inside_overlap < 14400.064
    point = timeline.locate(inside_overlap)
    assert point.part_index == 2
    assert point.media_seconds == pytest.approx(inside_overlap - 14336.082)


def test_the_timeline_ends_where_the_last_recording_ends():
    timeline = MediaTimeline(ANDREW)
    assert timeline.canonical_end_seconds == pytest.approx(14336.082 + 2870.336)


# --- ranges -----------------------------------------------------------------

def test_a_range_inside_one_recording_is_one_segment():
    segments = MediaTimeline(ANDREW).segments(10026.8, 10086.8)
    assert len(segments) == 1
    assert segments[0].part_index == 1
    assert segments[0].media_start_seconds == pytest.approx(10026.8)
    assert segments[0].duration_seconds == pytest.approx(60.0)


def test_a_range_crossing_a_recording_boundary_splits_without_overlapping():
    """
    The seconds where two recordings contain the same audio must appear in the
    output exactly once, or the concatenated part stutters.
    """
    timeline = MediaTimeline(ANDREW)
    segments = timeline.segments(14300.0, 14500.0)
    assert [segment.part_index for segment in segments] == [1, 2]

    # Contiguous on the canonical timeline, with no gap and no double-cover.
    assert segments[0].canonical_end_seconds == pytest.approx(
        segments[1].canonical_start_seconds)
    assert segments[0].canonical_start_seconds == pytest.approx(14300.0)
    assert segments[1].canonical_end_seconds == pytest.approx(14500.0)

    total = sum(segment.duration_seconds for segment in segments)
    assert total == pytest.approx(200.0)

    # And the split happens at the second recording's start, not anywhere else.
    assert segments[0].canonical_end_seconds == pytest.approx(14336.082)
    assert segments[1].media_start_seconds == pytest.approx(0.0)


def test_crossing_is_reported_so_a_caller_can_decide():
    timeline = MediaTimeline(ANDREW)
    assert not timeline.crosses_recording_boundary(10026.8, 11000.0)
    assert timeline.crosses_recording_boundary(14300.0, 14500.0)


def test_a_full_lecture_range_is_covered_completely():
    timeline = MediaTimeline(ANDREW)
    segments = timeline.segments(10026.8, 17194.5)
    total = sum(segment.duration_seconds for segment in segments)
    assert total == pytest.approx(17194.5 - 10026.8)
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.canonical_end_seconds == pytest.approx(
            later.canonical_start_seconds)


# --- refusals ---------------------------------------------------------------

def test_a_time_past_the_end_of_the_media_is_refused_rather_than_clamped():
    """
    Clamping would turn "never recorded" into "here is a moment that was",
    which is how a pipeline delivers a confident clip of the wrong thing.
    """
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline(RISK).locate(20000.0)
    assert caught.value.code == OUTSIDE_RECORDED_MEDIA


def test_a_range_running_past_the_media_is_refused():
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline(PPC).segments(12000.0, 13000.0)
    assert caught.value.code == OUTSIDE_RECORDED_MEDIA


def test_a_negative_time_is_refused():
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline(PPC).locate(-5.0)
    assert caught.value.code == OUTSIDE_RECORDED_MEDIA


def test_an_inverted_or_empty_range_is_refused():
    timeline = MediaTimeline(PPC)
    for start, end in ((500.0, 500.0), (900.0, 400.0)):
        with pytest.raises(MediaCoordinateError) as caught:
            timeline.segments(start, end)
        assert caught.value.code == INVALID_RANGE


def test_a_lecture_with_no_recording_is_refused():
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline([])
    assert caught.value.code == NO_RECORDED_MEDIA


def test_a_part_with_no_measured_duration_is_refused():
    """
    Zero is what an unmeasured duration looks like, and a timeline built on it
    would accept every request and produce nothing.
    """
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline([part(1, 0.0, 0.0)])
    assert caught.value.code == INVALID_TIMELINE


def test_a_timeline_that_does_not_start_at_zero_is_refused():
    """
    Part 1's offset is zero by construction. A non-zero first offset means the
    parts were assembled wrongly, and guessing which end is missing would be
    inventing a coordinate.
    """
    with pytest.raises(MediaCoordinateError) as caught:
        MediaTimeline([part(2, 14336.082, 2870.336)])
    assert caught.value.code == INVALID_TIMELINE


# --- provenance -------------------------------------------------------------

def test_the_description_is_persistable_and_carries_no_secret():
    described = MediaTimeline(ANDREW).describe()
    assert described["media_coordinate_version"] == MEDIA_COORDINATE_VERSION
    assert len(described["parts"]) == 2
    assert described["canonical_end_seconds"] == pytest.approx(17206.418)
    flattened = repr(described).lower()
    for forbidden in ("http", "bearer", "token", "sig=", "secret"):
        assert forbidden not in flattened


def test_parts_are_ordered_by_offset_regardless_of_input_order():
    timeline = MediaTimeline(list(reversed(ANDREW)))
    assert [p.part_index for p in timeline.parts] == [1, 2]


# --- the measured tail overrun ----------------------------------------------

def test_a_transcript_that_overruns_its_recording_slightly_is_clamped():
    """
    Real case. "Martech - Fri" has a final cue ending 0.243s after the last
    frame of its media, because a cue's end time is rounded and a recording
    stops when the host stops it. Refusing that would block a whole lecture
    over a quarter of a second.
    """
    martech = [part(1, 0.0, 7340.437)]
    timeline = MediaTimeline(martech)
    point = timeline.locate(7340.680)
    assert point.part_index == 1
    assert point.media_seconds == pytest.approx(7340.437)

    segments = timeline.segments(7000.0, 7340.680)
    assert segments[-1].media_end_seconds == pytest.approx(7340.437)


def test_an_overrun_beyond_the_tolerance_is_still_refused():
    """
    The tolerance exists for rounding, not for a wrong timeline. Thirty
    seconds past the end is a real problem and must not be rounded away.
    """
    timeline = MediaTimeline([part(1, 0.0, 7340.437)])
    with pytest.raises(MediaCoordinateError) as caught:
        timeline.locate(7370.0)
    assert caught.value.code == OUTSIDE_RECORDED_MEDIA
    with pytest.raises(MediaCoordinateError):
        timeline.segments(7000.0, 7370.0)


def test_the_tolerance_never_applies_at_the_start():
    timeline = MediaTimeline([part(1, 0.0, 7340.437)])
    with pytest.raises(MediaCoordinateError) as caught:
        timeline.locate(-0.5)
    assert caught.value.code == OUTSIDE_RECORDED_MEDIA
