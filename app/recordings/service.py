"""
The RECORDING_LINK decision, and the write that may follow it.

    target -> preconditions -> organizer Graph lookup -> DriveItem discovery
           -> candidate matching -> (several exact: media evidence for those
              files only + full-recording resolution) -> decision
           -> (armed only) publish + write

`evaluate` never writes anything anywhere. `link` evaluates, and only for an
EXACT_RECORDING_FILE_MATCHED decision publishes a URL and runs the guarded
legacy write. Both record the outcome in `lecture_recording_links` when asked
to persist, which is what makes the stage idempotent and bounded.

Cancelled / non-delivered: a lecture whose registry row is cancelled, or whose
legacy QA row says `cancelled_session = 'true'` (the rendered answer for a
non-delivered lecture - app/rendering renders NON_DELIVERED that way), expects
no recording. That is NOT_APPLICABLE, not a missing recording.
"""
from __future__ import annotations

from app.graph.transport import GraphError
from app.recordings.graph_lookup import RecordingMetadataGateway
from app.recordings.matching import match_candidates
from app.recordings.models import (
    DRIVE_ITEM_DISCOVERY_FAILED,
    EXACT_RECORDING_FILE_MATCHED,
    LECTURE_IDENTITY_MISMATCH,
    LINK_DRIVE_ITEM_WEB_URL,
    LINK_ORGANIZATION_VIEW,
    LINK_URL_UNAVAILABLE,
    NO_LEGACY_QA_ROW,
    NO_RECORDING_EXPECTED_CANCELLED,
    ORGANIZER_LOOKUP_ID_MISSING,
    RECORDING_ALREADY_LINKED,
    SESSION_CALL_ID_MISSING,
    WRITE_REFUSED_ALREADY_LINKED,
    WRITTEN,
    RecordingLinkDecision,
)
from app.recordings.resolution import DEFAULT_POLICY, MAX_EVIDENCE_CANDIDATES, RULE_SINGLE
from app.transcripts.identity import teams_call_id


class RecordingLinkService:

    def __init__(self, *, repository, metadata_gateway: RecordingMetadataGateway,
                 discovery, publisher=None, evidence_reader=None, policy=None):
        self.repository = repository
        self.metadata = metadata_gateway
        self.discovery = discovery
        self.publisher = publisher
        # Reads size / video duration for the final exact candidates only
        # (drive_items.CandidateMetadataReader). None = use what discovery had.
        self.evidence_reader = evidence_reader
        self.policy = policy or DEFAULT_POLICY

    @property
    def graph_calls(self) -> int:
        return (self.metadata.calls + self.discovery.calls
                + (self.publisher.calls if self.publisher else 0)
                + (self.evidence_reader.calls if self.evidence_reader else 0))

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, connection, lecture_id, legacy_session_id) -> RecordingLinkDecision:
        target = self.repository.target(connection, lecture_id, legacy_session_id)
        if target is None:
            raise LookupError(f"unknown lecture {lecture_id}")
        return self.decide(target)

    def decide(self, target) -> RecordingLinkDecision:
        decision = RecordingLinkDecision(
            lecture_id=target.lecture_id, session_date=target.session_date,
            subject=target.subject, status="", reason="",
            legacy_session_id=target.legacy_session_id,
            meeting_id_present=bool(target.meeting_id),
            organizer_lookup_id_present=bool(
                str(target.meeting_lookup_user_id or "").strip()))

        def done(status, reason):
            decision.status, decision.reason = status, reason
            return decision

        if not target.legacy_session_id:
            return done(NO_LEGACY_QA_ROW, "no legacy QA row to carry a recording link")
        if str(target.existing_recording_url or "").strip():
            return done(RECORDING_ALREADY_LINKED, "recording_url is already set; never overwritten")
        if target.cancelled:
            return done(NO_RECORDING_EXPECTED_CANCELLED, target.cancelled_reason or "cancelled")
        if not target.meeting_id:
            return done(LECTURE_IDENTITY_MISMATCH, "MEETING_ID_MISSING")
        if (target.legacy_meeting_id and target.lecture_meeting_id
                and target.legacy_meeting_id != target.lecture_meeting_id):
            return done(LECTURE_IDENTITY_MISMATCH,
                        "legacy row meeting_id differs from the lecture's meeting_id")
        if not decision.organizer_lookup_id_present:
            return done(ORGANIZER_LOOKUP_ID_MISSING,
                        "lecture_sessions.meeting_lookup_user_id is empty")
        decision.call_id = teams_call_id(target.legacy_session_id)
        if not decision.call_id:
            return done(SESSION_CALL_ID_MISSING,
                        "the session id does not decode to a Teams call id")

        graph = self.metadata.lookup(
            meeting_lookup_user_id=target.meeting_lookup_user_id,
            meeting_id=target.meeting_id, call_id=decision.call_id)
        decision.graph = graph
        # Several recordings for the call are carried into resolution, which
        # fails closed (GRAPH_RECORDING_AMBIGUOUS) when files cannot decide it.
        if not graph.resolvable:
            detail = (f"HTTP {graph.http_status} {graph.error_code} "
                      f"{graph.diagnostic_reason or ''}".strip()
                      if graph.http_status or graph.error_code else
                      f"{graph.recordings_returned} recordings, "
                      f"{graph.call_id_matches} with the call id")
            return done(graph.status, detail)

        discovered = self.discovery.discover(target)
        decision.discovery_sources_failed = discovered.sources_failed
        decision.extra["discovery_evidence"] = discovered.evidence
        if discovered.live_failures:
            decision.extra["live_discovery_failures"] = list(discovered.live_failures)
        decision.extra["live_source_counts"] = dict(
            discovered.live_source_counts if discovered.evidence == "OFFLINE"
            else discovered.source_counts)
        decision.extra["raw_candidate_count"] = discovered.raw_candidate_count
        if not discovered.sources_attempted:
            return done(DRIVE_ITEM_DISCOVERY_FAILED, "NO_APPLICABLE_DISCOVERY_SOURCE")
        candidates = discovered.candidates
        match = self._match(target, graph, candidates)
        needs = [c for c in match.exact_candidates if c.needs_media_evidence]
        several = match.exact_candidate_count > 1 or len(graph.logical_recordings) > 1
        if (several and needs and self.evidence_reader is not None
                and not discovered.sources_failed
                and match.exact_candidate_count <= MAX_EVIDENCE_CANDIDATES):
            try:
                fresh = {c.physical_id: self.evidence_reader.enrich(c) for c in needs}
            except GraphError as error:
                decision.match = match
                return done(DRIVE_ITEM_DISCOVERY_FAILED,
                            f"candidate_metadata:{error.status}:{str(error.code or '')[:120]} "
                            f"(partial result: {match.status})")
            candidates = tuple(fresh.get(c.physical_id, c) for c in candidates)
            match = self._match(target, graph, candidates)
        decision.match = match
        if discovered.sources_failed:
            # A source that could not be read may hold a second qualifying file,
            # so "exactly one" is unproven. Retry rather than guess.
            failures = ", ".join(f"{f['source']}:{f['http_status']}:{f['error_code']}"
                                 for f in discovered.sources_failed)
            return done(DRIVE_ITEM_DISCOVERY_FAILED,
                        f"{failures} (partial result: {match.status})")
        rule = (match.resolution or {}).get("rule")
        if match.status != EXACT_RECORDING_FILE_MATCHED:
            return done(match.status, f"{match.exact_candidate_count} exact of "
                                      f"{match.candidate_file_count} candidates"
                                      + (f"; {rule}" if rule else ""))
        # Only live evidence may authorize a write.
        decision.would_write = discovered.evidence == "LIVE" and graph.evidence == "LIVE"
        decision.would_update_perfect = decision.would_write and bool(
            target.lecture_key and target.perfect_recording_url_empty)
        return done(EXACT_RECORDING_FILE_MATCHED,
                    f"lead {match.timestamp_difference_seconds}s via {match.candidate.source}"
                    + (f"; {rule}" if rule and rule != RULE_SINGLE else ""))

    def _match(self, target, graph, candidates):
        return match_candidates(subject=target.subject, session_date=target.session_date,
                                graph=graph, candidates=candidates,
                                expected_duration_seconds=target.expected_duration_seconds,
                                policy=self.policy)

    # -- the stage action --------------------------------------------------------

    def link(self, connection, lecture_id, legacy_session_id, *, write: bool,
             persist: bool = True) -> dict:
        """
        Evaluate one lecture and, only when `write` and the match is exact,
        publish a URL and perform the guarded write. Returns an outcome summary.
        """
        target = self.repository.target(connection, lecture_id, legacy_session_id)
        if target is None:
            raise LookupError(f"unknown lecture {lecture_id}")
        decision = self.decide(target)
        outcome = {"status": decision.status, "reason": decision.reason,
                   "legacy_sessions_written": 0, "perfect_rows_written": 0,
                   "written": False, "preview": decision.preview_row()}

        if decision.status == EXACT_RECORDING_FILE_MATCHED and write and decision.would_write:
            if self.publisher is None:
                raise RuntimeError("an armed recording link write needs a publisher")
            url, kind, create_failure = self.publisher.publish(decision.match.candidate)
            if not url:
                decision.status, decision.reason = LINK_URL_UNAVAILABLE, (
                    f"no usable URL: {create_failure}")
                decision.would_write = False
            else:
                link_status = (LINK_ORGANIZATION_VIEW if kind == "organization"
                               else LINK_DRIVE_ITEM_WEB_URL)
                result = self.repository.write_link(
                    connection, session_id=target.legacy_session_id,
                    meeting_id=target.meeting_id, lecture_key=target.lecture_key,
                    recording_url=url, candidate=decision.match.candidate,
                    link_status=link_status)
                if result["sessions_updated"] == 1:
                    decision.status, decision.reason = WRITTEN, link_status
                    outcome.update(written=True, legacy_sessions_written=1,
                                   perfect_rows_written=result["perfect_rows_updated"],
                                   link_status=link_status)
                else:
                    # Someone (the live n8n branch) linked it first. Their link
                    # stands; ours is not written. That is success, not a race.
                    decision.status, decision.reason = (
                        WRITE_REFUSED_ALREADY_LINKED,
                        "recording_url was set concurrently; left unchanged")
        outcome["status"], outcome["reason"] = decision.status, decision.reason
        outcome["stage_state"] = decision.stage_state
        if persist:
            outcome["state"] = self.repository.record(
                connection, decision, written=outcome["written"],
                perfect_rows_written=outcome["perfect_rows_written"])
        outcome["graph_calls"] = self.graph_calls
        return outcome
