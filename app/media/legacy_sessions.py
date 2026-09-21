"""
Phase 6B: build the measured media timeline for an ANALYSED legacy lecture.

WHY THIS EXISTS
---------------
The positive-moment analysis covers 15-25 July 2026. The canonical registry
begins on 4 September 2026. They do not overlap, so the lectures that have
clips to cut are exactly the lectures Phase 6A did not measure.

They are still reachable, and by the same proven route. Phase 6B established:

  * `qa_doctors_sessions.session_id` IS the Microsoft Graph transcript id -
    it base64-decodes to a string ending `-TranscriptV2`.
  * `GET /users/{u}/onlineMeetings/{m}/transcripts/{session_id}` returns that
    transcript, including its `contentCorrelationId`.
  * Exactly one recording carries that correlation id, and its
    `createdDateTime` equals the transcript's.
  * Audio alignment on the pilot lecture put the best lag at +1 s, the same
    systematic figure measured on every September recording - so the legacy
    timestamps ARE media timestamps.

So an analysed lecture is a one-part timeline at offset zero, and the ONE
validated transform reduces to the identity on its own. No second coordinate
model is introduced here and none is needed.

FINDING THE MAILBOX
-------------------
Graph addresses a meeting through a user context. The registry already knows
`meeting_lookup_user_id` for many meetings, and that is used first. When it
does not, the known lookup users are tried in a deterministic sorted order and
Graph itself confirms the match by returning the transcript - this is a lookup,
not a guess: a wrong mailbox returns an error rather than a different meeting.
"""
from dataclasses import dataclass
from urllib.parse import quote

from app.media.coordinates import MEDIA_COORDINATE_VERSION
from app.media.mp4 import DEFAULT_HEADER_BYTES, Mp4ReadError, read_movie_header
from app.media.recordings import (
    AMBIGUOUS_RECORDING,
    NO_CORRELATED_RECORDING,
    UNREADABLE_MEDIA,
    RecordingUnavailable,
)


NO_TRANSCRIPT_CONTEXT = "NO_TRANSCRIPT_CONTEXT"

ANALYSED_SESSION = """
SELECT d.session_id, d.subject, d.date, d.meeting_id, d.trainer,
       d.positive_clips, d.clips_analysis_completeness,
       d.recording_drive_id, d.recording_item_id
  FROM public.qa_doctors_sessions d
 WHERE d.session_id = %s
"""

KNOWN_USER_FOR_MEETING = """
SELECT meeting_lookup_user_id
  FROM public.lecture_sessions
 WHERE meeting_id = %s AND meeting_lookup_user_id IS NOT NULL
 LIMIT 1
"""

# Deterministic order, so two runs try the same mailboxes in the same sequence.
ALL_KNOWN_USERS = """
SELECT DISTINCT meeting_lookup_user_id
  FROM public.lecture_sessions
 WHERE meeting_lookup_user_id IS NOT NULL
 ORDER BY 1
"""


@dataclass(frozen=True)
class LegacyRecording:
    session_id: str
    user_id: str
    meeting_id: str
    recording_id: str
    correlation_id: str
    media_duration_seconds: float
    media_duration_source: str
    container: str
    video_codec: str | None
    audio_codec: str | None
    provider_created_at: str | None
    provider_end_at: str | None

    def as_row(self) -> dict:
        """The one part of a legacy lecture's timeline: offset zero."""
        return {
            "part_index": 1,
            "canonical_offset_ms": 0,
            "selection_id": None,
            "user_id": self.user_id,
            "meeting_id": self.meeting_id,
            "recording_id": self.recording_id,
            "content_correlation_id": self.correlation_id,
            "media_duration_seconds": round(self.media_duration_seconds, 3),
            "media_start_time_seconds": 0.0,
            "media_duration_source": self.media_duration_source,
            "container": self.container,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "size_bytes": None,
            "provider_created_at": self.provider_created_at,
            "provider_end_at": self.provider_end_at,
            "media_coordinate_version": MEDIA_COORDINATE_VERSION,
        }


class LegacySessionMeasurement:
    """Resolve and measure the recording behind one analysed legacy lecture."""

    def __init__(self, *, graph, repository, header_bytes: int = DEFAULT_HEADER_BYTES):
        self.graph = graph
        self.repository = repository
        self.header_bytes = header_bytes

    # -- mailbox resolution ---------------------------------------------------

    def candidate_users(self, connection, meeting_id: str) -> list[str]:
        preferred = connection.execute(KNOWN_USER_FOR_MEETING, (meeting_id,)).fetchone()
        others = [row[0] for row in connection.execute(ALL_KNOWN_USERS).fetchall()]
        if preferred:
            return [preferred[0]] + [u for u in others if u != preferred[0]]
        return others

    # -- Graph ----------------------------------------------------------------

    def _transcript(self, users, meeting_id, session_id):
        """The transcript, and the mailbox that could see it."""
        for user in users:
            try:
                payload = self.graph.get_json(
                    f"/users/{user}/onlineMeetings/{meeting_id}"
                    f"/transcripts/{session_id}")
            except Exception:
                continue
            if payload.get("id"):
                return user, payload
        raise RecordingUnavailable(
            NO_TRANSCRIPT_CONTEXT,
            "no known mailbox could read this transcript; the analysed session "
            "cannot be tied to a meeting")

    def _recording(self, user, meeting_id, correlation_id):
        if not correlation_id:
            raise RecordingUnavailable(
                NO_CORRELATED_RECORDING,
                "the transcript carries no contentCorrelationId")
        expression = quote(f"contentCorrelationId eq '{correlation_id}'", safe="")
        items = self.graph.get_json(
            f"/users/{user}/onlineMeetings/{meeting_id}"
            f"/recordings?$filter={expression}").get("value") or []
        if not items:
            raise RecordingUnavailable(
                NO_CORRELATED_RECORDING,
                "Graph reported no recording correlated with this transcript")
        if len(items) > 1:
            raise RecordingUnavailable(
                AMBIGUOUS_RECORDING,
                f"{len(items)} recordings share one contentCorrelationId")
        return items[0]

    def _measure(self, user, meeting_id, recording_id):
        response = self.graph.request(
            "GET",
            f"/users/{user}/onlineMeetings/{meeting_id}"
            f"/recordings/{recording_id}/content",
            accept="*/*",
            headers={"Range": f"bytes=0-{self.header_bytes - 1}"})
        try:
            return read_movie_header(response.body)
        except Mp4ReadError as exc:
            raise RecordingUnavailable(UNREADABLE_MEDIA, str(exc)) from None

    # -- the operation ---------------------------------------------------------

    def measure(self, connection, session_id: str) -> LegacyRecording:
        row = connection.execute(ANALYSED_SESSION, (session_id,)).fetchone()
        if not row:
            raise RecordingUnavailable(
                NO_TRANSCRIPT_CONTEXT, f"no analysed session {session_id!r}")
        meeting_id = row[3]
        users = self.candidate_users(connection, meeting_id)
        user, transcript = self._transcript(users, meeting_id, session_id)
        recording = self._recording(user, meeting_id,
                                    transcript.get("contentCorrelationId"))
        probe = self._measure(user, meeting_id, recording["id"])
        return LegacyRecording(
            session_id=session_id, user_id=user, meeting_id=meeting_id,
            recording_id=recording["id"],
            correlation_id=transcript["contentCorrelationId"],
            media_duration_seconds=probe.duration_seconds,
            media_duration_source=probe.source, container=probe.container,
            video_codec=probe.video_codec, audio_codec=probe.audio_codec,
            provider_created_at=recording.get("createdDateTime"),
            provider_end_at=recording.get("endDateTime"))

    def persist(self, connection, measured: LegacyRecording):
        return self.repository.upsert_legacy(
            connection, measured.session_id, measured.as_row())
