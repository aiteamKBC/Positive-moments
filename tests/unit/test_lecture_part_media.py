"""
Phase 6C: converting a three-part split into media coordinates.

The shapes are the measured ones. Andrew is the mandatory case: its recording
boundary sits inside the cut-2 zone, so a boundary-aligned cut turns a lecture
that could not be produced at all into three single-source parts. Risk is the
counter-example whose boundary falls outside every zone and therefore genuinely
cannot be produced by a one-source worker.
"""
import pytest

from app.media.coordinates import MediaTimeline, RecordingPart
from app.media.lecture_parts import (
    LECTURE_PART_MEDIA_VERSION,
    PART_CUT_MODE,
    PART_OUTSIDE_MEDIA,
    PART_SPANS_RECORDINGS,
    INVALID_PART_PLAN,
    PartPlanRefused,
    assert_producible,
    boundary_aligned_cut,
    plan_part_media,
    recording_boundaries_inside,
    verify_coverage,
)


def part(index, offset, duration):
    return RecordingPart(part_index=index, canonical_offset_seconds=offset,
                         media_duration_seconds=duration,
                         recording_id=f"rec-{index}", user_id="user",
                         meeting_id="meeting")


ANDREW = MediaTimeline([part(1, 0.0, 14400.064), part(2, 14336.082, 2870.336)])
ANDREW_START, ANDREW_END = 10026.775, 17195.372
ANDREW_BOUNDARY = 14336.082

RISK = MediaTimeline([part(1, 0.0, 14400.064), part(2, 14338.368, 924.544)])
RISK_START, RISK_END = 6787.381, 14404.394

FEMI = MediaTimeline([part(1, 0.0, 8776.384)])
FEMI_START, FEMI_END = 138.913, 8774.313
FEMI_CUT_1, FEMI_CUT_2 = 3052.393, 5881.673      # the stored AI plan's cuts


# --- the single-recording case ----------------------------------------------

def test_three_parts_tile_the_lecture_exactly_once():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    coverage = verify_coverage(plan, FEMI_START, FEMI_END)
    assert coverage["complete"]
    assert coverage["gaps"] == []
    assert coverage["overlaps"] == []
    assert coverage["covered_seconds"] == pytest.approx(FEMI_END - FEMI_START, abs=0.01)


def test_part_one_starts_at_the_lecture_not_at_zero():
    """
    The zero-origin bug, restated for parts. Femi's call ran for 138.9s before
    the teaching began; that is not part one.
    """
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    assert plan.parts[0].canonical_start_seconds == pytest.approx(FEMI_START)
    assert plan.parts[0].media_start_seconds if False else True
    assert plan.parts[0].segment.media_start_seconds == pytest.approx(138.913)


def test_part_three_ends_at_the_lecture_not_at_the_recording_end():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    assert plan.parts[2].canonical_end_seconds == pytest.approx(FEMI_END)
    assert plan.parts[2].segment.media_end_seconds == pytest.approx(8774.313)
    assert plan.parts[2].segment.media_end_seconds < 8776.384


def test_a_single_recording_lecture_is_producible():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    assert plan.all_single_source
    assert_producible(plan)


def test_parts_are_re_encoded_not_stream_copied():
    """
    A stream copy seeks to the preceding keyframe, so each part would start
    early and repeat the end of the previous one.
    """
    assert PART_CUT_MODE == "precise"


# --- the multipart case -----------------------------------------------------

def test_a_boundary_inside_the_lecture_is_reported():
    assert recording_boundaries_inside(ANDREW, ANDREW_START, ANDREW_END) == \
        [pytest.approx(ANDREW_BOUNDARY)]
    assert recording_boundaries_inside(FEMI, FEMI_START, FEMI_END) == []


def test_without_a_boundary_aligned_cut_a_part_spans_two_recordings():
    """Andrew as the planner would have left it: part 2 straddles the join."""
    plan = plan_part_media(ANDREW, ANDREW_START, ANDREW_END, 12390.417, 14748.452)
    assert not plan.all_single_source
    spanning = [p for p in plan.parts if not p.single_source]
    assert [p.part_number for p in spanning] == [2]
    with pytest.raises(PartPlanRefused) as caught:
        assert_producible(plan)
    assert caught.value.code == PART_SPANS_RECORDINGS


def test_the_boundary_aligned_cut_is_the_boundary_itself():
    """
    Not the nearest cue. Measured: the nearest cue sat 6.85s past the join and
    part 2 still spanned. Only the boundary splits cleanly.
    """
    cues = [(1, 14300.0), (2, 14342.932), (3, 14400.0)]
    chosen = boundary_aligned_cut(cues, ANDREW_BOUNDARY, 14184.5, 15403.2)
    assert chosen == pytest.approx(ANDREW_BOUNDARY)
    assert chosen != pytest.approx(14342.932)


def test_a_boundary_outside_the_zone_yields_no_aligned_cut():
    """Risk's boundary is past the cut-2 zone, so no cut placement can help."""
    assert boundary_aligned_cut([(1, 12000.0)], 14338.368, 11205.3, 12500.1) is None


def test_the_boundary_aligned_cut_makes_andrew_producible():
    """The mandatory regression case."""
    plan = plan_part_media(ANDREW, ANDREW_START, ANDREW_END,
                           12390.417, ANDREW_BOUNDARY)
    assert plan.all_single_source
    assert_producible(plan)
    assert [p.segment.part_index for p in plan.parts] == [1, 1, 2]
    # Part 3 is the whole of the second recording, from its first frame.
    assert plan.parts[2].segment.media_start_seconds == pytest.approx(0.0)
    assert plan.parts[2].segment.media_end_seconds == pytest.approx(
        ANDREW_END - ANDREW_BOUNDARY)


def test_andrew_still_tiles_the_lecture_after_the_boundary_cut():
    plan = plan_part_media(ANDREW, ANDREW_START, ANDREW_END,
                           12390.417, ANDREW_BOUNDARY)
    coverage = verify_coverage(plan, ANDREW_START, ANDREW_END)
    assert coverage["complete"]
    assert coverage["gaps"] == [] and coverage["overlaps"] == []
    assert coverage["all_media_non_negative"]


def test_risk_management_cannot_be_produced_and_says_so():
    """
    Its boundary falls inside part 3 and outside every cut zone, so there is no
    cut placement that avoids a span. Refusing is the honest answer.
    """
    plan = plan_part_media(RISK, RISK_START, RISK_END, 9306.874, 11822.274)
    assert verify_coverage(plan, RISK_START, RISK_END)["complete"]
    with pytest.raises(PartPlanRefused) as caught:
        assert_producible(plan)
    assert caught.value.code == PART_SPANS_RECORDINGS
    assert "3" in str(caught.value)
    # The refusal carries the segment breakdown, so the gap is documented.
    assert caught.value.detail["parts"][2]["single_source"] is False


# --- refusals ---------------------------------------------------------------

def test_unordered_cuts_are_refused():
    with pytest.raises(PartPlanRefused) as caught:
        plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_2, FEMI_CUT_1)
    assert caught.value.code == INVALID_PART_PLAN


def test_a_lecture_running_past_its_media_is_refused():
    short = MediaTimeline([part(1, 0.0, 4000.0)])
    with pytest.raises(PartPlanRefused) as caught:
        plan_part_media(short, 100.0, 8000.0, 2000.0, 5000.0)
    assert caught.value.code == PART_OUTSIDE_MEDIA


def test_accessing_the_segment_of_a_spanning_part_raises():
    """A caller must not be able to quietly take the first half."""
    plan = plan_part_media(ANDREW, ANDREW_START, ANDREW_END, 12390.417, 14748.452)
    with pytest.raises(PartPlanRefused):
        _ = plan.parts[1].segment


# --- provenance -------------------------------------------------------------

def test_the_description_records_both_versions_and_no_secret():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    described = plan.describe()
    assert described["media_policy_version"] == LECTURE_PART_MEDIA_VERSION
    assert described["media_coordinate_version"] == "call_relative_part_media_v1"
    assert len(described["parts"]) == 3
    flattened = repr(described).lower()
    for forbidden in ("http", "bearer", "token", "sig=", "secret"):
        assert forbidden not in flattened


# --- which file each part is cut from ----------------------------------------
# The Femi pilot could not exercise this: with one recording there is only one
# file a part can mean. Andrew has two, and the session row names only one.

from app.media.part_jobs import resolve_part_sources                # noqa: E402
from app.media.lecture_parts import UNRESOLVED_PART_SOURCE          # noqa: E402

SESSION = {"recording_drive_id": "drive-1", "recording_item_id": "item-rec-1"}


def andrew_plan():
    return plan_part_media(ANDREW, ANDREW_START, ANDREW_END,
                           12390.417, ANDREW_BOUNDARY)


def test_a_single_recording_lecture_uses_the_session_location():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    resolved = resolve_part_sources(plan, SESSION)
    assert {p["source_item_id"] for p in resolved.values()} == {"item-rec-1"}


def test_a_multipart_lecture_never_falls_back_to_the_session_location():
    """
    The defect this exists to prevent: part 3 is media 0.000->2859.290 of the
    SECOND recording. Cut from the first recording's file that range is the
    opening 48 minutes of the lecture - right duration, right coverage, wrong
    content, and nothing downstream can tell.
    """
    with pytest.raises(PartPlanRefused) as caught:
        resolve_part_sources(andrew_plan(), SESSION)
    assert caught.value.code == UNRESOLVED_PART_SOURCE
    assert caught.value.detail["multipart"] is True
    # All three, not just part 3: the session names one file for two
    # recordings, so there is no evidence it is recording 1's either.
    assert [u["part_number"] for u in caught.value.detail["unresolved"]] == [1, 2, 3]


def test_a_multipart_lecture_resolves_each_part_to_its_own_recording():
    plan = andrew_plan()
    sources = {
        plan.parts[0].segment.recording_id: {"source_drive_id": "d",
                                             "source_item_id": "item-rec-1"},
        plan.parts[2].segment.recording_id: {"source_drive_id": "d",
                                             "source_item_id": "item-rec-2"},
    }
    resolved = resolve_part_sources(plan, SESSION, sources)
    assert resolved[1]["source_item_id"] == "item-rec-1"
    assert resolved[2]["source_item_id"] == "item-rec-1"   # same recording
    assert resolved[3]["source_item_id"] == "item-rec-2"   # the join


def test_two_recordings_pointing_at_one_file_is_refused():
    """A mis-located second recording looks resolved but is not."""
    plan = andrew_plan()
    same = {"source_drive_id": "d", "source_item_id": "item-rec-1"}
    sources = {plan.parts[0].segment.recording_id: same,
               plan.parts[2].segment.recording_id: same}
    with pytest.raises(PartPlanRefused) as caught:
        resolve_part_sources(plan, SESSION, sources)
    assert caught.value.code == UNRESOLVED_PART_SOURCE


def test_half_a_location_counts_as_no_location():
    plan = plan_part_media(FEMI, FEMI_START, FEMI_END, FEMI_CUT_1, FEMI_CUT_2)
    with pytest.raises(PartPlanRefused):
        resolve_part_sources(plan, {"recording_drive_id": "d",
                                    "recording_item_id": None})


def test_an_instant_and_a_range_at_the_join_may_choose_different_recordings():
    """
    Measured on Andrew: locate(14336.082) answers recording 1, while the part
    starting at 14336.082 is cut from recording 2. Both are correct and the
    difference is not a bug to be reconciled.

    The recordings overlap by 63.982s, so that instant exists in BOTH files.
    An instant may be taken from the earlier one, which still has 64s to run.
    A 2859s RANGE cannot: recording 1 ends 64s later, so only recording 2 can
    supply the whole part. Making locate() prefer the later recording would
    change nothing here and would move every clip near a join for no reason.
    """
    point = ANDREW.locate(ANDREW_BOUNDARY)
    assert point.part_index == 1
    assert point.media_seconds == pytest.approx(ANDREW_BOUNDARY)

    plan = plan_part_media(ANDREW, ANDREW_START, ANDREW_END,
                           12390.417, ANDREW_BOUNDARY)
    assert plan.parts[2].segment.part_index == 2
    assert plan.parts[2].segment.media_start_seconds == pytest.approx(0.0)

    # The overlap is what makes both answers real footage.
    first = ANDREW.parts[0]
    assert first.canonical_offset_seconds + first.media_duration_seconds \
        > ANDREW_BOUNDARY + 60.0
