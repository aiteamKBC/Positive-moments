"""
P0-B: independent Microsoft Teams transcript acquisition for ONE lecture.

Reuses the existing storage and logic - no second transcript store:

    TranscriptGateway            (app/transcripts/graph.py)       list + fetch raw WebVTT
    TranscriptArtifactRepository (app/db/repositories/...)        artifact rows, append-only content versions
    select_transcript_parts      (app/transcripts/selection.py)   occurrence window selection
    TranscriptSelectionService.select_lecture                     the QA selection body, one lecture
    LectureLockManager           (app/orchestration/locks.py)     the per-lecture advisory lock

TRANSACTIONS
------------
One autocommit connection per job, so the per-lecture SESSION advisory lock
survives between short explicit transactions. Every database step is its own
`connection.transaction()` with lock_timeout/statement_timeout; NO transaction
is open while Microsoft Graph is called. Stored raw content is committed the
moment it is fetched, before selection runs, so nothing later (selection, QA,
a crash) can take it back.

CONCURRENCY WITH QA (P0-F: no dependency on QA's cycle status)
-----------------------------------------------------------------
QA's transcript stages are day-scoped and run inside the scheduler's ONE cycle
transaction, so every transcript row QA writes stays row-locked until the
cycle commits. QA always writes a transcript in the same order: upsert the
artifact row (locking it), then store its content; upsert the lecture's
selection row (locking it), then its parts and combined transcript. The worker
does not look at QA's cycle at all. Instead every write transaction starts by
locking the SAME parent row with FOR UPDATE NOWAIT:

    artifact registration / content / failure status -> the artifact row(s)
    selection, parts, combined transcript            -> the selection row

  * QA already wrote that transcript in its open cycle -> the lock fails at
    once, nothing is written, the job is DEFERRED (ROW_LOCKED) and retried
    after QA commits. QA's rows are never touched while QA owns them.
  * QA has not reached it yet -> the worker holds the parent row for one
    short transaction; QA's own upsert of that row waits (milliseconds), then
    QA reads the committed result: an identical content hash is reused (no
    duplicate version), a different one appends the next version. Version
    numbers are never allocated by two writers at once, and no version is lost
    or overwritten.
  * The only other wait - a row QA has INSERTED but not committed - is bounded
    by lock_timeout (default 250 ms, below PostgreSQL's default 1 s
    deadlock_timeout), so if a wait could ever close a cycle with QA the worker
    gives up first and PostgreSQL never picks QA's transaction as the victim.

Lecture-scoped QA stages hold the per-lecture lock, which the worker also
takes (try-lock, never waits). The unique constraints on (artifact_id,
version_number) and (artifact_id, content_sha256) remain the backstop.

FETCH POLICY
------------
Only the parts selected for THIS occurrence are downloaded (a series meeting
lists every occurrence's transcripts). Stored content is reused. A stored part
is re-fetched only at the bounded refresh points (6 h and 24 h after the
lecture's scheduled end, once each), and a failed re-fetch never touches stored
content or its status.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from app.common.errors import PlatformError
from app.common.hashing import content_sha256, provider_transcript_identity
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository, TranscriptSelectionRunRepository,
)
from app.orchestration.locks import LectureLockManager
from app.transcripts.models import (
    BLOCKED_TRANSCRIPT_ACCESS, NO_TRANSCRIPTS_AVAILABLE, PROVIDER_MICROSOFT_GRAPH, TRANSCRIPT_NOT_FOUND,
    TRANSCRIPT_NOT_READY, TRANSCRIPTS_FOUND, ReadyLecture,
)
from app.transcripts.selection import SELECTED, SELECTION_VERSION, select_transcript_parts
from app.transcripts.selection_service import (
    COMBINE_FAILED, TRANSCRIPT_UNUSABLE, TranscriptSelectionService, combine_failure_kind,
)

WORKER_VERSION = "publisher_transcripts_v1"

# Outcome kinds. The queue handler maps them onto job transitions.
READY = "READY"                    # selection SELECTED, every selected part's raw VTT stored
WAITING = "WAITING"                # Teams has not produced it yet (expected; bounded by a deadline)
RETRY = "RETRY"                    # transient failure: network, 429, 5xx, 401 after refresh, DB, lock timeout
DEFERRED = "DEFERRED"              # a row or lecture another writer holds right now; not a failure
REVIEW = "REVIEW"                  # configuration, unusable transcript, or deadline passed
NOT_APPLICABLE = "NOT_APPLICABLE"  # cancelled, LI-suppressed duplicate: nothing to acquire
PERMANENT = "PERMANENT"            # the lecture does not exist

ACQUIRING = (WAITING, RETRY)

_SEVERITY = {WAITING: 1, RETRY: 2, REVIEW: 3}


@dataclass(frozen=True)
class TranscriptPolicy:
    # A lecture with no usable transcript this long after its scheduled end
    # leaves the retry loop for review (audit section 15: 72 h -> NO_TRANSCRIPT).
    deadline_after_end: timedelta = timedelta(hours=72)
    # Bounded revision check: each stored part is re-fetched at most once per
    # point, and never after the last one.
    refresh_points: tuple = (timedelta(hours=6), timedelta(hours=24))
    defer_delay: timedelta = timedelta(minutes=10)
    # A transcript row another writer (QA's open cycle) holds: try again soon.
    row_locked_delay: timedelta = timedelta(minutes=5)
    # Below PostgreSQL's default deadlock_timeout (1 s): see CONCURRENCY WITH QA.
    lock_timeout_ms: int = 250
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

    def refresh_due(self, scheduled_end: datetime, content_fetched_at: datetime | None, now: datetime) -> bool:
        """True when a refresh point has passed since this content was last fetched."""
        if content_fetched_at is None:
            return True
        return any(scheduled_end + p <= now and content_fetched_at < scheduled_end + p
                   for p in self.refresh_points)

    def next_refresh_at(self, scheduled_end: datetime, now: datetime) -> datetime | None:
        upcoming = [scheduled_end + p for p in self.refresh_points if scheduled_end + p > now]
        return min(upcoming) if upcoming else None


@dataclass
class TranscriptOutcome:
    lecture_id: str
    kind: str
    reason: str
    retry_after: timedelta | None = None
    next_refresh_at: datetime | None = None
    graph_calls: int = 0
    detail: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        """Identity and counts only - never transcript text, URLs or Graph ids."""
        return {"kind": self.kind, "reason": self.reason, "graph_calls": self.graph_calls,
                "worker_version": WORKER_VERSION, **self.detail}


class _Deferred(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Lecture:
    ready: ReadyLecture | None
    lecture_id: str
    scheduled_end: datetime | None
    cancelled: bool
    duplicate_suppressed: bool
    exists: bool = True


LOAD_LECTURE = """
SELECT lecture_id, meeting_id, meeting_lookup_user_id, scheduled_start, scheduled_end,
       session_date, subject, module, downstream_ready, is_cancelled,
       metadata -> 'duplicate_suppression' IS NOT NULL,
       meeting_lookup_context_source, join_url_oid_hint_status
  FROM public.lecture_sessions WHERE lecture_id = %s
"""
STORED_STATE = """
SELECT s.selection_status,
       count(p.artifact_id),
       count(p.artifact_id) FILTER (WHERE a.content_sha256 IS NOT NULL),
       min(a.content_fetched_at)
  FROM public.lecture_transcript_selections s
  LEFT JOIN public.lecture_transcript_selection_parts p ON p.selection_id = s.selection_id
  LEFT JOIN public.lecture_transcript_artifacts a ON a.artifact_id = p.artifact_id
 WHERE s.lecture_id = %s AND s.selection_version = %s
 GROUP BY s.selection_status
"""
SELECTED_PARTS = """
SELECT p.part_index, p.artifact_id, p.provider_transcript_id, a.content_version, a.content_sha256,
       a.speaker_attribution, p.part_offset_ms, p.is_primary
  FROM public.lecture_transcript_selections s
  JOIN public.lecture_transcript_selection_parts p ON p.selection_id = s.selection_id
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = p.artifact_id
 WHERE s.lecture_id = %s AND s.selection_version = %s
 ORDER BY p.part_index
"""
FETCHED_AT = "SELECT content_fetched_at FROM public.lecture_transcript_artifacts WHERE artifact_id = %s"
# Write guards: the parent row QA locks first when it writes the same data.
LOCK_ARTIFACTS = """
SELECT artifact_id FROM public.lecture_transcript_artifacts
 WHERE artifact_id = ANY(%s::uuid[]) ORDER BY artifact_id FOR UPDATE NOWAIT
"""
LOCK_SELECTION = """
SELECT selection_id FROM public.lecture_transcript_selections
 WHERE lecture_id = %s AND selection_version = %s FOR UPDATE NOWAIT
"""
ROW_LOCKED = "ROW_LOCKED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cause(exc) -> BaseException:
    """The psycopg error a repository's PlatformError wraps (or the error itself)."""
    return exc.__cause__ if isinstance(exc, PlatformError) and exc.__cause__ is not None else exc


class TranscriptWorker:
    def __init__(self, *, connect, gateway, graph_client=None, policy: TranscriptPolicy | None = None,
                 clock=utcnow, lock_manager=None, artifact_repository=None, selection_repository=None):
        self.connect = connect                      # () -> psycopg connection; autocommit is set here
        self.gateway = gateway
        self.graph_client = graph_client            # optional: invalidate_token() on HTTP 401
        self.policy = policy or TranscriptPolicy()
        self.clock = clock
        self.locks = lock_manager or LectureLockManager()
        self.artifacts = artifact_repository or TranscriptArtifactRepository()
        self.selections = selection_repository or TranscriptSelectionRepository()
        self.selection_service = TranscriptSelectionService(
            lecture_repository=None, selection_repository=self.selections,
            run_repository=TranscriptSelectionRunRepository())
        self.log = logging.getLogger(__name__)

    # -- entry point ----------------------------------------------------------------
    def run(self, lecture_id, *, attempt: int = 1) -> TranscriptOutcome:
        lecture_id = str(lecture_id)
        try:
            connection = self.connect()
            connection.autocommit = True
        except Exception as exc:  # noqa: BLE001 - any connect failure is a database outage
            return TranscriptOutcome(lecture_id, RETRY, f"DATABASE_UNAVAILABLE:{type(exc).__name__}",
                                     retry_after=self.policy.retry_delay(attempt))
        try:
            outcome = self._run(connection, lecture_id, attempt)
        except _Deferred as deferred:
            outcome = TranscriptOutcome(lecture_id, DEFERRED, deferred.reason, retry_after=self.policy.defer_delay)
        except (psycopg.Error, PlatformError) as exc:
            cause = _cause(exc)
            if isinstance(cause, psycopg.errors.LockNotAvailable):
                # Another writer (typically QA's open cycle) holds this transcript's
                # rows. Nothing was written; retry once it has committed.
                outcome = TranscriptOutcome(lecture_id, DEFERRED, ROW_LOCKED,
                                            retry_after=self.policy.row_locked_delay)
            else:
                outcome = TranscriptOutcome(lecture_id, RETRY, f"DATABASE_ERROR:{type(cause).__name__}",
                                            retry_after=self.policy.retry_delay(attempt))
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        return outcome

    # -- the lifecycle ---------------------------------------------------------------
    def _run(self, connection, lecture_id, attempt) -> TranscriptOutcome:
        lecture = self._load(connection, lecture_id)
        if not lecture.exists:
            return TranscriptOutcome(lecture_id, PERMANENT, "LECTURE_NOT_FOUND")
        if lecture.cancelled:
            return TranscriptOutcome(lecture_id, NOT_APPLICABLE, "LECTURE_CANCELLED")
        if lecture.duplicate_suppressed:
            return TranscriptOutcome(lecture_id, NOT_APPLICABLE, "LI_DUPLICATE")
        now = self.clock()
        if lecture.ready is None:
            return self._deadline(lecture, now, TranscriptOutcome(
                lecture_id, WAITING, "MEETING_NOT_RESOLVED",
                retry_after=self.policy.waiting_delay(now - lecture.scheduled_end)))
        if now < lecture.scheduled_end:
            return TranscriptOutcome(lecture_id, WAITING, "LECTURE_NOT_ENDED",
                                     retry_after=min(lecture.scheduled_end - now + timedelta(minutes=10),
                                                     timedelta(hours=6)))
        if not self.locks.try_acquire(connection, lecture_id):
            raise _Deferred("LECTURE_LOCKED")
        try:
            outcome = self._acquire(connection, lecture, now, attempt)
        finally:
            try:
                self.locks.release(connection, lecture_id)
            except PlatformError:
                self.log.warning("lecture lock release failed for %s; released when the session ends", lecture_id)
        outcome.next_refresh_at = (self.policy.next_refresh_at(lecture.scheduled_end, now)
                                   if outcome.kind == READY else None)
        return self._deadline(lecture, now, outcome)

    def _acquire(self, connection, lecture: _Lecture, now, attempt) -> TranscriptOutcome:
        ready = lecture.ready
        counters = {"graph_calls": 0, "artifacts_listed": 0, "parts_selected": 0, "parts_reused": 0,
                    "parts_fetched": 0, "content_versions_created": 0, "refresh_failures": 0}

        stored = self._stored_state(connection, lecture)
        if stored["complete"] and not stored["refresh_due"]:
            return self._ready(connection, lecture, "STORED_TRANSCRIPT_REUSED", counters)

        listing = self._graph(counters, lambda: self.gateway.list_transcripts(
            ready.meeting_lookup_user_id, ready.meeting_id))
        if listing.status != TRANSCRIPTS_FOUND:
            failure = self._classify(listing.status, listing.http_status, attempt, now - lecture.scheduled_end)
            if stored["complete"]:
                # A refresh that could not even list: keep what is stored.
                counters["refresh_failures"] += 1
                return self._ready(connection, lecture, "REFRESH_FAILED_STORED_KEPT", counters,
                                   extra={"refresh_failure": failure.reason})
            return self._with(failure, lecture, counters)
        counters["artifacts_listed"] = len(listing.artifacts)

        # Register every listed artifact for this lecture (cheap rows, no content).
        with self._write(connection, artifacts=[self._artifact_id(a) for a in listing.artifacts]):
            for artifact in listing.artifacts:
                self.artifacts.upsert(
                    connection, self._artifact_id(artifact), ready.lecture_id,
                    provider=PROVIDER_MICROSOFT_GRAPH, provider_transcript_id=artifact.provider_transcript_id,
                    meeting_id=ready.meeting_id, meeting_lookup_user_id=ready.meeting_lookup_user_id,
                    provider_call_id=artifact.provider_call_id,
                    content_correlation_id=artifact.content_correlation_id,
                    provider_created_at=artifact.provider_created_at, provider_end_at=artifact.provider_end_at,
                    artifact_status="DISCOVERED", metadata=artifact.metadata)

        # Which artifacts belong to THIS occurrence? (the same rule QA uses)
        with connection.transaction():
            candidates = self.selections.load_candidates(connection, ready.lecture_id)
        plan = select_transcript_parts(candidates, scheduled_start=ready.scheduled_start,
                                       scheduled_end=ready.scheduled_end, target_date=ready.session_date,
                                       meeting_id=ready.meeting_id)
        if plan.status != SELECTED:
            self._persist_selection(connection, ready)
            # Other occurrences' transcripts may exist; none is in this window.
            return self._with(TranscriptOutcome(
                ready_id(ready), WAITING, f"NO_TRANSCRIPT_FOR_OCCURRENCE:{plan.status}",
                retry_after=self.policy.waiting_delay(now - lecture.scheduled_end)), lecture, counters)

        counters["parts_selected"] = len(plan.parts)
        failures: list[TranscriptOutcome] = []
        for part in plan.parts:
            candidate = part.candidate
            if candidate.content_sha256 is not None and not self._part_refresh_due(
                    connection, candidate.artifact_id, lecture, now):
                counters["parts_reused"] += 1
                continue
            content = self._graph(counters, lambda c=candidate: self.gateway.fetch_content(
                ready.meeting_lookup_user_id, ready.meeting_id, c.provider_transcript_id))
            if content.raw is None:
                if candidate.content_sha256 is not None:
                    counters["refresh_failures"] += 1          # stored content stays exactly as it was
                    continue
                with self._write(connection, artifacts=[candidate.artifact_id]):
                    self.artifacts.mark_content_failure(connection, candidate.artifact_id,
                                                        content.status, content.reason)
                failures.append(self._classify(content.status, content.http_status, attempt,
                                               now - lecture.scheduled_end))
                continue
            created = self._store(connection, candidate.artifact_id, content)
            counters["parts_fetched"] += 1
            counters["content_versions_created"] += int(created)

        selection = self._persist_selection(connection, ready)
        # QA's selection marks a lecture SELECTED when it can combine ANY of its
        # parts. Publishing needs every selected part's raw VTT, so READY here
        # is stricter and leaves QA's semantics alone.
        if selection["selection_status"] == SELECTED and not failures \
                and self._all_parts_stored(connection, lecture):
            return self._ready(connection, lecture, "TRANSCRIPT_STORED", counters)
        if selection["selection_status"] == COMBINE_FAILED and \
                combine_failure_kind(selection.get("combine_error")) == TRANSCRIPT_UNUSABLE:
            return self._with(TranscriptOutcome(ready_id(ready), REVIEW, "TRANSCRIPT_UNUSABLE"), lecture, counters)
        if failures:
            worst = max(failures, key=lambda f: _SEVERITY.get(f.kind, 0))
            return self._with(worst, lecture, counters)
        return self._with(TranscriptOutcome(ready_id(ready), WAITING, "CONTENT_NOT_YET_STORED",
                                            retry_after=self.policy.waiting_delay(now - lecture.scheduled_end)),
                          lecture, counters)

    # -- helpers ------------------------------------------------------------------------
    def _graph(self, counters, call):
        """One Graph call; on HTTP 401 drop the cached token and try exactly once more."""
        result = call()
        counters["graph_calls"] += 1
        if getattr(result, "http_status", None) == 401 and self.graph_client is not None \
                and hasattr(self.graph_client, "invalidate_token"):
            self.graph_client.invalidate_token()
            result = call()
            counters["graph_calls"] += 1
        return result

    def _classify(self, status, http_status, attempt, since_end) -> TranscriptOutcome:
        """Map a gateway answer onto waiting / transient / configuration."""
        if status in (NO_TRANSCRIPTS_AVAILABLE, TRANSCRIPT_NOT_READY, TRANSCRIPT_NOT_FOUND):
            return TranscriptOutcome("", WAITING, status, retry_after=self.policy.waiting_delay(since_end))
        if status == BLOCKED_TRANSCRIPT_ACCESS:
            # Permission or application access policy: no amount of retrying fixes it.
            return TranscriptOutcome("", REVIEW, f"GRAPH_ACCESS_BLOCKED:{http_status}")
        if http_status is None or http_status in (401, 408, 429) or http_status >= 500:
            label = {None: "NETWORK", 401: "UNAUTHORIZED", 408: "TIMEOUT", 429: "RATE_LIMITED"}.get(
                http_status, "SERVER_ERROR")
            return TranscriptOutcome("", RETRY, f"GRAPH_{label}:{http_status}",
                                     retry_after=self.policy.retry_delay(attempt))
        return TranscriptOutcome("", REVIEW, f"GRAPH_REJECTED:{http_status}")

    def _with(self, outcome, lecture, counters) -> TranscriptOutcome:
        outcome.lecture_id = lecture.lecture_id
        outcome.graph_calls = counters["graph_calls"]
        outcome.detail = {**counters, **outcome.detail}
        return outcome

    def _ready(self, connection, lecture, reason, counters, *, extra=None) -> TranscriptOutcome:
        with connection.transaction():
            rows = connection.execute(SELECTED_PARTS, (lecture.lecture_id, SELECTION_VERSION)).fetchall()
        parts = [{"part_index": r[0], "artifact_id": str(r[1]), "content_version": r[3],
                  "content_sha256": r[4], "speaker_attribution": r[5], "part_offset_ms": r[6],
                  "is_primary": r[7]} for r in rows]
        outcome = TranscriptOutcome(lecture.lecture_id, READY, reason,
                                    detail={"selection_version": SELECTION_VERSION, "parts": parts, **(extra or {})})
        return self._with(outcome, lecture, counters)

    def _deadline(self, lecture, now, outcome) -> TranscriptOutcome:
        if outcome.kind in ACQUIRING and lecture.scheduled_end is not None \
                and now > lecture.scheduled_end + self.policy.deadline_after_end:
            return TranscriptOutcome(outcome.lecture_id, REVIEW, f"DEADLINE_PASSED:{outcome.reason}",
                                     graph_calls=outcome.graph_calls, detail=outcome.detail)
        return outcome

    @staticmethod
    def _artifact_id(artifact):
        return provider_transcript_identity(provider=PROVIDER_MICROSOFT_GRAPH,
                                            provider_transcript_id=artifact.provider_transcript_id)

    @contextmanager
    def _write(self, connection, *, artifacts=(), selection_of=None):
        """
        One short write transaction, guarded by the parent row(s) QA locks first
        when it writes the same transcript (see CONCURRENCY WITH QA). A guard
        held elsewhere raises LockNotAvailable at once: nothing is written.
        """
        with connection.transaction():
            connection.execute(f"SET LOCAL lock_timeout = {int(self.policy.lock_timeout_ms)}")
            connection.execute(f"SET LOCAL statement_timeout = {int(self.policy.statement_timeout_ms)}")
            if artifacts:
                connection.execute(LOCK_ARTIFACTS, (sorted({str(a) for a in artifacts}),))
            if selection_of is not None:
                connection.execute(LOCK_SELECTION, (selection_of, SELECTION_VERSION))
            yield

    def _load(self, connection, lecture_id) -> _Lecture:
        with connection.transaction():
            row = connection.execute(LOAD_LECTURE, (lecture_id,)).fetchone()
        if row is None:
            return _Lecture(None, lecture_id, None, False, False, exists=False)
        (lid, meeting_id, user_id, start, end, session_date, subject, module, downstream_ready,
         cancelled, suppressed, context_source, oid_hint) = row
        ready = None
        if downstream_ready and meeting_id and user_id:          # the Phase 1 contract QA uses
            ready = ReadyLecture(lecture_id=lid, meeting_id=meeting_id, meeting_lookup_user_id=user_id,
                                 scheduled_start=start, scheduled_end=end, session_date=session_date,
                                 subject=subject, module=module, meeting_lookup_context_source=context_source,
                                 join_url_oid_hint_status=oid_hint)
        return _Lecture(ready, str(lid), end, bool(cancelled), bool(suppressed))

    def _stored_state(self, connection, lecture) -> dict:
        with connection.transaction():
            row = connection.execute(STORED_STATE, (lecture.lecture_id, SELECTION_VERSION)).fetchone()
        if row is None:
            return {"complete": False, "refresh_due": False}
        status, parts, stored, oldest_fetch = row
        complete = status == SELECTED and parts > 0 and stored == parts
        return {"complete": complete,
                "refresh_due": complete and self.policy.refresh_due(lecture.scheduled_end, oldest_fetch, self.clock())}

    def _all_parts_stored(self, connection, lecture) -> bool:
        with connection.transaction():
            rows = connection.execute(SELECTED_PARTS, (lecture.lecture_id, SELECTION_VERSION)).fetchall()
        return bool(rows) and all(row[4] is not None for row in rows)

    def _part_refresh_due(self, connection, artifact_id, lecture, now) -> bool:
        with connection.transaction():
            row = connection.execute(FETCHED_AT, (artifact_id,)).fetchone()
        return self.policy.refresh_due(lecture.scheduled_end, row[0] if row else None, now)

    def _store(self, connection, artifact_id, content) -> bool:
        """Commit the raw bytes now, under the artifact row lock. The unique-violation backstop re-reads once."""
        digest = content_sha256(content.raw)
        for attempt in (1, 2):
            try:
                with self._write(connection, artifacts=[artifact_id]):
                    stored = self.artifacts.store_content(
                        connection, artifact_id, raw_text=content.raw.decode("utf-8", errors="replace"),
                        content_sha256=digest, content_bytes=len(content.raw),
                        content_format=content.content_format,
                        speaker_attribution=bool(content.speaker_attribution))
                return bool(stored["version_created"])
            except PlatformError as exc:
                if attempt == 2 or not isinstance(_cause(exc), psycopg.errors.UniqueViolation):
                    raise
        return False

    def _persist_selection(self, connection, ready) -> dict:
        counters = {"selections_created": 0, "selections_updated": 0, "combined_transcripts_created": 0,
                    "combined_transcripts_reused": 0, "multi_part_selections": 0, "error_count": 0}
        with self._write(connection, selection_of=ready_id(ready)):
            return self.selection_service.select_lecture(connection, ready, counters, persist=True)


def ready_id(ready) -> str:
    return str(ready.lecture_id)
