"""
Read-only RECORDING_LINK preview for a date range.

Answers, per lecture, what the coded stage WOULD do - without writing to any
table, without persisting stage state, without creating a sharing link. Graph
is called only when the operator asks for `live_graph`, and then only with
GETs (and the read-only search POST when a region is configured).

Two populations are reported, because they are different questions:
  * coded lectures (lecture_sessions) - evaluated through the same resolver
    and the same RecordingLinkService the scheduler and backfill use;
  * legacy QA rows in range that no coded lecture owns - the coded stage
    cannot act on them until discovery/backfill registers the lecture, and the
    report says so instead of silently dropping them.

Evidence labels: LIVE_VERIFIED (decided from the live DB and live Graph),
OFFLINE_VERIFIED (decided from previously captured, read-only evidence -
never write-authorizing), NOT_YET_VERIFIED (the evidence needed is not
available yet, e.g. Graph refused or DriveItem discovery failed).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from urllib.parse import unquote

from app.graph.transport import GraphError
from app.orchestration.stages import RECORDING_LINK
from app.recordings.drive_items import DriveItemDiscovery
from app.recordings.graph_lookup import RecordingMetadataGateway
from app.recordings.models import (
    DRIVE_ITEM_DISCOVERY_FAILED,
    EXACT_RECORDING_FILE_MATCHED,
    GRAPH_LOOKUP_FAILED,
    DiscoveryResult,
    RecordingLinkDecision,
)
from app.recordings.service import RecordingLinkService

LIVE_VERIFIED = "LIVE_VERIFIED"
OFFLINE_VERIFIED = "OFFLINE_VERIFIED"
NOT_YET_VERIFIED = "NOT_YET_VERIFIED"
NOT_EVALUATED = "NOT_EVALUATED_GRAPH_DISABLED"

LEGACY_ROWS_IN_RANGE = """
SELECT q.session_id, q.date, q.subject, q.meeting_id,
       NULLIF(BTRIM(q.recording_url), '') IS NOT NULL, q.cancelled_session
  FROM public.qa_doctors_sessions q
 WHERE q.date BETWEEN %s AND %s
 ORDER BY q.date, q.subject, q.session_id
"""
LECTURES_IN_RANGE = """
SELECT l.lecture_id
  FROM public.lecture_sessions l
 WHERE l.session_date BETWEEN %s AND %s
   AND l.metadata -> 'duplicate_suppression' IS NULL
 ORDER BY l.session_date, l.scheduled_start, l.lecture_id
"""


# -- offline evidence ------------------------------------------------------------

class _RecordedRecordings:
    """A Graph stand-in answering recordings listings from captured responses."""

    def __init__(self, by_meeting: dict):
        self.by_meeting = by_meeting

    def get_collection(self, path: str):
        meeting = unquote(path.split("/onlineMeetings/", 1)[1].split("/recordings", 1)[0])
        recorded = self.by_meeting.get(meeting)
        if recorded is None or recorded.get("error_status") is not None:
            status = None if recorded is None else recorded["error_status"]
            raise GraphError("Microsoft Graph (offline evidence)", status,
                             "offline_evidence_unavailable",
                             "no successful captured response for this meeting")
        return list(recorded.get("value") or [])


class OfflineMetadataGateway(RecordingMetadataGateway):
    """Answers from captured responses. Its lookups are labelled OFFLINE."""

    def __init__(self, by_meeting: dict):
        super().__init__(_RecordedRecordings(by_meeting))

    def lookup(self, **kwargs):
        result = super().lookup(**kwargs)
        self.calls = 0                      # no Graph call was made
        return replace(result, evidence="OFFLINE")


class LiveThenOfflineMetadata:
    """Live organizer lookup; if Graph refuses, a captured success may answer."""

    def __init__(self, live: RecordingMetadataGateway | None,
                 offline: OfflineMetadataGateway | None):
        self.live, self.offline = live, offline

    @property
    def calls(self) -> int:
        return self.live.calls if self.live else 0

    def lookup(self, **kwargs):
        result = self.live.lookup(**kwargs) if self.live else None
        if (result is None or result.status == GRAPH_LOOKUP_FAILED) and self.offline:
            fallback = self.offline.lookup(**kwargs)
            if fallback.status != GRAPH_LOOKUP_FAILED or result is None:
                return fallback
        return result


class _FixtureSource:
    name = "offline_evidence"

    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def applicable(self, target) -> bool:
        return True

    def items(self, target):
        return self._items


class LiveThenOfflineDiscovery:
    """Live discovery; if it fails and captured files exist, answer from those."""

    def __init__(self, live: DriveItemDiscovery | None, offline_items=None):
        self.live = live
        self.offline = (DriveItemDiscovery([_FixtureSource(offline_items)])
                        if offline_items is not None else None)

    @property
    def calls(self) -> int:
        return self.live.calls if self.live else 0

    def discover(self, target) -> DiscoveryResult:
        result = self.live.discover(target) if self.live else DiscoveryResult()
        # An empty live answer is only as complete as the sources that ran; with
        # no search region the tenant search never runs. A preview may then show
        # what captured evidence says - labelled OFFLINE, never write-authorizing.
        unusable = (not result.sources_attempted or result.sources_failed
                    or not result.candidates)
        if unusable and self.offline is not None:
            offline = self.offline.discover(target)
            return DiscoveryResult(candidates=offline.candidates,
                                   sources_attempted=offline.sources_attempted,
                                   sources_failed=(), evidence="OFFLINE",
                                   live_failures=result.sources_failed,
                                   source_counts=offline.source_counts,
                                   live_source_counts=result.source_counts)
        return result


# -- the preview -----------------------------------------------------------------

def verification_of(decision: RecordingLinkDecision) -> str:
    graph, status = decision.graph, decision.status
    if graph is None:
        return LIVE_VERIFIED                      # decided from the live DB alone
    offline = (graph.evidence == "OFFLINE"
               or decision.extra.get("discovery_evidence") == "OFFLINE")
    if status in (GRAPH_LOOKUP_FAILED, DRIVE_ITEM_DISCOVERY_FAILED):
        return NOT_YET_VERIFIED
    return OFFLINE_VERIFIED if offline else LIVE_VERIFIED


class RecordingLinkPreview:

    def __init__(self, *, resolver, repository, metadata_gateway, discovery,
                 metadata_evidence: str):
        self.resolver = resolver
        self.repository = repository
        self.metadata_evidence = metadata_evidence
        self.service = RecordingLinkService(repository=repository,
                                            metadata_gateway=metadata_gateway,
                                            discovery=discovery, publisher=None)

    def run(self, connection, date_from, date_to) -> dict:
        legacy = connection.execute(LEGACY_ROWS_IN_RANGE, (date_from, date_to)).fetchall()
        lecture_ids = [str(row[0]) for row in
                       connection.execute(LECTURES_IN_RANGE, (date_from, date_to)).fetchall()]
        rows, owned = [], set()
        for lecture_id in lecture_ids:
            state = self.resolver.for_lecture(connection, lecture_id)
            if state.get("is_suppressed_duplicate"):
                continue
            stage = state["stages"][RECORDING_LINK]
            session_id = stage.get("legacy_session_id")
            target = self.repository.target(connection, lecture_id, session_id)
            decision = self.service.decide(target)
            decision.verification = verification_of(decision)
            if session_id:
                owned.add(session_id)
            row = decision.preview_row()
            row["population"] = "CODED_LECTURE"
            row["resolver_stage_state"] = stage["state"]
            row["discovery_evidence"] = decision.extra.get("discovery_evidence")
            row["live_discovery_failures"] = decision.extra.get("live_discovery_failures")
            row["legacy_session_ref"] = (hashlib.sha256(session_id.encode()).hexdigest()[:10]
                                         if session_id else None)
            row["live_source_counts"] = decision.extra.get("live_source_counts")
            # What the limited Perfect update WOULD touch if this match were live.
            row["perfect_row_would_update_if_live"] = bool(
                decision.status == EXACT_RECORDING_FILE_MATCHED and target.lecture_key
                and target.perfect_recording_url_empty)
            rows.append(row)
        for session_id, day, subject, meeting_id, has_url, cancelled in legacy:
            if session_id in owned:
                continue
            rows.append({
                "lecture_id": None, "date": day.isoformat(), "subject": subject,
                "meeting_id_present": bool(meeting_id),
                "organizer_lookup_id_present": None, "call_id_resolved": None,
                "graph_lookup_status": None, "graph_http_status": None,
                "graph_recording_count": None, "candidate_file_count": None,
                "exact_candidate_count": None, "timestamp_difference_seconds": None,
                "recording_match_status": ("RECORDING_ALREADY_LINKED" if has_url
                                           else "NO_CODED_LECTURE"),
                "stage_state": None, "would_write": False,
                "would_update_perfect": False,
                "reason": ("recording_url present" if has_url else
                           "legacy row has no coded lecture; register it through "
                           "discovery/backfill before the coded stage can act"),
                "verification": LIVE_VERIFIED, "population": "LEGACY_ROW_ONLY",
                "legacy_cancelled": str(cancelled or "").lower() == "true",
            })
        return self.summarize(rows, legacy, date_from, date_to)

    def summarize(self, rows, legacy, date_from, date_to) -> dict:
        coded = [r for r in rows if r["population"] == "CODED_LECTURE"]
        with_url = sum(1 for row in legacy if row[4])
        return {
            "date_from": str(date_from), "date_to": str(date_to),
            "read_only": True, "database_writes": 0, "sharing_links_created": 0,
            "graph_calls": self.service.graph_calls,
            "metadata_evidence": self.metadata_evidence,
            "legacy_rows": {"total": len(legacy), "with_recording": with_url,
                            "without_recording": len(legacy) - with_url},
            "coded_lectures": len(coded),
            "by_status": dict(sorted(Counter(r["recording_match_status"]
                                             for r in rows).items())),
            "by_verification": dict(sorted(Counter(
                r["verification"] for r in rows
                if r["recording_match_status"] != "RECORDING_ALREADY_LINKED").items())),
            "would_write_sessions": sum(1 for r in rows if r["would_write"]),
            "would_update_perfect": sum(1 for r in rows if r["would_update_perfect"]),
            "perfect_rows_updatable_if_live": sum(
                1 for r in rows if r.get("perfect_row_would_update_if_live")),
            "offline_exact_matches": sum(
                1 for r in rows if r["recording_match_status"] == EXACT_RECORDING_FILE_MATCHED
                and r["verification"] == OFFLINE_VERIFIED),
            "live_exact_matches": sum(
                1 for r in rows if r["recording_match_status"] == EXACT_RECORDING_FILE_MATCHED
                and r["verification"] == LIVE_VERIFIED),
            "rows": rows,
        }
