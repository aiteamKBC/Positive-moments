from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID


# --- per-lecture acquisition outcomes ---------------------------------------
TRANSCRIPTS_FOUND = "TRANSCRIPTS_FOUND"
NO_TRANSCRIPTS_AVAILABLE = "NO_TRANSCRIPTS_AVAILABLE"
TRANSCRIPT_NOT_READY = "TRANSCRIPT_NOT_READY"
BLOCKED_TRANSCRIPT_ACCESS = "BLOCKED_TRANSCRIPT_ACCESS"
CONTENT_STORED = "CONTENT_STORED"
CONTENT_STORED_NO_SPEAKER_ATTRIBUTION = "CONTENT_STORED_NO_SPEAKER_ATTRIBUTION"
ERROR = "ERROR"

# --- per-artifact statuses ---------------------------------------------------
DISCOVERED = "DISCOVERED"
TRANSCRIPT_NOT_FOUND = "TRANSCRIPT_NOT_FOUND"
CONTENT_FETCH_ERROR = "CONTENT_FETCH_ERROR"

# --- distinguishable provider failure reasons --------------------------------
# An empty but SUCCESSFUL Graph collection is NO_TRANSCRIPTS_AVAILABLE and is
# never conflated with any of these.
GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED = "GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED"
SPEAKER_ATTRIBUTION_NOT_ALLOWED = "SPEAKER_ATTRIBUTION_NOT_ALLOWED"
APPLICATION_ACCESS_POLICY_ERROR = "APPLICATION_ACCESS_POLICY_ERROR"
GRAPH_PERMISSION_ERROR = "GRAPH_PERMISSION_ERROR"
PROVIDER_ERROR = "PROVIDER_ERROR"

PROVIDER_MICROSOFT_GRAPH = "MICROSOFT_GRAPH"
VTT = "text/vtt"


@dataclass(frozen=True)
class ReadyLecture:
    """A canonical Phase 1 lecture that is safe to acquire transcripts for."""

    lecture_id: UUID
    meeting_id: str
    meeting_lookup_user_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    session_date: date
    subject: str
    module: str | None
    meeting_lookup_context_source: str | None = None
    join_url_oid_hint_status: str | None = None


@dataclass(frozen=True)
class TranscriptArtifact:
    """One raw Graph callTranscript, exactly as the provider described it."""

    provider_transcript_id: str
    provider_created_at: datetime | None
    provider_end_at: datetime | None
    provider_call_id: str | None
    content_correlation_id: str | None
    provider_meeting_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptListing:
    """Outcome of listing one meeting's transcripts. Empty != failed."""

    status: str
    artifacts: list[TranscriptArtifact] = field(default_factory=list)
    reason: str | None = None
    http_status: int | None = None
    provider_code: str | None = None


@dataclass(frozen=True)
class TranscriptContent:
    """Outcome of fetching one artifact's raw content."""

    status: str
    raw: bytes | None = None
    content_format: str | None = None
    speaker_attribution: bool | None = None
    reason: str | None = None
    http_status: int | None = None
    provider_code: str | None = None
    used_speaker_attribution_fallback: bool = False
