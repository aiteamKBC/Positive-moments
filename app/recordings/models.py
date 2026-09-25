"""
The RECORDING_LINK vocabulary: statuses, the stage state each one maps to,
and the small value types passed between lookup, discovery, matching and the
decision.

Every status here answers ONE question and none is a euphemism for another.
In particular an HTTP/auth/access failure is never "not found": Graph refusing
to answer proves nothing about whether a recording exists, and the legacy n8n
branch reporting 43 access failures as absences is the defect this replaces.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


RECORDING_LINK_VERSION = "recording_link_v1"
MATCH_METHOD = "exact_call_id_subject_timestamp_v9"
# The file name carries the moment recording started on the client, which the
# 2026-09-25 audit measured 17-73 s BEFORE Graph's createdDateTime and never
# after it. A constant, not a setting: widening it is a code review.
MAX_FILE_LEAD_SECONDS = 120

# -- preconditions (no Graph call is made) -----------------------------------
NO_RECORDING_EXPECTED_CANCELLED = "NO_RECORDING_EXPECTED_CANCELLED"
NO_LEGACY_QA_ROW = "NO_LEGACY_QA_ROW"
RECORDING_ALREADY_LINKED = "RECORDING_ALREADY_LINKED"
ORGANIZER_LOOKUP_ID_MISSING = "ORGANIZER_LOOKUP_ID_MISSING"
SESSION_CALL_ID_MISSING = "SESSION_CALL_ID_MISSING"
LECTURE_IDENTITY_MISMATCH = "LECTURE_IDENTITY_MISMATCH"

# -- Graph recording metadata (organizer-aware) -------------------------------
GRAPH_LOOKUP_FAILED = "GRAPH_LOOKUP_FAILED"
GRAPH_RECORDING_NOT_FOUND = "GRAPH_RECORDING_NOT_FOUND"
GRAPH_RECORDING_AMBIGUOUS = "GRAPH_RECORDING_AMBIGUOUS"
GRAPH_RECORDING_FOUND = "GRAPH_RECORDING_FOUND"

# -- DriveItem discovery and matching ------------------------------------------
DRIVE_ITEM_DISCOVERY_FAILED = "DRIVE_ITEM_DISCOVERY_FAILED"
RECORDING_FILE_NOT_FOUND = "RECORDING_FILE_NOT_FOUND"
TIMESTAMP_MISMATCH = "TIMESTAMP_MISMATCH"
SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
AMBIGUOUS_RECORDING_FILES = "AMBIGUOUS_RECORDING_FILES"
EXACT_RECORDING_FILE_MATCHED = "EXACT_RECORDING_FILE_MATCHED"

# -- the write -------------------------------------------------------------------
# The two statuses persisted to qa_doctors_sessions.recording_link_status on a
# successful write. They are the values the n8n branch has always written, so
# every existing consumer keeps reading the column the same way.
LINK_ORGANIZATION_VIEW = "organization_view_link_created_exact_match"
LINK_DRIVE_ITEM_WEB_URL = "drive_item_web_url_used_exact_match"
WRITTEN = "WRITTEN"
WRITE_REFUSED_ALREADY_LINKED = "WRITE_REFUSED_ALREADY_LINKED"
LINK_URL_UNAVAILABLE = "LINK_URL_UNAVAILABLE"

# -- how each status settles the stage ---------------------------------------------
STATE_COMPLETE = "COMPLETE"
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"
STATE_WAITING = "WAITING"          # retry later, bounded
STATE_REVIEW = "REVIEW_REQUIRED"   # terminal until a human looks
STATE_READY = "READY"              # an exact match that may be written

# Transient: the answer can change without a human (Graph access restored, the
# recording finishes uploading, the file is indexed). Retried with backoff and
# a finite budget - never every cycle, never forever.
#
# SUBJECT_MISMATCH is here, not under review: at KBC several cohorts start at
# 08:00, so "a file near our time with another subject" is usually a parallel
# lecture's recording while ours is not yet discoverable. The budget still
# ends in review if it never appears.
RETRYABLE_STATUSES = frozenset({
    GRAPH_LOOKUP_FAILED, GRAPH_RECORDING_NOT_FOUND, DRIVE_ITEM_DISCOVERY_FAILED,
    RECORDING_FILE_NOT_FOUND, SUBJECT_MISMATCH, LINK_URL_UNAVAILABLE,
})
# Terminal until an operator acts: retrying cannot change the answer and a
# guess would be worse than no link.
REVIEW_STATUSES = frozenset({
    GRAPH_RECORDING_AMBIGUOUS, AMBIGUOUS_RECORDING_FILES, TIMESTAMP_MISMATCH,
    ORGANIZER_LOOKUP_ID_MISSING, SESSION_CALL_ID_MISSING, LECTURE_IDENTITY_MISMATCH,
})

# Backoff for RETRYABLE_STATUSES: 6 h, 12 h, 24 h, then daily, 8 attempts.
RETRY_BASE_HOURS = 6
RETRY_MAX_HOURS = 24
MAX_ATTEMPTS = 8
ATTEMPTS_EXHAUSTED = "RECORDING_LINK_ATTEMPTS_EXHAUSTED"


def stage_state_for(status: str) -> str:
    if status in (WRITTEN, RECORDING_ALREADY_LINKED, WRITE_REFUSED_ALREADY_LINKED):
        return STATE_COMPLETE
    if status in (NO_RECORDING_EXPECTED_CANCELLED, NO_LEGACY_QA_ROW):
        return STATE_NOT_APPLICABLE
    if status == EXACT_RECORDING_FILE_MATCHED:
        return STATE_READY
    if status in REVIEW_STATUSES:
        return STATE_REVIEW
    return STATE_WAITING


@dataclass(frozen=True)
class RecordingTarget:
    """Everything the stage needs to know about ONE lecture, read from the DB."""
    lecture_id: str
    session_date: str                 # YYYY-MM-DD, the lecture's business date
    legacy_session_id: str | None     # qa_doctors_sessions.session_id
    legacy_meeting_id: str | None
    lecture_meeting_id: str | None
    subject: str | None               # the legacy row's subject (what n8n matched)
    meeting_lookup_user_id: str | None
    meeting_organizer_user_id: str | None
    existing_recording_url: str | None
    cancelled: bool
    cancelled_reason: str | None
    lecture_key: str | None           # qa_perfect_lectures.lecture_key, if any
    perfect_recording_url_empty: bool | None
    thread_id: str | None = None      # the Teams thread the transcript names

    @property
    def meeting_id(self) -> str | None:
        return self.legacy_meeting_id or self.lecture_meeting_id


@dataclass(frozen=True)
class GraphRecordingLookup:
    status: str
    call_id: str | None
    http_status: int | None = None
    error_code: str | None = None
    diagnostic_reason: str | None = None
    recordings_returned: int | None = None
    call_id_matches: int = 0
    recording_id: str | None = None
    created_at: str | None = None     # ISO 8601, as Graph returned it
    content_correlation_id: str | None = None
    evidence: str = "LIVE"            # "OFFLINE" = captured evidence, never write-authorizing


@dataclass(frozen=True)
class DriveItemCandidate:
    item_id: str
    drive_id: str
    name: str
    web_url: str | None
    source: str
    subject_key: str | None = None    # normalized subject parsed from the name
    file_date: str | None = None      # YYYY-MM-DD parsed from the name (UTC)
    file_timestamp: float | None = None  # epoch seconds parsed from the name


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple = ()
    sources_attempted: tuple = ()
    sources_failed: tuple = ()        # ({"source", "http_status", "error_code"}, ...)
    # "LIVE" = read from Graph now. "OFFLINE" = a read-only preview substituted
    # previously captured evidence; such a result can inform a preview but can
    # never authorize a write.
    evidence: str = "LIVE"
    live_failures: tuple = ()
    source_counts: tuple = ()         # ((source, mp4 items returned), ...)
    live_source_counts: tuple = ()

    @property
    def failed(self) -> bool:
        """Every source failed: an empty result would mean nothing."""
        return bool(self.sources_attempted) and len(self.sources_failed) == len(
            self.sources_attempted)


@dataclass(frozen=True)
class MatchResult:
    status: str
    candidate_file_count: int = 0
    exact_candidate_count: int = 0
    candidate: DriveItemCandidate | None = None
    timestamp_difference_seconds: float | None = None
    nearest_same_subject_lead_seconds: float | None = None
    ambiguous_filenames: tuple = ()


@dataclass
class RecordingLinkDecision:
    """One lecture's evaluation: the preview row, and the write's input."""
    lecture_id: str
    session_date: str
    subject: str | None
    status: str
    reason: str
    legacy_session_id: str | None = None
    meeting_id_present: bool = False
    organizer_lookup_id_present: bool = False
    call_id: str | None = None
    graph: GraphRecordingLookup | None = None
    match: MatchResult | None = None
    discovery_sources_failed: tuple = ()
    would_write: bool = False
    would_update_perfect: bool = False
    verification: str = "NOT_YET_VERIFIED"
    extra: dict = field(default_factory=dict)

    @property
    def stage_state(self) -> str:
        return stage_state_for(self.status)

    def preview_row(self) -> dict:
        graph, match = self.graph, self.match
        return {
            "lecture_id": self.lecture_id,
            "date": self.session_date,
            "subject": self.subject,
            "meeting_id_present": self.meeting_id_present,
            "organizer_lookup_id_present": self.organizer_lookup_id_present,
            "call_id_resolved": bool(self.call_id),
            "graph_lookup_status": graph.status if graph else None,
            "graph_http_status": graph.http_status if graph else None,
            "graph_recording_count": graph.recordings_returned if graph else None,
            "candidate_file_count": match.candidate_file_count if match else None,
            "exact_candidate_count": match.exact_candidate_count if match else None,
            "timestamp_difference_seconds": (match.timestamp_difference_seconds
                                             if match else None),
            "nearest_same_subject_lead_seconds": (
                match.nearest_same_subject_lead_seconds if match else None),
            "graph_created_at": graph.created_at if graph else None,
            "recording_match_status": self.status,
            "stage_state": self.stage_state,
            "would_write": self.would_write,
            "would_update_perfect": self.would_update_perfect,
            "reason": self.reason,
            "verification": self.verification,
        }

    def attempt_detail(self) -> dict:
        """Persistable diagnostics. No URL, token, transcript text or learner data."""
        graph, match = self.graph, self.match
        return {
            "graph": ({k: v for k, v in asdict(graph).items()} if graph else None),
            "candidate_file_count": match.candidate_file_count if match else None,
            "exact_candidate_count": match.exact_candidate_count if match else None,
            "timestamp_difference_seconds": (match.timestamp_difference_seconds
                                             if match else None),
            "nearest_same_subject_lead_seconds": (
                match.nearest_same_subject_lead_seconds if match else None),
            "ambiguous_filenames": list(match.ambiguous_filenames) if match else [],
            "discovery_sources_failed": list(self.discovery_sources_failed),
        }
