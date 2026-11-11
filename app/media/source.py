"""
Which recording file a Positive Moment clip is cut from.

This is NOT a second recording matcher. The shared Recording Link stage
(app/recordings, v2 full-recording resolution) decides the file; media only
reads its answer:

  * the pipeline resolver's RECORDING_LINK stage must be COMPLETE;
  * the legacy row must carry an exact-match link status and the durable
    Graph identity (recording_drive_id, recording_item_id) - written by the
    coded stage or by the n8n v9 branch, both under the same identity rule;
  * no URL is ever parsed to rediscover a file.

Anything else - missing, waiting, review, ambiguous, multipart, blocked behind
an earlier stage - is WAITING_FOR_RECORDING, carrying the Recording Link
stage's own status and reason so the operator sees the real cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.media.alignment import RecordingRef
from app.recordings.models import (
    LINK_DRIVE_ITEM_WEB_URL,
    LINK_ORGANIZATION_VIEW,
    RESOLUTION_POLICY_VERSION,
)

READY = "READY"
WAITING_FOR_RECORDING = "WAITING_FOR_RECORDING"
NOT_APPLICABLE = "NOT_APPLICABLE"

EXACT_LINK_STATUSES = (LINK_ORGANIZATION_VIEW, LINK_DRIVE_ITEM_WEB_URL)


@dataclass(frozen=True)
class RecordingSource:
    state: str
    status: str | None
    reason: str | None
    drive_id: str | None = None
    item_id: str | None = None
    file_name: str | None = None
    duration_seconds: float | None = None
    rule: str | None = None
    recordings: tuple = ()          # Graph callRecordings for the call, if known
    checked_at: object = None
    detail: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == READY


def _recordings_from_metadata(metadata: dict) -> tuple:
    graph = (metadata or {}).get("graph") or {}
    rows = graph.get("recordings") or []
    found = []
    for row in rows:
        if isinstance(row, dict) and row.get("created_at"):
            found.append(RecordingRef(row.get("recording_id"), row["created_at"],
                                      row.get("end_at"), row.get("content_correlation_id")))
    if not found and graph.get("created_at"):
        found.append(RecordingRef(graph.get("recording_id"), graph["created_at"],
                                  graph.get("end_at"), graph.get("content_correlation_id")))
    return tuple(found)


def _file_duration(metadata: dict, item_id: str) -> float | None:
    resolution = (metadata or {}).get("resolution") or {}
    selected = resolution.get("selected") or {}
    if selected.get("duration_source") == "video_facet" and selected.get("duration_seconds"):
        return float(selected["duration_seconds"])
    return None


def resolve_recording(*, state: dict, legacy: dict | None, coded_link: dict | None,
                      is_cancelled: bool = False) -> RecordingSource:
    stage = (state.get("stages") or {}).get("RECORDING_LINK") or {}
    checked = (coded_link or {}).get("last_attempted_at")
    last_status = (coded_link or {}).get("status")
    if is_cancelled or stage.get("reason") == "NO_RECORDING_EXPECTED_CANCELLED":
        return RecordingSource(NOT_APPLICABLE, "NO_RECORDING_EXPECTED_CANCELLED",
                               "the lecture was cancelled", checked_at=checked)
    if stage.get("state") != "COMPLETE":
        executable = state.get("executable_stage")
        if (stage.get("action") == "LINK_RECORDING" and executable
                and executable != "RECORDING_LINK"):
            status, reason = "BLOCKED_BY_EARLIER_STAGE", f"waiting for {executable}"
        else:
            status = last_status or stage.get("reason") or stage.get("state") or "MISSING"
            reason = (coded_link or {}).get("reason") or stage.get("reason") or stage.get("action")
        return RecordingSource(WAITING_FOR_RECORDING, status, reason, checked_at=checked,
                               detail={"stage_state": stage.get("state"),
                                       "stage_action": stage.get("action")})
    if not legacy or not str(legacy.get("recording_url") or "").strip():
        return RecordingSource(WAITING_FOR_RECORDING, "RECORDING_URL_MISSING",
                               "the legacy row carries no recording", checked_at=checked)
    drive, item = legacy.get("recording_drive_id"), legacy.get("recording_item_id")
    if not drive or not item or legacy.get("recording_link_status") not in EXACT_LINK_STATUSES:
        # Linked, but not under the exact-identity rule (or before ids were
        # recorded). Media will not rediscover the file from its URL.
        return RecordingSource(WAITING_FOR_RECORDING, "RECORDING_IDENTITY_UNAVAILABLE",
                               "the recording link has no durable Graph identity",
                               checked_at=checked)
    metadata = (coded_link or {}).get("metadata") or {}
    if isinstance(metadata, str):
        import json
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    same_file = (coded_link or {}).get("recording_item_id") == item
    resolution = (metadata.get("resolution") or {}) if same_file else {}
    return RecordingSource(
        READY, last_status or "RECORDING_LINKED",
        (coded_link or {}).get("reason") if same_file else "linked (legacy branch)",
        drive_id=drive, item_id=item, file_name=legacy.get("recording_filename"),
        duration_seconds=_file_duration(metadata, item) if same_file else None,
        rule=resolution.get("rule"),
        recordings=_recordings_from_metadata(metadata) if same_file else (),
        checked_at=checked or legacy.get("recording_link_updated_at"),
        detail={"resolution_policy": metadata.get("resolution_policy") if same_file else None,
                "current_resolution_policy": RESOLUTION_POLICY_VERSION,
                "linked_by": "CODED_RECORDING_LINK" if same_file else "LEGACY_OR_PRIOR"})
