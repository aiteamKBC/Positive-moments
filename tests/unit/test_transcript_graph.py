"""
Phase 2A Graph transcript gateway.

Covers the explicit user-object-ID endpoints, the full provider error taxonomy,
and the rule that a successful EMPTY collection is never reported as a failure.
"""
import pytest

from app.transcripts.graph import TranscriptGateway, classify_graph_error, has_speaker_attribution
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
)
from automation.lecture_parts.graph_client import GraphError, GraphResponse


USER_ID = "737679b4-8eac-4fe9-a491-76d8cdf65f6d"
MEETING_ID = "MSo3Mzc2*0**19:abc@thread.tacv2"
ATTRIBUTED = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Good morning.</v>\n"
UNATTRIBUTED = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nGood morning.\n"


def _row(transcript_id, **extra):
    return {
        "id": transcript_id,
        "meetingId": MEETING_ID,
        "callId": "call-1",
        "contentCorrelationId": "corr-1-20260904_075836",
        "createdDateTime": "2026-09-04T07:58:36.9077226Z",
        "endDateTime": "2026-09-04T10:11:43.1277226Z",
        "transcriptContentUrl": "https://graph.microsoft.com/v1.0/x/content",
        **extra,
    }


class FakeClient:
    def __init__(self, rows=None, content=None):
        self.rows = rows if rows is not None else []
        self.content = content
        self.collection_paths = []
        self.content_paths = []
        self.accepts = []

    def get_collection(self, path, *, headers=None):
        self.collection_paths.append(path)
        if isinstance(self.rows, GraphError):
            raise self.rows
        return self.rows

    def request(self, method, path, *, accept="application/json", headers=None):
        self.content_paths.append(path)
        self.accepts.append(accept)
        payload = self.content
        if isinstance(payload, dict):
            for fragment, value in payload.items():
                if fragment in path:
                    payload = value
                    break
            else:
                payload = payload.get("*")
        if isinstance(payload, GraphError):
            raise payload
        return GraphResponse(payload if payload is not None else b"", VTT, 200)


# --- listing ------------------------------------------------------------------


def test_listing_uses_the_explicit_user_object_id_endpoint():
    client = FakeClient([_row("t-1")])
    TranscriptGateway(client).list_transcripts(USER_ID, MEETING_ID)
    path = client.collection_paths[0]
    assert path.startswith(f"/users/{USER_ID}/onlineMeetings/")
    assert path.endswith("/transcripts")
    assert "/me/" not in path
    # The meeting ID is percent-encoded, never interpolated raw.
    assert "19:abc@thread.tacv2" not in path


def test_zero_transcript_artifacts_is_a_successful_empty_answer():
    result = TranscriptGateway(FakeClient([])).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == NO_TRANSCRIPTS_AVAILABLE
    assert result.artifacts == []
    assert result.reason is None          # not an error
    assert result.provider_code is None


def test_one_transcript_artifact_is_parsed_with_provider_fields():
    result = TranscriptGateway(FakeClient([_row("t-1")])).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == TRANSCRIPTS_FOUND
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.provider_transcript_id == "t-1"
    assert artifact.provider_created_at.isoformat() == "2026-09-04T07:58:36.907722+00:00"
    assert artifact.provider_end_at.isoformat() == "2026-09-04T10:11:43.127722+00:00"
    assert artifact.provider_call_id == "call-1"
    assert artifact.content_correlation_id == "corr-1-20260904_075836"


def test_multiple_transcript_artifacts_are_all_preserved():
    """A series meeting exposes every occurrence's transcripts; keep them all."""
    rows = [_row(f"t-{index}") for index in range(14)]
    result = TranscriptGateway(FakeClient(rows)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == TRANSCRIPTS_FOUND
    assert [a.provider_transcript_id for a in result.artifacts] == [f"t-{i}" for i in range(14)]


def test_provider_failure_is_distinct_from_an_empty_success():
    broken = GraphError("Microsoft Graph", 500, "InternalServerError", "boom")
    result = TranscriptGateway(FakeClient(broken)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == PROVIDER_ERROR
    assert result.status != NO_TRANSCRIPTS_AVAILABLE
    assert result.http_status == 500
    assert result.provider_code == "InternalServerError"


def test_graph_access_to_transcripts_disabled_is_reported_as_blocked():
    error = GraphError("Microsoft Graph", 403, "GraphAccessToTranscriptsDisabled",
                       "Tenant admin has disabled Graph access to transcripts")
    result = TranscriptGateway(FakeClient(error)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == BLOCKED_TRANSCRIPT_ACCESS
    assert result.reason == GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED


def test_application_access_policy_error_is_reported_distinctly():
    error = GraphError("Microsoft Graph", 403, "Forbidden",
                       "Application access policy not configured for this user")
    result = TranscriptGateway(FakeClient(error)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == BLOCKED_TRANSCRIPT_ACCESS
    assert result.reason == APPLICATION_ACCESS_POLICY_ERROR


def test_generic_403_is_distinct_from_zero_transcripts_and_from_policy_errors():
    error = GraphError("Microsoft Graph", 403, "Forbidden", "Insufficient privileges")
    result = TranscriptGateway(FakeClient(error)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == BLOCKED_TRANSCRIPT_ACCESS
    assert result.reason == GRAPH_PERMISSION_ERROR
    assert result.reason != APPLICATION_ACCESS_POLICY_ERROR
    assert result.status != NO_TRANSCRIPTS_AVAILABLE


def test_404_on_listing_is_transcript_not_found():
    error = GraphError("Microsoft Graph", 404, "NotFound", "meeting not found")
    result = TranscriptGateway(FakeClient(error)).list_transcripts(USER_ID, MEETING_ID)
    assert result.status == TRANSCRIPT_NOT_FOUND


# --- content ------------------------------------------------------------------


def test_content_request_targets_the_user_object_id_and_prefers_vtt():
    client = FakeClient([], content=ATTRIBUTED)
    result = TranscriptGateway(client).fetch_content(USER_ID, MEETING_ID, "t-1")
    path = client.content_paths[0]
    assert path.startswith(f"/users/{USER_ID}/onlineMeetings/")
    assert path.endswith("/transcripts/t-1/content?$format=text/vtt")
    assert client.accepts[0] == VTT
    assert result.raw == ATTRIBUTED
    assert result.content_format == VTT
    assert result.speaker_attribution is True


def test_speaker_attribution_is_decided_from_the_bytes_not_the_request():
    client = FakeClient([], content=UNATTRIBUTED)
    result = TranscriptGateway(client).fetch_content(USER_ID, MEETING_ID, "t-1")
    assert result.speaker_attribution is False
    assert has_speaker_attribution(ATTRIBUTED) is True
    assert has_speaker_attribution(UNATTRIBUTED) is False


def test_speaker_attribution_not_allowed_triggers_one_controlled_fallback():
    denied = GraphError("Microsoft Graph", 403, "SpeakerAttributionNotAllowed",
                        "Speaker attribution is not allowed for this tenant")
    client = FakeClient([], content={"$format=text/vtt": denied, "*": UNATTRIBUTED})
    result = TranscriptGateway(client).fetch_content(USER_ID, MEETING_ID, "t-1")

    assert len(client.content_paths) == 2                    # exactly one fallback
    assert "$format" in client.content_paths[0]
    assert "$format" not in client.content_paths[1]
    assert result.raw == UNATTRIBUTED
    assert result.used_speaker_attribution_fallback is True
    # Never pretend unattributed content is speaker-attributed.
    assert result.speaker_attribution is False
    assert result.reason == SPEAKER_ATTRIBUTION_NOT_ALLOWED


def test_the_fallback_can_be_disabled_and_reports_the_original_cause():
    denied = GraphError("Microsoft Graph", 403, "SpeakerAttributionNotAllowed", "not allowed")
    client = FakeClient([], content={"$format=text/vtt": denied, "*": UNATTRIBUTED})
    result = TranscriptGateway(client, allow_unattributed_fallback=False).fetch_content(
        USER_ID, MEETING_ID, "t-1")
    assert len(client.content_paths) == 1
    assert result.raw is None
    assert result.reason == SPEAKER_ATTRIBUTION_NOT_ALLOWED


def test_no_fallback_for_an_unrelated_content_failure():
    error = GraphError("Microsoft Graph", 500, "InternalServerError", "boom")
    client = FakeClient([], content=error)
    result = TranscriptGateway(client).fetch_content(USER_ID, MEETING_ID, "t-1")
    assert len(client.content_paths) == 1
    assert result.status == CONTENT_FETCH_ERROR
    assert result.raw is None


def test_transcript_content_404_is_handled_as_not_found():
    error = GraphError("Microsoft Graph", 404, "NotFound", "transcript not found")
    result = TranscriptGateway(FakeClient([], content=error)).fetch_content(
        USER_ID, MEETING_ID, "t-1")
    assert result.status == TRANSCRIPT_NOT_FOUND
    assert result.raw is None


def test_empty_content_body_is_transcript_not_ready_not_stored():
    result = TranscriptGateway(FakeClient([], content=b"   ")).fetch_content(
        USER_ID, MEETING_ID, "t-1")
    assert result.status == TRANSCRIPT_NOT_READY
    assert result.raw is None


@pytest.mark.parametrize("status, code, message, expected", [
    (403, "GraphAccessToTranscriptsDisabled", "", GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED),
    (403, "SpeakerAttributionNotAllowed", "", SPEAKER_ATTRIBUTION_NOT_ALLOWED),
    (403, "Forbidden", "Application access policy is missing", APPLICATION_ACCESS_POLICY_ERROR),
    (403, "Forbidden", "Insufficient privileges", GRAPH_PERMISSION_ERROR),
    (404, "NotFound", "", TRANSCRIPT_NOT_FOUND),
    (500, "InternalServerError", "", PROVIDER_ERROR),
    (429, "TooManyRequests", "", PROVIDER_ERROR),
])
def test_graph_error_classification_is_specific(status, code, message, expected):
    assert classify_graph_error(GraphError("Microsoft Graph", status, code, message)) == expected
