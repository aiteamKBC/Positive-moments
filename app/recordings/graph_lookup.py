"""
Organizer-aware Graph recording metadata lookup.

    GET /users/{meeting_lookup_user_id}/onlineMeetings/{meeting_id}/recordings

The user in the path is the lecture's persisted `meeting_lookup_user_id` - the
same mailbox context the coded platform already uses for transcripts and for
Phase 6A recording measurement. Never `/me`: an app-only token has no `/me`,
and a delegated `/me` only sees meetings that user organized, which is why the
n8n branch failed on 43 of 50 lectures.

Needs, per Microsoft's "List recordings": application permission
OnlineMeetingRecording.Read.All (admin consented) plus a Teams application
access policy granted to the user in the path.

Classification is exhaustive and never conflates failure with absence:
  * any HTTP / auth / network failure   -> GRAPH_LOOKUP_FAILED
  * 200, no recording with the call id  -> GRAPH_RECORDING_NOT_FOUND
  * 200, several with the call id       -> GRAPH_RECORDING_AMBIGUOUS
  * 200, exactly one, valid timestamp   -> GRAPH_RECORDING_FOUND

Several recordings for one call (a false start and the real delivery, or an
interrupted lecture) are NOT a dead end: GRAPH_RECORDING_AMBIGUOUS carries
every one of them - createdDateTime, endDateTime, contentCorrelationId - so the
resolution policy can decide from file and duration evidence, and fails closed
when it cannot. Byte-identical duplicates (same id, or same created AND end
time) are collapsed first: two entries for one moment are one recording.
"""
from __future__ import annotations

from urllib.parse import quote

from app.common.time import parse_graph_datetime
from app.graph.transport import GraphError
from app.recordings.models import (
    GRAPH_LOOKUP_FAILED,
    GRAPH_RECORDING_AMBIGUOUS,
    GRAPH_RECORDING_FOUND,
    GRAPH_RECORDING_NOT_FOUND,
    GraphRecording,
    GraphRecordingLookup,
)


def recordings_path(meeting_lookup_user_id: str, meeting_id: str) -> str:
    if not str(meeting_lookup_user_id or "").strip() or not str(meeting_id or "").strip():
        raise ValueError("an organizer lookup id and a meeting id are both required")
    return (f"/users/{quote(meeting_lookup_user_id.strip(), safe='')}"
            f"/onlineMeetings/{quote(meeting_id.strip(), safe='')}/recordings")


def _valid_time(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parse_graph_datetime(value)
    except (TypeError, ValueError):
        return None
    return value


def distinct_recordings(rows) -> tuple:
    """Graph recordings for one call, identical duplicates collapsed, in time order."""
    seen, found = set(), []
    for row in rows:
        created = row.get("createdDateTime")
        end = _valid_time(row.get("endDateTime"))
        moment = (parse_graph_datetime(created).timestamp(),
                  parse_graph_datetime(end).timestamp() if end else None)
        keys = {("id", row.get("id"))} if row.get("id") else set()
        keys.add(("moment", moment))
        if keys & seen:
            continue
        seen |= keys
        found.append(GraphRecording(recording_id=row.get("id"), created_at=created,
                                    end_at=end,
                                    content_correlation_id=row.get("contentCorrelationId")))
    return tuple(sorted(found, key=lambda r: parse_graph_datetime(r.created_at)))


def _diagnostic(error: GraphError) -> str | None:
    message = str(error.message or "").casefold()
    if "application access policy" in message:
        return "APPLICATION_ACCESS_POLICY_DENIED"
    if error.status == 401:
        return "GRAPH_AUTHENTICATION_FAILED"
    if error.status == 403:
        return "GRAPH_ACCESS_FORBIDDEN"
    if error.status == 404:
        return "GRAPH_MEETING_NOT_VISIBLE_TO_LOOKUP_USER"
    if error.status is None:
        return "GRAPH_NETWORK_FAILURE"
    return None


class RecordingMetadataGateway:
    """One Graph listing per lecture, through the platform's app-only client."""

    def __init__(self, graph):
        self.graph = graph
        self.calls = 0

    def lookup(self, *, meeting_lookup_user_id: str, meeting_id: str,
               call_id: str) -> GraphRecordingLookup:
        expected = str(call_id or "").lower()
        path = recordings_path(meeting_lookup_user_id, meeting_id)
        self.calls += 1
        try:
            # get_collection follows @odata.nextLink, so "no second recording
            # for this call" is judged on the whole collection, never a page.
            recordings = self.graph.get_collection(path)
        except GraphError as error:
            return GraphRecordingLookup(
                status=GRAPH_LOOKUP_FAILED, call_id=expected or None,
                http_status=error.status, error_code=str(error.code or "")[:120],
                diagnostic_reason=_diagnostic(error))
        matches = [row for row in recordings
                   if str(row.get("callId") or "").lower() == expected and expected]
        base = {"call_id": expected or None, "recordings_returned": len(recordings),
                "call_id_matches": len(matches)}
        if not matches:
            return GraphRecordingLookup(status=GRAPH_RECORDING_NOT_FOUND, **base)
        if any(_valid_time(row.get("createdDateTime")) is None for row in matches):
            return GraphRecordingLookup(status=GRAPH_LOOKUP_FAILED,
                                        error_code="invalid_created_date_time",
                                        diagnostic_reason="GRAPH_RESPONSE_INVALID",
                                        **base)
        recordings = distinct_recordings(matches)
        if len(recordings) > 1:
            return GraphRecordingLookup(status=GRAPH_RECORDING_AMBIGUOUS,
                                        recordings=recordings, **base)
        [recording] = recordings
        return GraphRecordingLookup(
            status=GRAPH_RECORDING_FOUND, recording_id=recording.recording_id,
            created_at=recording.created_at, end_at=recording.end_at,
            content_correlation_id=recording.content_correlation_id,
            recordings=recordings, **base)
