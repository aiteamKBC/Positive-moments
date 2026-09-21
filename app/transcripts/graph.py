"""
Microsoft Graph v1.0 transcript acquisition.

USER CONTEXT
------------
Every call uses the Graph user object ID Phase 1 already resolved and persisted
as `lecture_sessions.meeting_lookup_user_id`. There is no `/me`, no rederivation
from the calendar organizer (a Unified Group cohort mailbox, not a user), and no
re-parsing of the JoinWebUrl Oid.

    GET /users/{userObjectId}/onlineMeetings/{meetingId}/transcripts
    GET /users/{userObjectId}/onlineMeetings/{meetingId}/transcripts/{id}/content

SERIES SCOPE
------------
A Teams *series* onlineMeeting exposes the transcripts of EVERY occurrence in
the series, so this listing routinely returns artifacts from other dates.
Phase 2A preserves all of them verbatim; occurrence attribution is Phase 2B.

Authentication is the existing app-only client-credentials implementation with
its in-memory, refresh-before-expiry token cache. No second auth system exists,
and no token, Authorization header, or secret is logged or persisted.
"""
import urllib.parse

from app.common.time import parse_graph_datetime
from app.transcripts.models import (
    APPLICATION_ACCESS_POLICY_ERROR,
    BLOCKED_TRANSCRIPT_ACCESS,
    CONTENT_FETCH_ERROR,
    GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED,
    GRAPH_PERMISSION_ERROR,
    NO_TRANSCRIPTS_AVAILABLE,
    PROVIDER_ERROR,
    SPEAKER_ATTRIBUTION_NOT_ALLOWED,
    TRANSCRIPT_NOT_FOUND,
    TRANSCRIPT_NOT_READY,
    TRANSCRIPTS_FOUND,
    VTT,
    TranscriptArtifact,
    TranscriptContent,
    TranscriptListing,
)
from app.graph.transport import GraphError


# Provider error-code fragments, matched case-insensitively against the
# sanitized Graph code and message. Ordered: the most specific wins.
_BLOCKED_REASONS = (
    ("graphaccesstotranscriptsdisabled", GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED),
    ("access to transcripts", GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED),
    ("speakerattributionnotallowed", SPEAKER_ATTRIBUTION_NOT_ALLOWED),
    ("speaker attribution", SPEAKER_ATTRIBUTION_NOT_ALLOWED),
    ("application access policy", APPLICATION_ACCESS_POLICY_ERROR),
)


def classify_graph_error(exc: GraphError) -> str:
    """Map a sanitized Graph failure to one distinguishable reason."""
    haystack = f"{exc.code} {getattr(exc, 'message', '')}".casefold()
    for fragment, reason in _BLOCKED_REASONS:
        if fragment in haystack:
            return reason
    if exc.status == 403:
        return GRAPH_PERMISSION_ERROR
    if exc.status == 404:
        return TRANSCRIPT_NOT_FOUND
    return PROVIDER_ERROR


def has_speaker_attribution(raw: bytes) -> bool:
    """
    Decide attribution from the CONTENT, never from what we asked for.

    Speaker-attributed WebVTT carries `<v Speaker Name>` voice spans. Absent
    those, the payload is unattributed and must be recorded as such.
    """
    return b"<v " in raw


class TranscriptGateway:
    def __init__(self, client, *, allow_unattributed_fallback: bool = True):
        self.client = client
        self.allow_unattributed_fallback = allow_unattributed_fallback

    @staticmethod
    def _base(user_object_id: str, meeting_id: str) -> str:
        user = urllib.parse.quote(user_object_id, safe="")
        meeting = urllib.parse.quote(meeting_id, safe="")
        return f"/users/{user}/onlineMeetings/{meeting}/transcripts"

    def list_transcripts(self, user_object_id: str, meeting_id: str) -> TranscriptListing:
        """
        Every transcript artifact Graph exposes for this meeting.

        A successful empty collection is NO_TRANSCRIPTS_AVAILABLE. That is a
        real, truthful provider answer and is never reported as an error.
        """
        try:
            rows = self.client.get_collection(self._base(user_object_id, meeting_id))
        except GraphError as exc:
            reason = classify_graph_error(exc)
            status = (
                TRANSCRIPT_NOT_FOUND if reason == TRANSCRIPT_NOT_FOUND
                else BLOCKED_TRANSCRIPT_ACCESS if reason in (
                    GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED,
                    APPLICATION_ACCESS_POLICY_ERROR,
                    GRAPH_PERMISSION_ERROR,
                )
                else PROVIDER_ERROR
            )
            return TranscriptListing(
                status=status, reason=reason,
                http_status=exc.status, provider_code=exc.code,
            )
        artifacts = [self._artifact(row) for row in rows if row.get("id")]
        if not artifacts:
            return TranscriptListing(status=NO_TRANSCRIPTS_AVAILABLE)
        return TranscriptListing(status=TRANSCRIPTS_FOUND, artifacts=artifacts)

    @staticmethod
    def _artifact(row: dict) -> TranscriptArtifact:
        created = row.get("createdDateTime")
        ended = row.get("endDateTime")
        return TranscriptArtifact(
            provider_transcript_id=str(row["id"]),
            provider_created_at=parse_graph_datetime(created, "UTC") if created else None,
            provider_end_at=parse_graph_datetime(ended, "UTC") if ended else None,
            provider_call_id=row.get("callId"),
            content_correlation_id=row.get("contentCorrelationId"),
            provider_meeting_id=row.get("meetingId"),
            # Structured provider metadata only; transcriptContentUrl is a
            # pointer, not content, and no token is present in it.
            metadata={
                key: value for key, value in row.items()
                if key in {"meetingOrganizer", "transcriptContentUrl"}
            },
        )

    def fetch_content(self, user_object_id: str, meeting_id: str,
                      provider_transcript_id: str) -> TranscriptContent:
        """
        Raw provider bytes for one artifact, preferring speaker-attributed VTT.

        The primary request asks for `$format=text/vtt`. If, and only if, that
        fails specifically because speaker attribution is not allowed, one
        controlled fallback re-requests the same `/content` endpoint without the
        `$format` override. Attribution of whatever comes back is then decided
        by inspecting the bytes, so unattributed content is never recorded as
        attributed.
        """
        base = self._base(user_object_id, meeting_id)
        transcript = urllib.parse.quote(provider_transcript_id, safe="")
        content_path = f"{base}/{transcript}/content"

        primary = self._request(f"{content_path}?$format=text/vtt")
        if primary.status != CONTENT_FETCH_ERROR or primary.reason != SPEAKER_ATTRIBUTION_NOT_ALLOWED:
            return primary
        if not self.allow_unattributed_fallback:
            return primary

        fallback = self._request(content_path)
        if fallback.raw is None:
            # Report the original, more specific cause.
            return primary
        return TranscriptContent(
            status=fallback.status,
            raw=fallback.raw,
            content_format=fallback.content_format,
            speaker_attribution=fallback.speaker_attribution,
            reason=SPEAKER_ATTRIBUTION_NOT_ALLOWED,
            http_status=fallback.http_status,
            used_speaker_attribution_fallback=True,
        )

    def _request(self, path: str) -> TranscriptContent:
        try:
            response = self.client.request("GET", path, accept=VTT)
        except GraphError as exc:
            reason = classify_graph_error(exc)
            status = (
                TRANSCRIPT_NOT_FOUND if reason == TRANSCRIPT_NOT_FOUND
                else BLOCKED_TRANSCRIPT_ACCESS if reason in (
                    GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED,
                    APPLICATION_ACCESS_POLICY_ERROR,
                    GRAPH_PERMISSION_ERROR,
                )
                else CONTENT_FETCH_ERROR
            )
            if exc.status in (404, 503) and reason == PROVIDER_ERROR:
                status = TRANSCRIPT_NOT_READY
            return TranscriptContent(
                status=status, reason=reason,
                http_status=exc.status, provider_code=exc.code,
            )
        raw = response.body
        if raw is None or not raw.strip():
            # Graph answered, but there is nothing to store yet.
            return TranscriptContent(
                status=TRANSCRIPT_NOT_READY, reason="EMPTY_CONTENT_BODY",
                http_status=response.status,
            )
        return TranscriptContent(
            status="FETCHED",
            raw=raw,
            content_format=VTT,
            speaker_attribution=has_speaker_attribution(raw),
            http_status=response.status,
        )
