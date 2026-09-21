"""
Phase 6A: the ONE media coordinate transform.

WHAT THIS ANSWERS
-----------------
Given a time on the canonical transcript timeline, which recording file is that
moment in, and how many seconds into that file does it sit?

Every consumer - positive clips, lecture parts, anything later - must ask this
module. Offset arithmetic scattered across two pipelines is two pipelines that
cut different moments from the same lecture.

WHAT WAS MEASURED, AND HOW
--------------------------
This is not inferred from Graph metadata. It was proven in Phase 6A against the
recording bytes themselves, read over HTTP range requests from

    GET /users/{id}/onlineMeetings/{id}/recordings/{id}/content

(the callRecording content route; the driveItem route is 403 for this app and
is not used). For each transcript part of three lectures - the extreme
non-zero-origin case, a non-zero control and a near-zero control - FFprobe
reported the real container duration and FFmpeg's silencedetect measured the
speech/silence pattern, which was then compared against the pattern the
transcript predicts.

The findings the model below encodes:

1.  EVERY SELECTED TRANSCRIPT PART HAS ITS OWN RECORDING. Microsoft caps both
    a transcript and a recording at four hours, so a long call arrives as
    several parts and several recordings, one recording per part.

2.  `callRecording.createdDateTime` EQUALS that part's transcript
    `provider_created_at` to the microsecond, and the recording's real media
    duration equals that part's transcript window. Measured delta: +0.000s on
    every part tested.

3.  THEREFORE, WITHIN A PART, MEDIA TIME IS CANONICAL TIME MINUS THE PART'S
    OFFSET. Every stream reports `start_time = 0.000`, so there is no edit
    list to compensate for.

4.  CONSECUTIVE RECORDINGS OVERLAP. The next recording starts a little before
    the previous one ends (64.0s for one measured lecture, 61.6s for another),
    because Teams begins the next segment before closing the last. So a
    canonical instant inside the overlap exists in BOTH files. The later part
    wins, which makes the mapping a function rather than a choice.

WHAT THIS REFUTES
-----------------
The legacy assumption `media_time == transcript_time` holds only for a
single-part lecture whose first cue is near zero. For a lecture whose call ran
for hours before the teaching started, or one that spans two recordings, it
produces a time past the end of the file - a request that fails loudly - or,
worse, the wrong moment inside a later file.

The competing "the timeline starts at the first cue" reading was measured and
refuted: at the position it predicts, the recording is silent.
"""
from dataclasses import dataclass


MEDIA_COORDINATE_VERSION = "call_relative_part_media_v1"

# Sub-frame differences are not a coordinate problem. Two boundaries closer
# than this are the same boundary.
EPSILON_SECONDS = 0.001

# How far a transcript may legitimately overrun the end of its own recording.
#
# Measured, not chosen: one lecture in the registry ("Martech - Fri") has a
# final cue ending 0.243s after the last frame of its media. A transcript cue
# carries a rounded end time and a recording stops when the host stops it, so a
# fractional overrun at the very end is normal rather than a mapping error.
#
# The tolerance applies ONLY to the tail of the timeline and only by clamping
# to media that exists. It never extends the timeline, never applies at the
# start, and anything beyond it is still refused - a lecture whose transcript
# runs thirty seconds past its recording has a real problem that must not be
# rounded away.
TAIL_TOLERANCE_SECONDS = 1.0


class MediaCoordinateError(Exception):
    """A canonical time that cannot be expressed in media coordinates."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


OUTSIDE_RECORDED_MEDIA = "OUTSIDE_RECORDED_MEDIA"
NO_RECORDED_MEDIA = "NO_RECORDED_MEDIA"
INVALID_RANGE = "INVALID_RANGE"
INVALID_TIMELINE = "INVALID_TIMELINE"


@dataclass(frozen=True)
class RecordingPart:
    """
    One transcript part and the single recording that carries it.

    `canonical_offset_seconds` is `lecture_transcript_selection_parts.
    part_offset_ms / 1000` - where this part's local time zero sits on the
    canonical timeline.

    `media_duration_seconds` is the MEASURED duration of the recording file,
    from FFprobe or from the MP4 movie header. It is never the Graph
    `endDateTime - createdDateTime` window unless that value has been checked
    against the file, and it is never the free-text meeting duration.
    """
    part_index: int
    canonical_offset_seconds: float
    media_duration_seconds: float
    recording_id: str
    user_id: str
    meeting_id: str
    correlation_id: str | None = None

    @property
    def canonical_start(self) -> float:
        return self.canonical_offset_seconds

    @property
    def canonical_end(self) -> float:
        """Where this part's own media runs out, on the canonical timeline."""
        return self.canonical_offset_seconds + self.media_duration_seconds


@dataclass(frozen=True)
class MediaPoint:
    """One canonical instant, located in one recording."""
    part_index: int
    recording_id: str
    user_id: str
    meeting_id: str
    media_seconds: float
    canonical_seconds: float


@dataclass(frozen=True)
class MediaSegment:
    """
    A contiguous span of ONE recording.

    A canonical range that crosses a recording boundary produces several of
    these, in order. Producing media for such a range means cutting each
    segment and concatenating them - never cutting one range from one file and
    hoping the boundary did not matter.
    """
    part_index: int
    recording_id: str
    user_id: str
    meeting_id: str
    media_start_seconds: float
    media_end_seconds: float
    canonical_start_seconds: float
    canonical_end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.media_end_seconds - self.media_start_seconds


class MediaTimeline:
    """
    The canonical timeline of one lecture, mapped onto its recordings.

    Construction validates the shape of the input rather than trusting it: an
    empty part list, a non-positive duration or a negative offset are all
    refused here, because every one of them would otherwise surface later as a
    plausible-looking timestamp.
    """

    version = MEDIA_COORDINATE_VERSION

    def __init__(self, parts: list[RecordingPart]):
        if not parts:
            raise MediaCoordinateError(
                NO_RECORDED_MEDIA,
                "the lecture has no recorded media parts")
        ordered = sorted(parts, key=lambda part: part.canonical_offset_seconds)
        for part in ordered:
            if part.media_duration_seconds <= 0:
                raise MediaCoordinateError(
                    INVALID_TIMELINE,
                    f"part {part.part_index} has a non-positive media duration")
            if part.canonical_offset_seconds < 0:
                raise MediaCoordinateError(
                    INVALID_TIMELINE,
                    f"part {part.part_index} has a negative canonical offset")
        if ordered[0].canonical_offset_seconds > EPSILON_SECONDS:
            raise MediaCoordinateError(
                INVALID_TIMELINE,
                "the first part must start the canonical timeline at zero, "
                f"not at {ordered[0].canonical_offset_seconds:.3f}s")
        self.parts = tuple(ordered)

        # Where each part's authority ends. Recordings OVERLAP, so a part's
        # authority stops where the next part begins, not where its own media
        # runs out. This is what makes the mapping single-valued.
        self._upper = tuple(
            [ordered[index + 1].canonical_offset_seconds
             for index in range(len(ordered) - 1)]
            + [ordered[-1].canonical_end])

    # -- the timeline itself -------------------------------------------------

    @property
    def canonical_start_seconds(self) -> float:
        return self.parts[0].canonical_start

    @property
    def canonical_end_seconds(self) -> float:
        """The last canonical instant that exists in any recording."""
        return self._upper[-1]

    @property
    def covered_seconds(self) -> float:
        return self.canonical_end_seconds - self.canonical_start_seconds

    def covers(self, canonical_seconds: float) -> bool:
        return (self.canonical_start_seconds - EPSILON_SECONDS
                <= canonical_seconds
                < self.canonical_end_seconds + EPSILON_SECONDS)

    # -- the transform -------------------------------------------------------

    def locate(self, canonical_seconds: float) -> MediaPoint:
        """
        Where one canonical instant lives. Raises rather than clamping.

        Clamping would turn "this moment was never recorded" into "here is a
        moment that was", which is the failure mode that produces a
        confidently delivered clip of the wrong thing.
        """
        if canonical_seconds < self.canonical_start_seconds - EPSILON_SECONDS:
            raise MediaCoordinateError(
                OUTSIDE_RECORDED_MEDIA,
                f"{canonical_seconds:.3f}s precedes the first recording")
        for index, part in enumerate(self.parts):
            if canonical_seconds < self._upper[index] + EPSILON_SECONDS:
                media = canonical_seconds - part.canonical_offset_seconds
                if media < -EPSILON_SECONDS:
                    raise MediaCoordinateError(
                        OUTSIDE_RECORDED_MEDIA,
                        f"{canonical_seconds:.3f}s falls in a gap before part "
                        f"{part.part_index}")
                media = min(max(media, 0.0), part.media_duration_seconds)
                return MediaPoint(
                    part_index=part.part_index, recording_id=part.recording_id,
                    user_id=part.user_id, meeting_id=part.meeting_id,
                    media_seconds=media, canonical_seconds=canonical_seconds)
        overrun = canonical_seconds - self.canonical_end_seconds
        if overrun <= TAIL_TOLERANCE_SECONDS:
            # The transcript's last cue ends a fraction after the recording
            # does. The final frame is the honest answer; inventing one is not.
            final = self.parts[-1]
            return MediaPoint(
                part_index=final.part_index, recording_id=final.recording_id,
                user_id=final.user_id, meeting_id=final.meeting_id,
                media_seconds=final.media_duration_seconds,
                canonical_seconds=canonical_seconds)
        raise MediaCoordinateError(
            OUTSIDE_RECORDED_MEDIA,
            f"{canonical_seconds:.3f}s is {overrun:.3f}s past the end of the "
            f"recorded media ({self.canonical_end_seconds:.3f}s)")

    def segments(self, canonical_start: float,
                 canonical_end: float) -> list[MediaSegment]:
        """
        Cover [start, end) with one segment per recording, in order.

        The segments are contiguous on the canonical timeline and never
        overlap, so concatenating their media reproduces the requested span
        exactly once - including across the seconds where two recordings
        genuinely contain the same audio.
        """
        if canonical_end <= canonical_start + EPSILON_SECONDS:
            raise MediaCoordinateError(
                INVALID_RANGE,
                f"the range {canonical_start:.3f}s..{canonical_end:.3f}s is "
                "empty or inverted")
        if canonical_start < self.canonical_start_seconds - EPSILON_SECONDS:
            raise MediaCoordinateError(
                OUTSIDE_RECORDED_MEDIA,
                f"the range starts at {canonical_start:.3f}s, before the first "
                f"recording ({self.canonical_start_seconds:.3f}s)")
        if canonical_end > self.canonical_end_seconds + EPSILON_SECONDS:
            overrun = canonical_end - self.canonical_end_seconds
            if overrun > TAIL_TOLERANCE_SECONDS:
                raise MediaCoordinateError(
                    OUTSIDE_RECORDED_MEDIA,
                    f"the range ends {overrun:.3f}s past the end of the recorded "
                    f"media ({self.canonical_end_seconds:.3f}s)")
            # Within tolerance: deliver the media that exists, not a shortfall
            # dressed up as a failure.
            canonical_end = self.canonical_end_seconds

        produced: list[MediaSegment] = []
        cursor = canonical_start
        for index, part in enumerate(self.parts):
            lower = max(part.canonical_offset_seconds, cursor)
            upper = min(self._upper[index], canonical_end)
            if upper <= lower + EPSILON_SECONDS:
                continue
            produced.append(MediaSegment(
                part_index=part.part_index, recording_id=part.recording_id,
                user_id=part.user_id, meeting_id=part.meeting_id,
                media_start_seconds=lower - part.canonical_offset_seconds,
                media_end_seconds=upper - part.canonical_offset_seconds,
                canonical_start_seconds=lower,
                canonical_end_seconds=upper))
            cursor = upper
            if cursor >= canonical_end - EPSILON_SECONDS:
                break
        if not produced:
            raise MediaCoordinateError(
                OUTSIDE_RECORDED_MEDIA,
                f"no recording covers {canonical_start:.3f}s..{canonical_end:.3f}s")
        if cursor < canonical_end - EPSILON_SECONDS:
            raise MediaCoordinateError(
                OUTSIDE_RECORDED_MEDIA,
                f"the recordings stop at {cursor:.3f}s, short of the requested "
                f"{canonical_end:.3f}s")
        return produced

    def crosses_recording_boundary(self, canonical_start: float,
                                   canonical_end: float) -> bool:
        return len(self.segments(canonical_start, canonical_end)) > 1

    def describe(self) -> dict:
        """Provenance for a persisted plan. Carries no URL and no token."""
        return {
            "media_coordinate_version": self.version,
            "canonical_start_seconds": round(self.canonical_start_seconds, 3),
            "canonical_end_seconds": round(self.canonical_end_seconds, 3),
            "parts": [
                {"part_index": part.part_index,
                 "canonical_offset_seconds": round(part.canonical_offset_seconds, 3),
                 "media_duration_seconds": round(part.media_duration_seconds, 3),
                 "canonical_upper_seconds": round(self._upper[index], 3),
                 "recording_id": part.recording_id}
                for index, part in enumerate(self.parts)],
        }
