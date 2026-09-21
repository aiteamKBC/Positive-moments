"""
Phase 6A: the lecture-parts planner, after the zero-origin fix.

THE BUG THESE TESTS PIN DOWN
----------------------------
The planner used to place its candidate zones at fractions of the RECORDING
length, on the assumption that a lecture runs 0 -> duration. Phase 6A measured
that this is false twice over: cue times are call-relative, so a lecture whose
call sat open for hours starts at 10026.8s and has no cues anywhere near "33%
of the recording"; and a long call is recorded in several files, so the last
cue can legitimately sit past the end of the first one.

The shapes below are the real measured ones. `ANDREW_*` is the case that used
to raise "no transcript cues fall inside the cut_1 zone".
"""
import pytest

from automation.lecture_parts.planner import (
    CUT1_MAX,
    CUT1_MIN,
    CUT2_MAX,
    CUT2_MIN,
    Cue,
    LectureTimeline,
    build_candidate_windows,
    lecture_timeline,
    validate_ai_output,
)


def transcript(first_start: float, last_end: float, count: int = 600):
    """
    A regularly spaced transcript covering exactly [first_start, last_end].

    The last cue ends on `last_end` rather than a step short of it, so the
    derived timeline is the span these tests claim it is.
    """
    step = (last_end - first_start) / count
    cues = [Cue(index + 1,
                first_start + index * step,
                first_start + index * step + step * 0.8,
                f"Speaker {index % 3}", f"line {index}")
            for index in range(count)]
    final = cues[-1]
    cues[-1] = Cue(final.cue_id, final.start_seconds, last_end,
                   final.speaker, final.text)
    return cues


# The extreme case: the call ran for 2h47m before the teaching started.
ANDREW = transcript(10026.8, 17194.5)
# A lecture that begins almost immediately.
PPC = transcript(8.6, 12594.7)


def choose(windows, name, index=0):
    return windows[name]["cues"][index]["cue_id"]


# --- the timeline -----------------------------------------------------------

def test_the_timeline_is_the_transcript_span_not_the_recording():
    timeline = lecture_timeline(ANDREW)
    assert timeline.start_seconds == pytest.approx(10026.8)
    assert timeline.end_seconds == pytest.approx(17194.5, abs=0.5)
    assert timeline.span_seconds == pytest.approx(7167.7, abs=0.5)


def test_fractions_are_taken_along_the_lecture_not_from_zero():
    timeline = LectureTimeline(10026.8, 17194.5)
    assert timeline.at(0.0) == pytest.approx(10026.8)
    assert timeline.at(1.0) == pytest.approx(17194.5)
    assert timeline.at(0.5) == pytest.approx((10026.8 + 17194.5) / 2)


def test_an_empty_transcript_has_no_timeline():
    with pytest.raises(ValueError, match="empty transcript"):
        lecture_timeline([])


# --- the regression ---------------------------------------------------------

def test_a_lecture_starting_three_hours_into_the_call_still_gets_zones():
    """
    The exact failure Phase 6A was asked to fix. Under the old rule the cut_1
    zone was 3600s-6048s, a stretch containing no cues at all, and planning
    raised before the AI was ever asked.
    """
    timeline = lecture_timeline(ANDREW)
    windows = build_candidate_windows(ANDREW, timeline)
    for name in ("cut_1", "cut_2"):
        assert windows[name]["cues"], f"{name} zone was empty"
        assert windows[name]["zone_start_seconds"] > 10026.8


def test_the_zones_sit_where_the_lecture_actually_is():
    timeline = lecture_timeline(ANDREW)
    windows = build_candidate_windows(ANDREW, timeline)
    assert windows["cut_1"]["target_seconds"] == pytest.approx(
        timeline.at(0.33), abs=1.0)
    assert windows["cut_2"]["target_seconds"] == pytest.approx(
        timeline.at(0.66), abs=1.0)
    # And every offered cue really is inside its zone.
    for name, low, high in (("cut_1", CUT1_MIN, CUT1_MAX),
                            ("cut_2", CUT2_MIN, CUT2_MAX)):
        for cue in windows[name]["cues"]:
            assert (timeline.at(low) - 1
                    <= _seconds(cue["start"]) <= timeline.at(high) + 1)


def _seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def test_a_near_zero_lecture_is_unaffected():
    """The fix must not move the boundaries of the lectures that already worked."""
    timeline = lecture_timeline(PPC)
    windows = build_candidate_windows(PPC, timeline)
    assert windows["cut_1"]["target_seconds"] == pytest.approx(
        timeline.at(0.33), abs=1.0)
    assert windows["cut_1"]["zone_start_seconds"] == pytest.approx(
        timeline.at(CUT1_MIN), abs=0.01)


# --- validation -------------------------------------------------------------

def accepted_plan(cues):
    timeline = lecture_timeline(cues)
    windows = build_candidate_windows(cues, timeline)
    middle = len(windows["cut_1"]["cues"]) // 2
    ai = {
        "cut_1_cue": choose(windows, "cut_1", middle),
        "cut_2_cue": choose(windows, "cut_2", middle),
        "parts": [{"part_number": n, "title": f"Part {n}", "summary": "s"}
                  for n in (1, 2, 3)],
        "confidence": 0.8,
    }
    return validate_ai_output(ai, cues, windows, timeline, "session-1", "live",
                              recording_duration_seconds=14400.064), timeline


def test_part_one_starts_at_the_lecture_not_at_zero():
    """
    Part 1 used to start at 0.0, which for Andrew meant nearly three hours of
    an empty call would have been delivered as the first third of a lecture.
    """
    plan, timeline = accepted_plan(ANDREW)
    assert plan.planner_status == "planned", plan.errors
    assert plan.parts[0]["start_seconds"] == pytest.approx(timeline.start_seconds)
    assert plan.parts[0]["start_seconds"] > 10000


def test_part_three_ends_at_the_lecture_not_at_the_recording_length():
    plan, timeline = accepted_plan(ANDREW)
    assert plan.parts[2]["end_seconds"] == pytest.approx(timeline.end_seconds)
    # The recording it was planned against is shorter than the lecture's end,
    # which is exactly the multipart case the old rule rejected.
    assert plan.recording_duration_seconds < plan.parts[2]["end_seconds"]


def test_the_three_parts_are_contiguous_and_balanced():
    plan, timeline = accepted_plan(ANDREW)
    assert [p["part_number"] for p in plan.parts] == [1, 2, 3]
    for earlier, later in zip(plan.parts, plan.parts[1:]):
        assert earlier["end_seconds"] == pytest.approx(later["start_seconds"])
    assert sum(p["duration_seconds"] for p in plan.parts) == pytest.approx(
        timeline.span_seconds, abs=0.01)
    assert sum(p["share"] for p in plan.parts) == pytest.approx(1.0, abs=0.001)


def test_shares_are_of_the_lecture_not_of_the_recording():
    plan, timeline = accepted_plan(ANDREW)
    for part in plan.parts:
        expected = part["duration_seconds"] / timeline.span_seconds
        assert part["share"] == pytest.approx(expected, abs=0.001)


def test_the_plan_records_the_timeline_it_was_built_on():
    """Provenance. A later reader must not have to guess which axis was used."""
    plan, timeline = accepted_plan(ANDREW)
    assert plan.timeline_start_seconds == pytest.approx(timeline.start_seconds)
    assert plan.timeline_end_seconds == pytest.approx(timeline.end_seconds)


def test_a_cut_outside_the_allowed_region_is_still_rejected():
    """The fix moved the region; it did not remove the check."""
    cues = ANDREW
    timeline = lecture_timeline(cues)
    windows = build_candidate_windows(cues, timeline)
    ai = {
        "cut_1_cue": choose(windows, "cut_1"),
        # A cue from the cut_1 window offered as cut_2: inside the transcript,
        # inside a window, and in the wrong place.
        "cut_2_cue": choose(windows, "cut_1", 1),
        "parts": [{"part_number": n, "title": "t", "summary": "s"} for n in (1, 2, 3)],
    }
    plan = validate_ai_output(ai, cues, windows, timeline, "session-1", "live")
    assert plan.planner_status == "rejected"
    assert plan.errors


def test_an_invented_cue_is_still_rejected():
    cues = PPC
    timeline = lecture_timeline(cues)
    windows = build_candidate_windows(cues, timeline)
    ai = {"cut_1_cue": 999999, "cut_2_cue": choose(windows, "cut_2"),
          "parts": [{"part_number": n, "title": "t", "summary": "s"} for n in (1, 2, 3)]}
    plan = validate_ai_output(ai, cues, windows, timeline, "session-1", "live")
    assert plan.planner_status == "rejected"
    assert any("does not exist" in error for error in plan.errors)
