"""
Phase 6A: find each transcript part's recording and measure it.

WHAT THIS DOES
--------------
For one lecture: for every SELECTED transcript part, ask Graph which recording
carries the same content, read that recording's movie header over a range
request, and persist the measurement. The result is the input to
`MediaTimeline`.

WHY THE CORRELATION ID AND NOT THE DATES
----------------------------------------
`contentCorrelationId` is Microsoft's own statement that a transcript and a
recording came from the same stretch of the same call. Matching on timestamps
instead would work until a call was restarted twice in a minute, and then it
would match the wrong file - silently, because both candidates look plausible.

WHAT IT COSTS
-------------
One Graph listing and one ranged read per part. The ranged read pulls the movie
header only: about four megabytes out of nine hundred.

WHAT IT NEVER DOES
------------------
It does not download a recording, store a URL, log a token or write to any
legacy table.
"""
from dataclasses import dataclass
from urllib.parse import quote

from app.media.coordinates import (
    MEDIA_COORDINATE_VERSION,
    MediaCoordinateError,
    MediaTimeline,
    NO_RECORDED_MEDIA,
    RecordingPart,
)
from app.media.mp4 import DEFAULT_HEADER_BYTES, MediaProbe, Mp4ReadError, read_movie_header


SELECTED_PARTS = """
SELECT p.part_index, p.part_offset_ms, s.selection_id,
       a.content_correlation_id, p.provider_created_at, p.provider_end_at,
       l.meeting_id, l.meeting_lookup_user_id
  FROM public.lecture_sessions l
  JOIN public.lecture_transcript_selections s
       ON s.lecture_id = l.lecture_id AND s.selection_status = 'SELECTED'
  JOIN public.lecture_transcript_selection_parts p
       ON p.selection_id = s.selection_id
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = p.artifact_id
 WHERE l.lecture_id = %s
 ORDER BY p.part_index
"""


class RecordingUnavailable(Exception):
    """Graph has no recording for a transcript part that needs one."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


NO_CORRELATED_RECORDING = "NO_CORRELATED_RECORDING"
AMBIGUOUS_RECORDING = "AMBIGUOUS_RECORDING"
UNREADABLE_MEDIA = "UNREADABLE_MEDIA"
NO_SELECTED_TRANSCRIPT = "NO_SELECTED_TRANSCRIPT"


@dataclass(frozen=True)
class MeasuredPart:
    part_index: int
    canonical_offset_ms: int
    selection_id: object
    user_id: str
    meeting_id: str
    recording_id: str
    content_correlation_id: str | None
    probe: MediaProbe
    size_bytes: int | None
    provider_created_at: object
    provider_end_at: object

    def as_row(self) -> dict:
        return {
            "part_index": self.part_index,
            "canonical_offset_ms": self.canonical_offset_ms,
            "selection_id": self.selection_id,
            "user_id": self.user_id,
            "meeting_id": self.meeting_id,
            "recording_id": self.recording_id,
            "content_correlation_id": self.content_correlation_id,
            "media_duration_seconds": round(self.probe.duration_seconds, 3),
            # Every stream measured in Phase 6A reported start_time 0.000, so
            # there is no edit list to compensate for. Recorded anyway: if a
            # future recording does carry one, the planner must see it rather
            # than inherit today's luck.
            "media_start_time_seconds": 0.0,
            "media_duration_source": self.probe.source,
            "container": self.probe.container,
            "video_codec": self.probe.video_codec,
            "audio_codec": self.probe.audio_codec,
            "size_bytes": self.size_bytes,
            "provider_created_at": self.provider_created_at,
            "provider_end_at": self.provider_end_at,
            "media_coordinate_version": MEDIA_COORDINATE_VERSION,
        }


def content_path(user_id: str, meeting_id: str, recording_id: str) -> str:
    """
    The one route that works.

    The driveItem route returns 403 for this application registration; this is
    the callRecording content route, which Phase 6A proved returns the bytes.
    """
    return (f"/users/{user_id}/onlineMeetings/{meeting_id}"
            f"/recordings/{recording_id}/content")


class RecordingMeasurement:
    """Measures a lecture's recordings. One Graph client, one repository."""

    def __init__(self, *, graph, repository, header_bytes: int = DEFAULT_HEADER_BYTES):
        self.graph = graph
        self.repository = repository
        self.header_bytes = header_bytes

    # -- Graph ---------------------------------------------------------------

    def _recording_for(self, user_id, meeting_id, correlation_id) -> dict:
        if not correlation_id:
            raise RecordingUnavailable(
                NO_CORRELATED_RECORDING,
                "the transcript part carries no contentCorrelationId")
        expression = quote(f"contentCorrelationId eq '{correlation_id}'", safe="")
        path = (f"/users/{user_id}/onlineMeetings/{meeting_id}"
                f"/recordings?$filter={expression}")
        items = self.graph.get_json(path).get("value") or []
        if not items:
            raise RecordingUnavailable(
                NO_CORRELATED_RECORDING,
                "Graph reported no recording correlated with this transcript part")
        if len(items) > 1:
            # Two recordings claiming the same stretch of call is not something
            # to pick between. Refusing keeps a wrong file out of a clip.
            raise RecordingUnavailable(
                AMBIGUOUS_RECORDING,
                f"{len(items)} recordings share one contentCorrelationId")
        return items[0]

    def _measure(self, user_id, meeting_id, recording_id) -> tuple[MediaProbe, int | None]:
        path = content_path(user_id, meeting_id, recording_id)
        response = self.graph.request(
            "GET", path, accept="*/*",
            headers={"Range": f"bytes=0-{self.header_bytes - 1}"})
        try:
            probe = read_movie_header(response.body)
        except Mp4ReadError as exc:
            raise RecordingUnavailable(UNREADABLE_MEDIA, str(exc)) from None
        return probe, None

    # -- the operation --------------------------------------------------------

    def measure_lecture(self, connection, lecture_id) -> list[MeasuredPart]:
        rows = connection.execute(SELECTED_PARTS, (str(lecture_id),)).fetchall()
        if not rows:
            raise RecordingUnavailable(
                NO_SELECTED_TRANSCRIPT,
                "the lecture has no selected transcript to align media against")

        measured: list[MeasuredPart] = []
        for (part_index, offset_ms, selection_id, correlation_id,
             created_at, end_at, meeting_id, user_id) in rows:
            recording = self._recording_for(user_id, meeting_id, correlation_id)
            probe, size = self._measure(user_id, meeting_id, recording["id"])
            measured.append(MeasuredPart(
                part_index=part_index, canonical_offset_ms=offset_ms,
                selection_id=selection_id, user_id=user_id,
                meeting_id=meeting_id, recording_id=recording["id"],
                content_correlation_id=correlation_id, probe=probe,
                size_bytes=size, provider_created_at=created_at,
                provider_end_at=end_at))
        return measured

    def persist(self, connection, lecture_id, measured: list[MeasuredPart]) -> int:
        for part in measured:
            self.repository.upsert(connection, lecture_id, part.as_row())
        return len(measured)


def timeline_from_rows(rows: list[dict]) -> MediaTimeline:
    """Build the transform's input from persisted measurements."""
    if not rows:
        raise MediaCoordinateError(
            NO_RECORDED_MEDIA,
            "no measured recording parts are stored for this lecture")
    return MediaTimeline([
        RecordingPart(
            part_index=row["part_index"],
            canonical_offset_seconds=row["canonical_offset_ms"] / 1000.0,
            media_duration_seconds=float(row["media_duration_seconds"]),
            recording_id=row["recording_id"],
            user_id=row["user_id"],
            meeting_id=row["meeting_id"],
            correlation_id=row.get("content_correlation_id"))
        for row in rows])
