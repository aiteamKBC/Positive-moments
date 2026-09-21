"""
Phase 6C: turn a validated three-part split into media jobs.

WHERE THE CUTS COME FROM
------------------------
Not from here. `automation/lecture_parts/planner.py` owns the semantic split:
the model chooses two cue IDs from candidate windows and the planner
re-derives every timestamp from the transcript. This module takes those two
CANONICAL cut times and answers the media question - which recording, which
seconds - through the one validated transform.

No model is called here, and a stored plan is reused rather than re-asked.

THE PART THAT CANNOT BE PRODUCED, AND WHY IT IS REFUSED
-------------------------------------------------------
The media worker takes ONE `source_download_url` and ONE range per job
(`services/kbc-media-worker/src/ffmpeg.js::validateJob`). A lecture whose call
ran past Microsoft's four-hour recording cap has several recordings, and a part
that straddles the join needs two reads and a concatenation the worker cannot
do.

Cutting only the first segment would produce a part that ends mid-sentence and
looks perfectly healthy - correct duration, valid MP4, uploaded successfully.
So that case is REFUSED, loudly, with the segment breakdown attached so the gap
is a documented worker capability rather than a silent truncation.

Where the recording boundary happens to fall inside a cut's allowed zone, the
planner can place the cut ON the boundary and all three parts become
single-source. That choice belongs to the planner, not here; this module
reports what the boundary is so the planner can use it.
"""
from dataclasses import dataclass, field

from app.media.coordinates import (
    MEDIA_COORDINATE_VERSION,
    MediaCoordinateError,
    MediaSegment,
    MediaTimeline,
)


LECTURE_PART_MEDIA_VERSION = "lecture_part_media_v1"

PART_SPANS_RECORDINGS = "PART_SPANS_RECORDINGS"
PART_OUTSIDE_MEDIA = "PART_OUTSIDE_MEDIA"
INVALID_PART_PLAN = "INVALID_PART_PLAN"
# A part resolved to no SharePoint file, or to the file of a different
# recording. Never substitute another recording's file: the cut would succeed,
# the durations would agree, and the part would contain the wrong lecture.
UNRESOLVED_PART_SOURCE = "UNRESOLVED_PART_SOURCE"

# Parts are re-encoded. A stream copy seeks to the preceding keyframe, which
# makes a part start EARLY and therefore repeat seconds the previous part
# already ended with - the "unexpected overlap" the three-part output must not
# have. Correctness over encode time.
PART_CUT_MODE = "precise"


class PartPlanRefused(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.detail = detail or {}
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class PartMedia:
    part_number: int
    canonical_start_seconds: float
    canonical_end_seconds: float
    segments: list[MediaSegment] = field(default_factory=list)

    @property
    def canonical_duration_seconds(self) -> float:
        return self.canonical_end_seconds - self.canonical_start_seconds

    @property
    def single_source(self) -> bool:
        return len(self.segments) == 1

    @property
    def segment(self) -> MediaSegment:
        if not self.single_source:
            raise PartPlanRefused(
                PART_SPANS_RECORDINGS,
                f"part {self.part_number} spans {len(self.segments)} recordings")
        return self.segments[0]


@dataclass(frozen=True)
class PartMediaPlan:
    parts: list[PartMedia]
    media_coordinate_version: str = MEDIA_COORDINATE_VERSION
    media_policy_version: str = LECTURE_PART_MEDIA_VERSION

    @property
    def all_single_source(self) -> bool:
        return all(part.single_source for part in self.parts)

    def describe(self) -> dict:
        return {
            "media_policy_version": self.media_policy_version,
            "media_coordinate_version": self.media_coordinate_version,
            "parts": [
                {"part_number": part.part_number,
                 "canonical_start_seconds": round(part.canonical_start_seconds, 3),
                 "canonical_end_seconds": round(part.canonical_end_seconds, 3),
                 "duration_seconds": round(part.canonical_duration_seconds, 3),
                 "single_source": part.single_source,
                 "segments": [
                     {"part_index": segment.part_index,
                      "recording_id": segment.recording_id,
                      "media_start_seconds": round(segment.media_start_seconds, 3),
                      "media_end_seconds": round(segment.media_end_seconds, 3)}
                     for segment in part.segments]}
                for part in self.parts],
        }


def recording_boundaries_inside(timeline: MediaTimeline,
                                lecture_start: float,
                                lecture_end: float) -> list[float]:
    """
    Canonical times where the lecture crosses from one recording to the next.

    A cut placed exactly on one of these makes the surrounding parts
    single-source. Reported so the planner can take that into account; this
    module never moves a cut on its own, because a cut is a semantic decision.
    """
    return [part.canonical_offset_seconds for part in timeline.parts[1:]
            if lecture_start < part.canonical_offset_seconds < lecture_end]


def plan_part_media(timeline: MediaTimeline, lecture_start: float,
                    lecture_end: float, cut_1: float, cut_2: float) -> PartMediaPlan:
    """
    Convert three canonical part ranges into media coordinates.

    The boundaries are shared, so the parts are contiguous by construction: no
    gaps and no overlaps are arithmetically possible.
    """
    if not (lecture_start < cut_1 < cut_2 < lecture_end):
        raise PartPlanRefused(
            INVALID_PART_PLAN,
            f"cuts must be ordered inside the lecture: "
            f"{lecture_start:.3f} < {cut_1:.3f} < {cut_2:.3f} < {lecture_end:.3f}")

    ranges = [(1, lecture_start, cut_1), (2, cut_1, cut_2), (3, cut_2, lecture_end)]
    parts = []
    for number, start, end in ranges:
        try:
            segments = timeline.segments(start, end)
        except MediaCoordinateError as exc:
            raise PartPlanRefused(
                PART_OUTSIDE_MEDIA,
                f"part {number} ({start:.3f}s..{end:.3f}s) is not covered by "
                f"recorded media: {exc}") from None
        parts.append(PartMedia(part_number=number, canonical_start_seconds=start,
                               canonical_end_seconds=end, segments=segments))
    return PartMediaPlan(parts=parts)


def assert_producible(plan: PartMediaPlan) -> None:
    """
    Raise unless every part can be cut from ONE recording.

    This is the worker's limitation stated as a precondition, so a lecture that
    needs concatenation is refused at planning time rather than delivered as a
    truncated part.
    """
    spanning = [part for part in plan.parts if not part.single_source]
    if not spanning:
        return
    raise PartPlanRefused(
        PART_SPANS_RECORDINGS,
        "part(s) " + ", ".join(str(part.part_number) for part in spanning)
        + " cross a recording boundary; the media worker cuts one source per "
          "job and cannot concatenate",
        detail=plan.describe())


def verify_coverage(plan: PartMediaPlan, lecture_start: float,
                    lecture_end: float, tolerance: float = 0.001) -> dict:
    """
    The property the three-part output has to have: the parts tile the lecture
    exactly once. Returns the evidence rather than just a boolean, so a
    validation run can show its working.
    """
    parts = sorted(plan.parts, key=lambda part: part.part_number)
    gaps, overlaps = [], []
    for earlier, later in zip(parts, parts[1:]):
        delta = later.canonical_start_seconds - earlier.canonical_end_seconds
        if delta > tolerance:
            gaps.append({"between": [earlier.part_number, later.part_number],
                         "seconds": round(delta, 3)})
        elif delta < -tolerance:
            overlaps.append({"between": [earlier.part_number, later.part_number],
                             "seconds": round(-delta, 3)})
    covered = sum(part.canonical_duration_seconds for part in parts)
    expected = lecture_end - lecture_start
    return {
        "starts_at_lecture_start":
            abs(parts[0].canonical_start_seconds - lecture_start) <= tolerance,
        "ends_at_lecture_end":
            abs(parts[-1].canonical_end_seconds - lecture_end) <= tolerance,
        "gaps": gaps,
        "overlaps": overlaps,
        "covered_seconds": round(covered, 3),
        "expected_seconds": round(expected, 3),
        "complete": (not gaps and not overlaps
                     and abs(covered - expected) <= tolerance
                     and all(part.canonical_duration_seconds > 0 for part in parts)),
        "all_media_non_negative": all(
            segment.media_start_seconds >= -tolerance
            for part in parts for segment in part.segments),
    }


def boundary_aligned_cut(cues, boundary: float,
                         zone_low: float, zone_high: float) -> float | None:
    """
    The cut that makes a recording boundary stop splitting a part.

    WHY IT IS THE BOUNDARY ITSELF AND NOT THE NEAREST CUE
    -----------------------------------------------------
    Everywhere else a cut is the START of a chosen cue, because a cut is a
    semantic decision and cues are where meaning changes. That does not work
    here. If the cut lands even slightly after the boundary the earlier part
    still crosses it; slightly before, and the later part does. The only value
    that splits cleanly is the boundary itself - measured first on a real
    lecture, where the nearest cue sat 6.85s past the join and part 2 still
    spanned two recordings.

    So this returns an exact, code-derived time. It is not an invented
    timestamp: it is where Microsoft ended one recording and began the next,
    and the recordings overlap there, so no content is lost or duplicated on
    either side of it.

    It applies ONLY when the boundary falls inside the cut's allowed zone, so
    the balance rule still holds and the other cut keeps its semantic freedom.
    When the boundary falls outside every zone, no cut placement can help and
    None is returned - that lecture needs concatenation, and saying so is the
    honest answer.

    `cues` is accepted so callers can pass their transcript uniformly; the
    chosen time deliberately does not depend on it.
    """
    if not (zone_low <= boundary <= zone_high):
        return None
    return boundary
