"""
Phase 6B: deterministic Positive Clip planning.

WHAT THIS IS
------------
Given a positive moment the analysis already found, and the measured media
timeline of the lecture it came from, decide exactly which seconds of which
recording become a clip.

It is pure. It performs no I/O, calls no model, and holds no opinion about what
a positive moment is - that judgement was made once by the analysis and is
treated here as source evidence.

THE COORDINATE RULE
-------------------
Every position goes through `app.media.coordinates`. There is NO offset
arithmetic in this module, and no special case for the analysed July lectures:
their timeline happens to be a single part at offset zero, so the one validated
transform reduces to the identity on its own. Writing that identity out by hand
here would be a second coordinate model, which is the thing Phase 6A exists to
prevent.

PADDING, AND WHY IT IS BOUNDED
------------------------------
The established policy is 60 seconds either side of the moment. The previous
SQL producer applied that unconditionally: it clamped the start at zero but let
the end run past the end of the recording, because it had no measured duration
to clamp against. Phase 6A measured one, so padding is now bounded at both ends
and a clip can no longer ask for media that does not exist.

Padding is a courtesy, not part of the evidence. When the recording cannot
supply it the clip is still produced with less padding; the moment itself is
never trimmed to fit, and if the MOMENT does not fit the plan is refused.
"""
from dataclasses import dataclass, field

from app.media.coordinates import (
    MEDIA_COORDINATE_VERSION,
    MediaCoordinateError,
    MediaSegment,
    MediaTimeline,
)


POSITIVE_CLIP_PLANNER_VERSION = "positive_clip_plan_v2"

# The existing business rule, unchanged: a minute of run-up and a minute of
# follow-through so the moment has context.
DEFAULT_PADDING_BEFORE_SECONDS = 60.0
DEFAULT_PADDING_AFTER_SECONDS = 60.0

# A clip shorter than this is not watchable; one longer than this is not a clip.
MIN_CLIP_SECONDS = 5.0
MAX_CLIP_SECONDS = 900.0


class ClipPlanRefused(Exception):
    """The moment cannot be turned into a clip. Carries the reason code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


MOMENT_OUTSIDE_MEDIA = "MOMENT_OUTSIDE_MEDIA"
MOMENT_TOO_SHORT = "MOMENT_TOO_SHORT"
MOMENT_TOO_LONG = "MOMENT_TOO_LONG"
INVALID_MOMENT = "INVALID_MOMENT"
CROSSES_RECORDING_BOUNDARY = "CROSSES_RECORDING_BOUNDARY"


@dataclass(frozen=True)
class ClipPolicy:
    padding_before_seconds: float = DEFAULT_PADDING_BEFORE_SECONDS
    padding_after_seconds: float = DEFAULT_PADDING_AFTER_SECONDS
    min_clip_seconds: float = MIN_CLIP_SECONDS
    max_clip_seconds: float = MAX_CLIP_SECONDS
    # Precise means re-encode. A stream copy lands on the nearest keyframe,
    # which for a clip built around one sentence can move the boundary by
    # seconds and cut the sentence in half.
    cut_mode: str = "precise"

    def as_dict(self) -> dict:
        return {
            "padding_before_seconds": self.padding_before_seconds,
            "padding_after_seconds": self.padding_after_seconds,
            "min_clip_seconds": self.min_clip_seconds,
            "max_clip_seconds": self.max_clip_seconds,
            "cut_mode": self.cut_mode,
        }


@dataclass(frozen=True)
class PositiveMoment:
    """One moment from the stored analysis. Identity is the cue pair."""
    clip_index: int
    start_seconds: float
    end_seconds: float
    start_cue: int | None = None
    end_cue: int | None = None
    speaker: str | None = None
    category: str | None = None
    quote: str | None = None


@dataclass(frozen=True)
class ClipPlan:
    """What will be cut, in both coordinate systems, with its provenance."""
    clip_index: int
    clip_key: str
    job_key: str

    moment_start_seconds: float
    moment_end_seconds: float

    canonical_start_seconds: float
    canonical_end_seconds: float

    media_start_seconds: float
    media_end_seconds: float

    applied_padding_before_seconds: float
    applied_padding_after_seconds: float

    recording_id: str
    user_id: str
    meeting_id: str
    part_index: int

    cut_mode: str
    segments: list[MediaSegment] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.media_end_seconds - self.media_start_seconds

    def provenance(self, timeline: MediaTimeline) -> dict:
        """
        Everything a later reader needs to decide whether this plan is stale.

        No URL, no token. A recording is named, never linked.
        """
        return {
            "planner_version": POSITIVE_CLIP_PLANNER_VERSION,
            "media_coordinate_version": MEDIA_COORDINATE_VERSION,
            "moment_start_seconds": round(self.moment_start_seconds, 3),
            "moment_end_seconds": round(self.moment_end_seconds, 3),
            "canonical_start_seconds": round(self.canonical_start_seconds, 3),
            "canonical_end_seconds": round(self.canonical_end_seconds, 3),
            "media_start_seconds": round(self.media_start_seconds, 3),
            "media_end_seconds": round(self.media_end_seconds, 3),
            "padding_before_seconds": round(self.applied_padding_before_seconds, 3),
            "padding_after_seconds": round(self.applied_padding_after_seconds, 3),
            "recording_id": self.recording_id,
            "recording_part_index": self.part_index,
            "recording_media_duration_seconds": round(
                next(part.media_duration_seconds for part in timeline.parts
                     if part.part_index == self.part_index), 3),
            "timeline": timeline.describe(),
        }


def clip_key(session_id: str, moment: PositiveMoment) -> str:
    """
    The existing clip identity, unchanged.

    It is the cue pair rather than the timestamps, so re-running the analysis
    and getting a millisecond-different boundary does not create a second clip
    for the same moment.
    """
    return f"{session_id}:{moment.start_cue}:{moment.end_cue}"


def job_key(session_id: str, moment: PositiveMoment) -> str:
    """The existing media-job identity, unchanged, so replays reuse the row."""
    import hashlib
    short = hashlib.md5(session_id.encode()).hexdigest()[:8]
    return f"positive:{short}:{moment.start_cue}:{moment.end_cue}"


def plan_clip(timeline: MediaTimeline, session_id: str, moment: PositiveMoment,
              policy: ClipPolicy | None = None) -> ClipPlan:
    """
    Plan one clip, or refuse with a reason.

    Refusing is a normal outcome. A moment whose media was never recorded has
    no clip, and inventing one would deliver a confident file of the wrong
    thing - which is exactly the failure Phase 6A was run to prevent.
    """
    policy = policy or ClipPolicy()

    if not (moment.end_seconds > moment.start_seconds):
        raise ClipPlanRefused(
            INVALID_MOMENT,
            f"moment {moment.clip_index} ends at or before it starts "
            f"({moment.start_seconds:.3f}s -> {moment.end_seconds:.3f}s)")
    if moment.start_cue is None or moment.end_cue is None:
        raise ClipPlanRefused(
            INVALID_MOMENT,
            f"moment {moment.clip_index} has no cue identity, so it cannot be "
            "given a stable clip key")

    lower = timeline.canonical_start_seconds
    upper = timeline.canonical_end_seconds

    # The moment itself must exist in media. Padding may be reduced; the moment
    # may not.
    if moment.start_seconds < lower or moment.start_seconds >= upper:
        raise ClipPlanRefused(
            MOMENT_OUTSIDE_MEDIA,
            f"moment {moment.clip_index} starts at {moment.start_seconds:.3f}s, "
            f"outside the recorded media ({lower:.3f}s..{upper:.3f}s)")

    moment_end = min(moment.end_seconds, upper)
    if moment_end - moment.start_seconds <= 0:
        raise ClipPlanRefused(
            MOMENT_OUTSIDE_MEDIA,
            f"moment {moment.clip_index} lies entirely past the end of the media")

    start = max(lower, moment.start_seconds - policy.padding_before_seconds)
    end = min(upper, moment_end + policy.padding_after_seconds)

    duration = end - start
    if duration < policy.min_clip_seconds:
        raise ClipPlanRefused(
            MOMENT_TOO_SHORT,
            f"moment {moment.clip_index} yields {duration:.3f}s, below the "
            f"{policy.min_clip_seconds:.0f}s minimum")
    if duration > policy.max_clip_seconds:
        raise ClipPlanRefused(
            MOMENT_TOO_LONG,
            f"moment {moment.clip_index} yields {duration:.3f}s, above the "
            f"{policy.max_clip_seconds:.0f}s maximum")

    try:
        segments = timeline.segments(start, end)
    except MediaCoordinateError as exc:
        raise ClipPlanRefused(MOMENT_OUTSIDE_MEDIA, str(exc)) from None

    if len(segments) > 1:
        # A clip spanning two recordings needs a concatenation the media worker
        # does not implement: it takes one source URL and one range. Refusing
        # is honest; cutting only the first segment would silently deliver a
        # truncated moment.
        raise ClipPlanRefused(
            CROSSES_RECORDING_BOUNDARY,
            f"moment {moment.clip_index} spans {len(segments)} recordings "
            f"({start:.3f}s..{end:.3f}s); the clip worker cuts one source only")

    segment = segments[0]
    return ClipPlan(
        clip_index=moment.clip_index,
        clip_key=clip_key(session_id, moment),
        job_key=job_key(session_id, moment),
        moment_start_seconds=moment.start_seconds,
        moment_end_seconds=moment.end_seconds,
        canonical_start_seconds=start,
        canonical_end_seconds=end,
        media_start_seconds=segment.media_start_seconds,
        media_end_seconds=segment.media_end_seconds,
        applied_padding_before_seconds=moment.start_seconds - start,
        applied_padding_after_seconds=end - moment_end,
        recording_id=segment.recording_id,
        user_id=segment.user_id,
        meeting_id=segment.meeting_id,
        part_index=segment.part_index,
        cut_mode=policy.cut_mode,
        segments=segments,
    )


def plan_lecture(timeline: MediaTimeline, session_id: str,
                 moments: list[PositiveMoment],
                 policy: ClipPolicy | None = None) -> tuple[list[ClipPlan], list[dict]]:
    """
    Plan every moment of one lecture.

    Returns (plans, refusals). One unplannable moment does not discard the
    others: a lecture with five good moments and one that ran past the end of
    the recording should still deliver five clips, and should still say why the
    sixth is missing.
    """
    plans: list[ClipPlan] = []
    refusals: list[dict] = []
    for moment in moments:
        try:
            plans.append(plan_clip(timeline, session_id, moment, policy))
        except ClipPlanRefused as exc:
            refusals.append({"clip_index": moment.clip_index,
                             "code": exc.code, "detail": str(exc)})
    return plans, refusals
