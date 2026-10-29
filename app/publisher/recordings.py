"""
P0-C: independent recording discovery for ONE lecture.

Starts from the authoritative lecture row (lecture_sessions), never from
qa_doctors_sessions: no legacy QA row, no legacy transcript id, no QA cycle,
no OpenAI, no n8n precheck. Reuses, read-only:

    recordings_path              (app/recordings/graph_lookup.py)  the organizer-aware recordings route
    DriveItemDiscovery + sources (app/recordings/drive_items.py)   tenant search, OneDrive, channel folder
    CandidateMetadataReader      (app/recordings/drive_items.py)   size + video duration of one driveItem
    recording_mapping            (app/publisher/recording_mapping.py) occurrence, per-part file, duration,
                                                                    transcript corroboration
    LectureLockManager           (app/orchestration/locks.py)      the per-lecture advisory lock

Never used: RecordingLinkPublisher (createLink is a Graph WRITE) and the
legacy qa_doctors_sessions write. No URL is persisted or logged.

LIFECYCLE
---------
load lecture -> per-lecture try-lock (else DEFERRED) -> not applicable / not
ended / meeting unresolved -> stored LOCATED and no re-check due: reuse, no
Graph -> otherwise list the meeting's callRecordings, select THIS
occurrence's parts, discover candidate MP4s, map one file per part, read each
matched file's size + measured duration, check it against the part's Graph
window, corroborate with the stored transcript parts when there are any ->
one short write transaction (lecture row + every part row) -> release lock.

A stored LOCATED lecture is re-checked at bounded points (24 h after its
scheduled end) or on request: same recordings, same files, unchanged size
and duration. A failed re-check read keeps the verified mapping.

SEVERAL RECORDINGS (2026-10-28). The occurrence's recordings are mapped as
before, then ONE is selected: the longest by actual duration
(recording_mapping.select_recording). The lecture is LOCATED when that part
is; the shorter ones stay on record, are never imported, and nothing of theirs
blocks the lecture. While the selected recording is still a fragment of the
slot and the occurrence window (scheduled end + 3 h) is open, a longer one may
still appear: WAITING. The selection is persisted in the discovery's metadata
(`recording_selection`) and carried through every later re-check; a lecture
located before this rule has none and keeps its parts as they were, until an
operator re-selects it (`rediscover` on a LOCATED lecture: a fresh evaluation
that replaces the stored one only when it is LOCATED itself).

TRANSACTIONS AND LOCKING
------------------------
One autocommit connection per run; the per-lecture SESSION advisory lock is
the same one P0-B and QA's lecture-scoped stages take, so two discovery
workers, a transcript worker and a QA lecture stage never work one lecture at
once. No transaction is open during a Graph call. The worker writes only its
own two tables (027), so it does not wait for a QA cycle; reads of transcript
selections are plain MVCC snapshots. The unique indexes (one owner per Graph
recording and per SharePoint file) are the backstop for anything the locks
do not cover.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import psycopg

from app.common.errors import PlatformError
from app.db.repositories.recording_sources import STATUS_FOR_KIND, RecordingSourceRepository
from app.graph.transport import GraphError
from app.lms.meeting_key import from_join_url
from app.orchestration.locks import LectureLockManager
from app.publisher import recording_mapping as rm
from app.recordings.graph_lookup import recordings_path
from app.recordings.models import DriveItemCandidate, RecordingTarget
from app.recordings.resolution import DEFAULT_POLICY, FRAGMENT
from app.transcripts.selection import SELECTION_VERSION, WINDOW_AFTER

WORKER_VERSION = "publisher_recordings_v1"

LOCATED = "LOCATED"                # every part of the occurrence has a verified file
WAITING = "WAITING"                # Teams/SharePoint has not produced it yet (bounded by a deadline)
RETRY = "RETRY"                    # transient: network, 401 after refresh, 408, 429, 5xx, database
DEFERRED = "DEFERRED"              # another worker or a QA lecture stage holds the lecture
REVIEW = "REVIEW"                  # ambiguity, conflict, permission or deadline
NOT_APPLICABLE = "NOT_APPLICABLE"  # cancelled, LI-suppressed duplicate
PERMANENT = "PERMANENT"            # the lecture does not exist

ACQUIRING = (WAITING, RETRY)
_SEVERITY = {WAITING: 1, RETRY: 2, REVIEW: 3}

KEPT = "PREVIOUSLY_VERIFIED_SOURCE_KEPT"

LOAD_LECTURE = """
SELECT lecture_id, meeting_id, meeting_lookup_user_id, meeting_organizer_user_id, scheduled_start,
       scheduled_end, session_date, subject, join_url, downstream_ready, is_cancelled,
       metadata -> 'duplicate_suppression' IS NOT NULL
  FROM public.lecture_sessions WHERE lecture_id = %s
"""


@dataclass(frozen=True)
class RecordingPolicy:
    # A lecture whose recording is still not located this long after its end
    # leaves the retry loop for review.
    deadline_after_end: timedelta = timedelta(hours=72)
    # Bounded re-check of a LOCATED lecture: late parts, moved or changed files.
    verify_points: tuple = (timedelta(hours=24),)
    defer_delay: timedelta = timedelta(minutes=10)
    lock_timeout_ms: int = 5_000
    statement_timeout_ms: int = 60_000

    def waiting_delay(self, since_end: timedelta) -> timedelta:
        if since_end < timedelta(hours=2):
            return timedelta(minutes=15)
        if since_end < timedelta(hours=12):
            return timedelta(minutes=30)
        if since_end < timedelta(hours=24):
            return timedelta(hours=1)
        return timedelta(hours=3)

    def retry_delay(self, attempt: int) -> timedelta:
        steps = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
        return steps[attempt - 1] if 1 <= attempt <= len(steps) else timedelta(hours=3)

    def verify_due(self, scheduled_end: datetime, checked_at: datetime | None, now: datetime) -> bool:
        if checked_at is None:
            return True
        return any(scheduled_end + p <= now and checked_at < scheduled_end + p for p in self.verify_points)

    def next_verify_at(self, scheduled_end: datetime, now: datetime) -> datetime | None:
        upcoming = [scheduled_end + p for p in self.verify_points if scheduled_end + p > now]
        return min(upcoming) if upcoming else None


@dataclass
class RecordingOutcome:
    lecture_id: str
    kind: str
    reason: str
    retry_after: timedelta | None = None
    next_refresh_at: datetime | None = None
    graph_calls: int = 0
    expected_part_count: int | None = None
    located_part_count: int = 0
    review_reason: str | None = None
    transcript_pairing: str = "NOT_CHECKED"
    parts: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    error_code: str | None = None
    http_status: int | None = None

    @property
    def missing_part_count(self) -> int | None:
        if self.expected_part_count is None:
            return None
        return max(self.expected_part_count - self.located_part_count, 0)

    @property
    def recording_status(self) -> str:
        return STATUS_FOR_KIND.get(self.kind, self.kind)

    def summary(self) -> dict:
        """The P0-C coverage result for one lecture."""
        return {"lecture_id": self.lecture_id, "expected_part_count": self.expected_part_count,
                "verified_source_part_count": self.located_part_count,
                "missing_part_count": self.missing_part_count, "recording_status": self.recording_status,
                "reason": self.reason, "review_reason": self.review_reason}

    def as_event(self) -> dict:
        """Identity and counts only - never a URL or file name."""
        return {**self.summary(), "kind": self.kind, "graph_calls": self.graph_calls,
                "transcript_pairing": self.transcript_pairing, "parts": self.parts,
                "worker_version": WORKER_VERSION, **self.detail}


class _Deferred(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Lecture:
    lecture_id: str
    exists: bool
    meeting_id: str | None = None
    lookup_user: str | None = None
    organizer: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    session_date: object = None
    subject: str | None = None
    join_url: str | None = None
    ready: bool = False
    cancelled: bool = False
    suppressed: bool = False

    @property
    def thread_id(self) -> str | None:
        key = from_join_url(self.join_url)
        return key.rsplit("/", 1)[0] if key else None

    @property
    def expected_duration(self) -> float | None:
        seconds = (self.scheduled_end - self.scheduled_start).total_seconds()
        return seconds if seconds > 0 else None

    def target(self) -> RecordingTarget:
        return RecordingTarget(
            lecture_id=self.lecture_id, session_date=self.session_date.isoformat(), legacy_session_id=None,
            legacy_meeting_id=None, lecture_meeting_id=self.meeting_id, subject=self.subject,
            meeting_lookup_user_id=self.lookup_user, meeting_organizer_user_id=self.organizer,
            existing_recording_url=None, cancelled=False, cancelled_reason=None, lecture_key=None,
            perfect_recording_url_empty=None, thread_id=self.thread_id,
            scheduled_start=self.scheduled_start, scheduled_end=self.scheduled_end)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cause(exc) -> BaseException:
    return exc.__cause__ if isinstance(exc, PlatformError) and exc.__cause__ is not None else exc


class RecordingWorker:
    def __init__(self, *, connect, graph, discovery_factory, evidence_reader, policy: RecordingPolicy | None = None,
                 resolution_policy=None, clock=utcnow, lock_manager=None, repository=None):
        self.connect = connect                      # () -> psycopg connection; autocommit is set here
        self.graph = graph                          # app-only client: get_collection, invalidate_token
        self.discovery_factory = discovery_factory  # () -> a FRESH DriveItemDiscovery (its caches are per run)
        self.reader = evidence_reader               # CandidateMetadataReader
        self.policy = policy or RecordingPolicy()
        self.resolution_policy = resolution_policy or DEFAULT_POLICY
        self.clock = clock
        self.locks = lock_manager or LectureLockManager()
        self.repository = repository or RecordingSourceRepository()
        self.log = logging.getLogger(__name__)

    # -- entry point ------------------------------------------------------------------
    def run(self, lecture_id, *, attempt: int = 1, verify: bool = False, rediscover: bool = False,
            dry_run: bool = False) -> RecordingOutcome:
        """
        `verify`: re-check a LOCATED lecture now. `rediscover`: an operator re-opens a REVIEW_REQUIRED one, or
        re-evaluates a LOCATED one. `dry_run`: evaluate on a READ ONLY connection and persist nothing.
        """
        lecture_id = str(lecture_id)
        try:
            connection = self.connect()
            connection.autocommit = True
            if dry_run:
                connection.read_only = True
        except Exception as exc:  # noqa: BLE001 - any connect failure is a database outage
            return RecordingOutcome(lecture_id, RETRY, f"DATABASE_UNAVAILABLE:{type(exc).__name__}",
                                    retry_after=self.policy.retry_delay(attempt))
        counters = {"graph_calls": 0}
        try:
            outcome = self._run(connection, lecture_id, attempt, verify, rediscover, counters, dry_run)
        except _Deferred as deferred:
            outcome = RecordingOutcome(lecture_id, DEFERRED, deferred.reason, retry_after=self.policy.defer_delay)
        except (psycopg.Error, PlatformError) as exc:
            cause = _cause(exc)
            reason = ("LOCK_TIMEOUT" if isinstance(cause, psycopg.errors.LockNotAvailable)
                      else f"DATABASE_ERROR:{type(cause).__name__}")
            outcome = RecordingOutcome(lecture_id, RETRY, reason, retry_after=self.policy.retry_delay(attempt))
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        outcome.graph_calls = counters["graph_calls"]
        return outcome

    def _run(self, connection, lecture_id, attempt, verify, rediscover, counters, dry_run=False) -> RecordingOutcome:
        lecture = self._load(connection, lecture_id)
        if not lecture.exists:
            return RecordingOutcome(lecture_id, PERMANENT, "LECTURE_NOT_FOUND")
        if not self.locks.try_acquire(connection, lecture_id):
            raise _Deferred("LECTURE_LOCKED")
        try:
            now = self.clock()
            with connection.transaction():
                discovery, stored = self.repository.load(connection, lecture_id)
            outcome, write = self._evaluate(connection, lecture, discovery, stored, now, attempt, verify,
                                            rediscover, counters)
            outcome = self._deadline(lecture, now, outcome)
            if write is not None and not dry_run:
                self._persist(connection, lecture, outcome, write, now)
            if outcome.kind == LOCATED:
                outcome.next_refresh_at = self.policy.next_verify_at(lecture.scheduled_end, now)
            return outcome
        finally:
            try:
                self.locks.release(connection, lecture_id)
            except PlatformError:
                self.log.warning("lecture lock release failed for %s; released when the session ends", lecture_id)

    # -- evaluation (Graph; no transaction open) ----------------------------------------
    def _evaluate(self, connection, lecture, discovery, stored, now, attempt, verify, rediscover, counters):
        lid = lecture.lecture_id
        if discovery is not None and discovery["status"] == "REVIEW_REQUIRED" and not rediscover:
            # Review is retained: nothing re-points or forgets a reviewed lecture until an operator asks.
            outcome = RecordingOutcome(lid, REVIEW, discovery["reason_code"], review_reason=discovery["review_reason"],
                                       expected_part_count=discovery["expected_part_count"],
                                       located_part_count=discovery["located_part_count"], detail={"retained": True})
            return outcome, None
        if lecture.cancelled:
            return RecordingOutcome(lid, NOT_APPLICABLE, "LECTURE_CANCELLED"), {}
        if lecture.suppressed:
            return RecordingOutcome(lid, NOT_APPLICABLE, "LI_DUPLICATE"), {}
        if now < lecture.scheduled_end:
            return RecordingOutcome(lid, WAITING, "LECTURE_NOT_ENDED", retry_after=min(
                lecture.scheduled_end - now + timedelta(minutes=10), timedelta(hours=6))), {}
        since_end = now - lecture.scheduled_end
        if not (lecture.ready and lecture.meeting_id and lecture.lookup_user):
            return RecordingOutcome(lid, WAITING, "MEETING_NOT_RESOLVED",
                                    retry_after=self.policy.waiting_delay(since_end)), {}

        located_before = discovery is not None and discovery["status"] == "LOCATED"
        if located_before:
            selection = discovery.get("recording_selection")
            if rediscover:
                # Operator re-selection: the stored, verified mapping stays unless the fresh one is LOCATED.
                outcome, write = self._discover(connection, lecture, stored, now, attempt, counters)
                if outcome.kind == LOCATED:
                    return outcome, write
                return self._stored(lid, "RESELECTION_INCONCLUSIVE_STORED_KEPT", stored, selection=selection,
                                    detail={"reselection": outcome.reason}), None
            checked = discovery["last_verified_at"] or discovery["located_at"]
            if not verify and not self.policy.verify_due(lecture.scheduled_end, checked, now):
                return self._stored(lid, "STORED_SOURCE_REUSED", stored, selection=selection), None
            outcome, write = self._verify(connection, lecture, stored, now, counters, selection)
            if selection and "recording_selection" not in outcome.detail:
                outcome.detail["recording_selection"] = selection       # a re-check never forgets the choice
            return outcome, write
        return self._discover(connection, lecture, stored, now, attempt, counters)

    def _discover(self, connection, lecture, stored, now, attempt, counters):
        lid, since_end = lecture.lecture_id, now - lecture.scheduled_end
        listing = self._list_recordings(lecture, counters)
        if isinstance(listing, GraphError):
            return self._graph_failure(lid, "GRAPH_RECORDINGS", listing, attempt, since_end, stored), {}
        occurrence = rm.select_occurrence(listing, scheduled_start=lecture.scheduled_start,
                                          scheduled_end=lecture.scheduled_end, session_date=lecture.session_date)
        detail = dict(occurrence.detail)
        if occurrence.status == rm.GRAPH_RESPONSE_INVALID:
            return RecordingOutcome(lid, RETRY, rm.GRAPH_RESPONSE_INVALID, detail=detail,
                                    retry_after=self.policy.retry_delay(attempt)), {}
        if occurrence.status == rm.NO_RECORDING_FOR_OCCURRENCE:
            # Another occurrence's recording of a series meeting is not this one's.
            return RecordingOutcome(lid, WAITING, "RECORDING_NOT_YET_AVAILABLE", detail=detail, expected_part_count=0,
                                    retry_after=self.policy.waiting_delay(since_end)), {}
        expected = len(occurrence.parts)
        call_ids = {p.call_id for p in occurrence.parts if p.call_id}
        if occurrence.status == rm.AMBIGUOUS_OCCURRENCE_RECORDINGS:
            return self._review(lid, rm.AMBIGUOUS_OCCURRENCE_RECORDINGS, expected, detail), {"call_ids": call_ids}
        if occurrence.in_progress:
            return RecordingOutcome(lid, WAITING, "RECORDING_IN_PROGRESS", expected_part_count=expected, detail=detail,
                                    retry_after=self.policy.waiting_delay(since_end)), {"call_ids": call_ids}

        discovered, discovery_calls = self._discover_files(lecture, counters)
        detail.update(sources_attempted=list(discovered.sources_attempted),
                      source_counts=dict(discovered.source_counts))
        if not discovered.sources_attempted:
            return self._review(lid, "NO_APPLICABLE_DISCOVERY_SOURCE", expected, detail), {"call_ids": call_ids}
        if discovered.sources_failed:
            # A source that could not be read may hold another qualifying file:
            # "exactly one file per part" is unproven, so nothing new is located.
            failure = self._source_failure(lid, discovered.sources_failed, attempt, since_end)
            failure.expected_part_count, failure.detail = expected, {**detail, **failure.detail}
            failure.located_part_count = sum(1 for r in stored.values() if r["locate_status"] == rm.LOCATED)
            return failure, {"call_ids": call_ids}

        assigned, file_diagnostics = rm.assign_files(occurrence.parts, discovered.candidates, subject=lecture.subject,
                                                     session_date=lecture.session_date.isoformat())
        detail.update(file_diagnostics)
        mappings, evidence_failure = [], None
        for index, (part, kept, shared) in enumerate(assigned, start=1):
            if len(kept) == 1 and not shared:
                enriched = self._enrich(kept[0].candidate, counters)
                if isinstance(enriched, GraphError):
                    if enriched.status == 404:
                        kept = []                                   # the file vanished since discovery
                    else:
                        evidence_failure = evidence_failure or enriched
                        mappings.append(rm.map_part(index, part, [], 0, expected_duration=lecture.expected_duration,
                                                    policy=self.resolution_policy))
                        continue
                else:
                    kept = [kept[0].__class__(enriched, kept[0].lead, kept[0].recording)]
            elif len(kept) > 1 and not shared:
                # Several distinct files fit this part: read each one's own duration so map_part can
                # tell the recording from a stray short file. Any read failure decides nothing.
                fresh = [self._enrich(exact.candidate, counters) for exact in kept]
                if any(isinstance(f, GraphError) for f in fresh):
                    fresh = [replace(exact.candidate, duration_seconds=None) for exact in kept]
                else:
                    detail["several_files_durations_read"] = len(kept)
                kept = [exact.__class__(f, exact.lead, exact.recording) for exact, f in zip(kept, fresh)]
            mappings.append(rm.map_part(index, part, kept, shared, expected_duration=lecture.expected_duration,
                                        policy=self.resolution_policy))
        if evidence_failure is not None:
            failure = self._graph_failure(lid, "SOURCE_METADATA", evidence_failure, attempt, since_end, stored)
            failure.expected_part_count = expected
            return failure, {"call_ids": call_ids}
        mappings = rm.claimed_twice(mappings)

        mappings, pairing, conflict = rm.pair_transcripts(mappings, occurrence.all_recordings,
                                                          self._transcript_parts(connection, lecture))
        if conflict:
            outcome = self._review(lid, conflict, expected, detail)
            outcome.transcript_pairing = pairing
            return outcome, {"call_ids": call_ids, "pairing": pairing}
        mappings, merge_conflict = rm.merge_with_stored(mappings, stored)
        if merge_conflict:
            return self._review(lid, merge_conflict, expected, detail), {"call_ids": call_ids}
        return self._aggregate(lid, mappings, pairing, detail, since_end, lecture, now, stored), {
            "call_ids": call_ids, "pairing": pairing, "mappings": mappings}

    def _aggregate(self, lid, mappings, pairing, detail, since_end, lecture, now, stored) -> RecordingOutcome:
        selection = rm.select_recording(mappings, stored)
        detail["recording_selection"] = selection.as_detail()
        parts = [{"part_index": m.part_index, "locate_status": m.locate_status, "reason": m.reason,
                  "classification": m.classification, "transcript_pairing": m.transcript_pairing,
                  "selected": m.part_index == selection.part_index} for m in mappings]
        if selection.part_index is None:
            self.log.info("recording selection undecided for %s: %s", lid, selection.rule)
            return RecordingOutcome(lid, WAITING, selection.rule, retry_after=self.policy.waiting_delay(since_end),
                                    expected_part_count=len(mappings),
                                    located_part_count=sum(1 for m in mappings if m.located),
                                    transcript_pairing=pairing, parts=parts, detail=detail)
        chosen = next(m for m in mappings if m.part_index == selection.part_index)
        # Expected = the ONE selected recording; the shorter ones are on record, never required.
        base = {"expected_part_count": 1, "located_part_count": 1 if chosen.located else 0,
                "transcript_pairing": pairing, "parts": parts, "detail": detail}
        if chosen.locate_status in (rm.AMBIGUOUS, rm.DURATION_MISMATCH):
            return RecordingOutcome(lid, REVIEW, chosen.reason,
                                    review_reason=f"PART_{chosen.part_index}:{chosen.reason}", **base)
        if not chosen.located:
            reason = ("FILE_DURATION_UNAVAILABLE" if chosen.locate_status == rm.DURATION_PENDING
                      else "RECORDING_FILE_NOT_FOUND")
            return RecordingOutcome(lid, WAITING, reason, retry_after=self.policy.waiting_delay(since_end), **base)
        window_closes = lecture.scheduled_end + WINDOW_AFTER
        if chosen.classification == FRAGMENT and now < window_closes:
            # The occurrence's recordings are not final until its window closes: a longer one may follow.
            return RecordingOutcome(lid, WAITING, "LONGER_RECORDING_MAY_FOLLOW",
                                    retry_after=min(self.policy.waiting_delay(since_end), window_closes - now), **base)
        reason = "ALL_PARTS_LOCATED" if len(mappings) == 1 else "LONGEST_RECORDING_SELECTED"
        return RecordingOutcome(lid, LOCATED, reason, **base)

    def _verify(self, connection, lecture, stored, now, counters, selection=None):
        """Re-check a LOCATED lecture: same recordings, same files, unchanged media, consistent transcript."""
        lid = lecture.lecture_id
        located = {k: v for k, v in stored.items() if v["locate_status"] == rm.LOCATED}
        if selection:
            # Only the selected recording's file is the lecture's; the set compared is every stored part.
            on_record, located = stored, {k: v for k, v in located.items()
                                          if v["part_index"] == selection.get("part_index")}
        else:
            on_record = located
        listing = self._list_recordings(lecture, counters)
        if isinstance(listing, GraphError):
            return self._stored(lid, "REVERIFY_FAILED_STORED_KEPT", stored,
                                detail={"verify_failure": f"GRAPH_RECORDINGS:{listing.status}"}), {"touch": True}
        occurrence = rm.select_occurrence(listing, scheduled_start=lecture.scheduled_start,
                                          scheduled_end=lecture.scheduled_end, session_date=lecture.session_date)
        if occurrence.status == rm.GRAPH_RESPONSE_INVALID:
            return self._stored(lid, "REVERIFY_FAILED_STORED_KEPT", stored,
                                detail={"verify_failure": rm.GRAPH_RESPONSE_INVALID}), {"touch": True}
        now_ids = [p.recording_id for p in occurrence.parts]
        stored_ids = [k for k, _ in sorted(on_record.items(), key=lambda kv: kv[1]["part_index"])]
        if occurrence.status != rm.SELECTED or now_ids != stored_ids:
            outcome = self._review(lid, "RECORDING_SET_CHANGED", len(now_ids),
                                   {**occurrence.detail, "stored_part_count": len(stored_ids)})
            outcome.located_part_count = min(len(stored_ids), len(now_ids))
            return outcome, {}
        statuses, failures = {}, []
        for recording_id, row in located.items():
            candidate = DriveItemCandidate(item_id=row["source_item_id"], drive_id=row["source_drive_id"],
                                           name=row["source_filename"] or "", web_url=None, source="verify")
            fresh = self._enrich(candidate, counters)
            if isinstance(fresh, GraphError):
                if fresh.status == 404:
                    statuses[recording_id] = rm.SOURCE_MISSING
                else:
                    failures.append(fresh)
                continue
            # enrich() hands back its input when the item is no longer this MP4.
            changed = fresh is candidate or not _unchanged(row, fresh)
            statuses[recording_id] = rm.SOURCE_CHANGED if changed else rm.LOCATED
        bad = sorted((v, k) for k, v in statuses.items() if v != rm.LOCATED)
        still = sum(1 for v in statuses.values() if v == rm.LOCATED) + len(failures)
        if bad:
            status = bad[0][0]
            index = located[bad[0][1]]["part_index"]
            reason = "SOURCE_FILE_MISSING" if status == rm.SOURCE_MISSING else "SOURCE_FILE_CHANGED"
            outcome = RecordingOutcome(lid, REVIEW, reason, review_reason=f"PART_{index}:{reason}",
                                       expected_part_count=len(located), located_part_count=still)
            return outcome, {"verified": statuses}
        if failures:
            return self._stored(lid, "REVERIFY_FAILED_STORED_KEPT", stored,
                                detail={"verify_failure": f"SOURCE_METADATA:{failures[0].status}"}), {
                "verified": statuses, "partial": True}
        # A transcript stored since the recording was located: validate the part relationship now.
        parts = {p.recording_id: p for p in occurrence.parts}
        mappings = [rm.PartMapping(part_index=row["part_index"], recording=parts[rid], locate_status=row["locate_status"],
                                   reason=KEPT) for rid, row in sorted(on_record.items(), key=lambda kv: kv[1]["part_index"])]
        paired, pairing, conflict = rm.pair_transcripts(mappings, occurrence.all_recordings,
                                                        self._transcript_parts(connection, lecture))
        if conflict:
            outcome = self._review(lid, conflict, len(located), {})
            outcome.located_part_count, outcome.transcript_pairing = len(located), pairing
            return outcome, {"pairing": pairing}
        outcome = self._stored(lid, "SOURCES_VERIFIED", stored, selection=selection)
        outcome.transcript_pairing = pairing
        links = {m.recording.recording_id: (m.transcript_artifact_id, m.transcript_part_index, m.transcript_pairing)
                 for m in paired if m.transcript_artifact_id and m.located}
        return outcome, {"verified": statuses, "verified_all": True, "pairing": pairing, "transcripts": links}

    def _transcript_parts(self, connection, lecture) -> list:
        with connection.transaction():
            rows = self.repository.transcript_parts(connection, lecture.lecture_id, SELECTION_VERSION)
        return [rm.TranscriptPartRef(str(r[0]), r[1], (r[2] or "").lower() or None, r[3], r[4]) for r in rows]

    # -- persistence (one short transaction) ---------------------------------------------
    def _persist(self, connection, lecture, outcome, write, now) -> None:
        if outcome.kind in (DEFERRED, PERMANENT):
            return
        lid = lecture.lecture_id
        mappings = write.get("mappings")
        next_check = (now + outcome.retry_after if outcome.retry_after is not None else
                      self.policy.next_verify_at(lecture.scheduled_end, now) if outcome.kind == LOCATED else None)
        with self._write(connection):
            if mappings is not None and self.repository.foreign_owners(connection, lid, mappings):
                outcome.kind, outcome.reason = REVIEW, "SOURCE_CLAIMED_BY_ANOTHER_LECTURE"
                outcome.review_reason, mappings = "SOURCE_CLAIMED_BY_ANOTHER_LECTURE", None
                outcome.located_part_count, next_check = 0, None
            if outcome.expected_part_count is not None:
                outcome.located_part_count = min(outcome.located_part_count, outcome.expected_part_count)
            self.repository.record(
                connection, lid, version=WORKER_VERSION, kind=outcome.kind, reason=outcome.reason,
                review_reason=outcome.review_reason if outcome.kind == REVIEW else None,
                expected=outcome.expected_part_count, located=outcome.located_part_count,
                missing=outcome.missing_part_count, call_ids=write.get("call_ids"),
                pairing=write.get("pairing", "NOT_CHECKED"), next_check=next_check, now=now,
                verified=bool(write.get("verified_all")) or (outcome.kind == LOCATED and mappings is not None),
                error_code=outcome.error_code, http_status=outcome.http_status,
                metadata={k: v for k, v in outcome.detail.items()
                          if isinstance(v, (int, float, str, bool, list, dict))})
            if mappings is not None:
                self.repository.replace_parts(connection, lid, mappings, method=rm.VERIFICATION_METHOD, now=now,
                                              kept_reason=KEPT)
            if write.get("verified"):
                self.repository.mark_verified(connection, lid, write["verified"], now=now)
            if write.get("transcripts"):
                self.repository.link_transcripts(connection, lid, write["transcripts"])

    @contextmanager
    def _write(self, connection):
        with connection.transaction():
            connection.execute(f"SET LOCAL lock_timeout = {int(self.policy.lock_timeout_ms)}")
            connection.execute(f"SET LOCAL statement_timeout = {int(self.policy.statement_timeout_ms)}")
            yield

    # -- Graph ---------------------------------------------------------------------------
    def _call(self, counters, call):
        """One Graph call; on HTTP 401 drop the cached token and try exactly once more."""
        for attempt in (1, 2):
            counters["graph_calls"] += 1
            try:
                return call()
            except GraphError as error:
                if error.status == 401 and attempt == 1 and hasattr(self.graph, "invalidate_token"):
                    self.graph.invalidate_token()
                    continue
                return error
        return None  # pragma: no cover

    def _list_recordings(self, lecture, counters):
        path = recordings_path(lecture.lookup_user, lecture.meeting_id)
        return self._call(counters, lambda: self.graph.get_collection(path))

    def _discover_files(self, lecture, counters):
        target = lecture.target()
        for attempt in (1, 2):
            discovery = self.discovery_factory()
            result = discovery.discover(target)
            counters["graph_calls"] += discovery.calls
            unauthorized = any(f.get("http_status") == 401 for f in result.sources_failed)
            if unauthorized and attempt == 1 and hasattr(self.graph, "invalidate_token"):
                self.graph.invalidate_token()
                continue
            return result, discovery.calls
        return result, 0  # pragma: no cover

    def _enrich(self, candidate, counters):
        before = self.reader.calls
        outcome = self._call({"graph_calls": 0}, lambda: self.reader.enrich(candidate))
        counters["graph_calls"] += max(self.reader.calls - before, 1)
        return outcome

    # -- classification ------------------------------------------------------------------
    def _graph_failure(self, lid, step, error: GraphError, attempt, since_end, stored) -> RecordingOutcome:
        located = sum(1 for r in stored.values() if r["locate_status"] == rm.LOCATED)
        status, message = error.status, str(error.message or "").casefold()
        common = {"located_part_count": located, "error_code": str(error.code or "")[:120], "http_status": status}
        if status in (401, 408, 429) or status is None or status >= 500:
            label = {None: "NETWORK", 401: "UNAUTHORIZED", 408: "TIMEOUT", 429: "RATE_LIMITED"}.get(
                status, "SERVER_ERROR")
            return RecordingOutcome(lid, RETRY, f"{step}_{label}:{status}",
                                    retry_after=self.policy.retry_delay(attempt), **common)
        if status == 403 or "application access policy" in message:
            reason = f"{step}_ACCESS_DENIED:{status}"
            return RecordingOutcome(lid, REVIEW, reason, review_reason=reason, **common)
        if status == 404:
            # The meeting / item is not (yet) visible to the lookup user: bounded wait.
            return RecordingOutcome(lid, WAITING, f"{step}_NOT_FOUND:404",
                                    retry_after=self.policy.waiting_delay(since_end), **common)
        reason = f"{step}_REJECTED:{status}"
        return RecordingOutcome(lid, REVIEW, reason, review_reason=reason, **common)

    def _source_failure(self, lid, failures, attempt, since_end) -> RecordingOutcome:
        outcomes = []
        for failure in failures:
            status, code = failure.get("http_status"), str(failure.get("error_code") or "")
            name = failure.get("source", "source").upper()
            if code.endswith("search_results_truncated") or code.endswith("files_folder_unresolved"):
                reason = f"SHAREPOINT_{name}:{code}"
                outcomes.append(RecordingOutcome(lid, REVIEW, reason, review_reason=reason, http_status=status))
            elif status == 404:
                # e.g. the channel is not in the lookup user's teams: a location we cannot read.
                reason = f"SHAREPOINT_LOCATION_INACCESSIBLE:{name}"
                outcomes.append(RecordingOutcome(lid, REVIEW, reason, review_reason=reason, http_status=status))
            else:
                error = GraphError("Microsoft Graph", status, code, "")
                outcomes.append(self._graph_failure(lid, f"SHAREPOINT_{name}", error, attempt, since_end, {}))
        worst = max(outcomes, key=lambda o: _SEVERITY.get(o.kind, 0))
        worst.detail = {"sources_failed": [{"source": f.get("source"), "http_status": f.get("http_status")}
                                           for f in failures]}
        return worst

    # -- helpers -------------------------------------------------------------------------
    def _review(self, lid, reason, expected, detail) -> RecordingOutcome:
        return RecordingOutcome(lid, REVIEW, reason, review_reason=reason, expected_part_count=expected,
                                detail=detail)

    def _stored(self, lid, reason, stored, *, detail=None, selection=None) -> RecordingOutcome:
        located = [r for r in stored.values() if r["locate_status"] == rm.LOCATED]
        chosen = selection.get("part_index") if selection else None
        parts = [{"part_index": r["part_index"], "locate_status": r["locate_status"],
                  "recording_source_id": str(r["recording_source_id"]),
                  **({"selected": r["part_index"] == chosen} if selection else {})}
                 for r in sorted(stored.values(), key=lambda r: r["part_index"])]
        count = 1 if selection else len(located)
        detail = {**(detail or {}), **({"recording_selection": selection} if selection else {})}
        return RecordingOutcome(lid, LOCATED, reason, expected_part_count=count,
                                located_part_count=count, parts=parts, detail=detail)

    def _deadline(self, lecture, now, outcome) -> RecordingOutcome:
        if outcome.kind in ACQUIRING and lecture.scheduled_end is not None \
                and now > lecture.scheduled_end + self.policy.deadline_after_end:
            reason = f"DEADLINE_PASSED:{outcome.reason}"
            outcome.kind, outcome.reason, outcome.review_reason, outcome.retry_after = REVIEW, reason, reason, None
        return outcome

    def _load(self, connection, lecture_id) -> _Lecture:
        with connection.transaction():
            row = connection.execute(LOAD_LECTURE, (lecture_id,)).fetchone()
        if row is None:
            return _Lecture(lecture_id, exists=False)
        (lid, meeting_id, user, organizer, start, end, session_date, subject, join_url, ready, cancelled,
         suppressed) = row
        return _Lecture(str(lid), True, meeting_id, user, organizer, start, end, session_date, subject, join_url,
                        bool(ready), bool(cancelled), bool(suppressed))


def _unchanged(row: dict, fresh) -> bool:
    """Same physical file, same bytes count, same measured length (within the alignment tolerance)."""
    if fresh is None or fresh.physical_id != (row["source_drive_id"], row["source_item_id"]):
        return False
    if row["source_size_bytes"] and fresh.size_bytes and int(row["source_size_bytes"]) != int(fresh.size_bytes):
        return False
    stored, now = row["source_duration_seconds"], fresh.duration_seconds
    if stored is not None and now is not None:
        tolerance = max(rm.MIN_DURATION_TOLERANCE_SECONDS, rm.DURATION_TOLERANCE_RATIO * float(stored))
        if abs(float(stored) - now) > tolerance:
            return False
    return True
