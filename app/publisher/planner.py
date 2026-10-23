"""
The 5-minute planning tick: discover, match, and schedule independent work.

    1. Teams calendar discovery (Lecture Intelligence's own LectureDiscoveryService)
       for today - or, on catch-up and the daily FULL run, every lookback day.
       It runs whether or not a QA scheduler cycle is active. QA's cycle is one
       long transaction that also upserts lecture_sessions, so each publisher
       upsert is its own short transaction that never waits on a row another
       writer holds (FOR UPDATE NOWAIT + a short lock_timeout): a row QA is
       writing right now is skipped for this tick (QA is persisting it; the
       next tick re-reads it), and every other lecture of the day is still
       discovered. QA's writes are never blocked for longer than one
       single-row publisher statement.
    2. LMS occurrence matching v2 and the two-sided coverage (P0-A): every LMS
       occurrence gets a coverage state, so a week-additional or LMS-only session
       surfaces instead of disappearing. The LMS side never CREATES a lecture:
       an LMS-only session waits in bunny_lms_occurrence_coverage until LI discovers
       it, so one session can never get two lecture identities.
    3. Track jobs for every eligible lecture once it has ended:
       ACQUIRE_TRANSCRIPT and RESOLVE_RECORDING, independently, once each
       (their own follow-ups refresh / verify; a reviewed track is restarted only
       by an operator).
    4. Bunny jobs per INCLUDED recording part, derived from that part's persisted
       state (no live row -> IMPORT_VIDEO; identified -> POLL_ENCODING; verified ->
       PUBLISH_CAPTION), only in a write mode and only for a VERIFIED LMS
       occurrence. A kind whose last job ended in review is left to the operator.

SCOPE. Lectures before PUBLISHER_START_DATE are historical-backfill scope:
never planned and never marked failed. A missing LMS match is a visible state
(PENDING / NO_LMS_SESSION / ...), never an inference that a lecture is legacy.

No transaction is held across Graph or LMS reads (autocommit connection; each
repository write is its own short transaction).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.common.time import CAIRO
from app.db.repositories.bunny_publisher import BunnyCaptionRepository, BunnyVideoRepository
from app.db.repositories.lms_matches import LmsMatchRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.lms import service as lms_service
from app.lms.matching import MatchConfig
from app.lms.sources import load_lectures, window_for
from app.publisher.handlers import BUNNY_PRIORITY, LIVE_PRIORITY, MAX_ATTEMPTS, part_subject_key, subject_key
from app.publisher.progress import INCLUDE, NEEDS_DECISION, load_facts

log = logging.getLogger(__name__)

EXCLUDED_LMS = ("TEST_EXCLUDED", "OUT_OF_LMS_SCOPE", "LI_DUPLICATE")
ELIGIBLE = """
SELECT l.lecture_id, l.scheduled_end, COALESCE(e.match_status, 'PENDING'),
       COALESCE(d.status, 'PENDING'), l.meeting_id IS NOT NULL
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_lms_match_evaluations e ON e.lecture_id = l.lecture_id AND e.is_current
  LEFT JOIN public.bunny_recording_discoveries d ON d.lecture_id = l.lecture_id
 WHERE l.session_date BETWEEN %s AND %s
   AND NOT l.is_cancelled
   AND l.metadata -> 'duplicate_suppression' IS NULL
 ORDER BY l.scheduled_start, l.lecture_id
"""
NEWER_CONTENT = """
SELECT EXISTS (SELECT 1 FROM public.lecture_transcript_artifacts
                WHERE artifact_id = %s AND content_version > %s)
"""


def cairo_today(now: datetime) -> date:
    return now.astimezone(CAIRO).date()


@dataclass
class PlanSummary:
    calendar: list = field(default_factory=list)
    lms: dict = field(default_factory=dict)
    lectures_considered: int = 0
    jobs_created: dict = field(default_factory=dict)
    would_import_parts: int = 0
    fragments_needing_decision: int = 0
    duplicates_blocked: int = 0
    duplicates_skipped: int = 0
    lectures_ready: int = 0

    def created(self, kind: str) -> None:
        self.jobs_created[kind] = self.jobs_created.get(kind, 0) + 1

    def as_dict(self) -> dict:
        return {"calendar": self.calendar, "lms": self.lms, "lectures_considered": self.lectures_considered,
                "jobs_created": self.jobs_created, "would_import_parts": self.would_import_parts,
                "fragments_needing_decision": self.fragments_needing_decision,
                "duplicates_blocked": self.duplicates_blocked, "duplicates_skipped": self.duplicates_skipped,
                "lectures_ready": self.lectures_ready}


class Planner:
    def __init__(self, *, config, connect, lms_source=None, calendar=None, queue=None, clock=None):
        self.config = config
        self.connect = connect
        self.lms_source = lms_source          # .load(window_start, window_end) -> LmsSnapshot
        self.calendar = calendar              # CalendarDiscovery or None
        self.queue = queue or PublisherQueueRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.videos, self.captions = BunnyVideoRepository(), BunnyCaptionRepository()

    def window(self, now: datetime) -> tuple[date, date]:
        today = cairo_today(now)
        end_date = getattr(self.config, "end_date", None)
        if end_date and today > end_date:
            today = end_date                     # a scoped replay: nothing after its last day
        start = today - timedelta(days=self.config.lookback_days)
        if self.config.start_date and start < self.config.start_date:
            start = self.config.start_date
        return start, today

    def tick(self, *, calendar_days: list[date] | None = None) -> dict:
        now = self.clock()
        summary = PlanSummary()
        date_from, today = self.window(now)
        connection = self.connect()
        connection.autocommit = True
        try:
            if date_from > today:
                summary.lms = {"status": "BEFORE_START_DATE"}
                return summary.as_dict()
            self._discover(connection, calendar_days if calendar_days is not None else [today], summary)
            self._match(connection, date_from, today, now, summary)
            self._plan(connection, date_from, today, now, summary)
        finally:
            connection.close()
        return summary.as_dict()

    # -- 1. calendar ------------------------------------------------------------------------
    def _discover(self, connection, days, summary) -> None:
        if not self.config.discover_calendar or self.calendar is None:
            summary.calendar.append({"status": "DISABLED"})
            return
        for day in days:
            if self.config.start_date and day < self.config.start_date:
                continue
            try:
                outcome = self.calendar.discover_day(connection, day)
                locked = outcome.get("registry_rows_locked", 0)
                summary.calendar.append({"session_date": day.isoformat(),
                                         "status": "COMPLETED_WITH_LOCKED_ROWS" if locked else "COMPLETED",
                                         "created": outcome.get("registry_rows_created", 0),
                                         "updated": outcome.get("registry_rows_updated", 0),
                                         "locked": locked})
            except Exception as exc:  # noqa: BLE001 - one day failing must not stop matching or planning
                log.warning("calendar discovery failed for %s: %s", day, type(exc).__name__)
                summary.calendar.append({"session_date": day.isoformat(), "status": "FAILED",
                                         "error": getattr(exc, "code", type(exc).__name__)})

    # -- 2. LMS ------------------------------------------------------------------------------
    def _match(self, connection, date_from, date_to, now, summary) -> None:
        if self.lms_source is None:
            summary.lms = {"status": "DISABLED"}
            return
        config = MatchConfig.from_text(self.config.test_programmes)
        try:
            lectures = load_lectures(connection, date_from, date_to)
            snapshot = self.lms_source.load(*window_for(date_from, date_to))
        except Exception as exc:  # noqa: BLE001 - the LMS being down leaves the stored decisions in place
            summary.lms = {"status": "FAILED", "error": getattr(exc, "code", type(exc).__name__)}
            return
        results = lms_service.evaluate(lectures, snapshot, config)
        recorded = lms_service.persist(connection, lectures, results)
        in_range = type(snapshot)(
            occurrences=tuple(o for o in snapshot.occurrences if date_from <= o.scheduled_start.date() <= date_to),
            shares=snapshot.shares, window_start=snapshot.window_start, window_end=snapshot.window_end)
        repository = LmsMatchRepository()
        rows = lms_service.reconcile_coverage(connection, in_range, config, now, repository=repository)
        repository.upsert_coverage(connection, rows)
        by_coverage: dict = {}
        for row in rows:
            by_coverage[row.coverage_status] = by_coverage.get(row.coverage_status, 0) + 1
        summary.lms = {"status": "COMPLETED", "lectures": len(results), "recorded": recorded,
                       "summary": lms_service.summarize(results)["by_status"], "coverage": by_coverage,
                       "lms_only_week_additional": sum(
                           1 for r in rows if r.coverage_status in ("LMS_ONLY_PENDING", "LMS_SESSION_NOT_DISCOVERED")
                           and str(r.occurrence.meeting_type) == "week-additional")}

    # -- 3/4. jobs ---------------------------------------------------------------------------
    def _plan(self, connection, date_from, date_to, now, summary) -> None:
        rows = connection.execute(ELIGIBLE, (date_from, date_to)).fetchall()
        for lecture_id, scheduled_end, lms_status, recording_status, meeting_resolved in rows:
            lecture_id = str(lecture_id)
            if lms_status in EXCLUDED_LMS:
                continue
            summary.lectures_considered += 1
            if scheduled_end is None or scheduled_end > now:
                continue
            if not meeting_resolved and self._blocked_duplicate(connection, lecture_id):
                # A calendar duplicate the validated rule conclusively blocks (DUPLICATE_LOSER), with no
                # Teams meeting to read from: no recording or transcript can ever be found for it, so no
                # track job. Ambiguous groups and any lecture with a resolved meeting - with or without
                # an LMS match - still get both tracks.
                summary.duplicates_skipped += 1
                continue
            for kind, priority in (("ACQUIRE_TRANSCRIPT", LIVE_PRIORITY), ("RESOLVE_RECORDING", LIVE_PRIORITY)):
                if self.queue.enqueue_once(connection, job_kind=kind, subject_key=subject_key(lecture_id),
                                           lecture_id=lecture_id, priority=priority, max_attempts=MAX_ATTEMPTS):
                    summary.created(kind)
            if lms_status == "VERIFIED" and recording_status == "LOCATED":
                self._plan_parts(connection, lecture_id, summary)

    @staticmethod
    def _blocked_duplicate(connection, lecture_id) -> bool:
        from app.publisher.progress import DUPLICATE_LOSER
        with connection.transaction():
            facts = load_facts(connection, lecture_id)
        return facts.duplicate_block == DUPLICATE_LOSER

    def _plan_parts(self, connection, lecture_id, summary) -> None:
        from app.publisher.bunny_jobs import mark_lecture_ready
        with connection.transaction():
            facts = load_facts(connection, lecture_id)
        if facts.duplicate_unresolved:
            # A duplicate loser or an ambiguous group never reaches Bunny; the
            # reconciler raises DUPLICATE_BLOCKED so it is visible for review.
            summary.duplicates_blocked += 1
            return
        library = self.config.library_id
        for part in facts.parts:
            policy = facts.policy(part)
            if policy == NEEDS_DECISION:
                summary.fragments_needing_decision += 1
            if policy != INCLUDE:
                continue
            if not self.config.writes_bunny or library is None:
                summary.would_import_parts += 1
                continue
            kind = self._next_kind(connection, library, lecture_id, part["part_index"])
            if kind is None:
                continue
            subject = part_subject_key(lecture_id, part["part_index"])
            latest = self.queue.latest(connection, kind, subject)
            if latest is not None and latest["status"] in ("REVIEW_REQUIRED", "FAILED", "QUEUED", "RUNNING",
                                                            "RETRY_WAIT"):
                continue                       # live already, or waiting for an operator
            _, created = self.queue.enqueue(connection, job_kind=kind, subject_key=subject, lecture_id=lecture_id,
                                            priority=BUNNY_PRIORITY[kind], max_attempts=MAX_ATTEMPTS)
            if created:
                summary.created(kind)
        if self.config.writes_bunny and library is not None and mark_lecture_ready(
                connection, lecture_id, library, self.config.caption_language, self.queue):
            summary.lectures_ready += 1

    def _next_kind(self, connection, library, lecture_id, part_index) -> str | None:
        """What this part needs next, from its persisted state only."""
        with connection.transaction():
            row = self.videos.live_for_part(connection, library, lecture_id, part_index)
            if row is None or row["import_status"] in ("PENDING", "SUBMITTING", "SUBMITTED", "SUBMISSION_UNCERTAIN"):
                return "IMPORT_VIDEO"
            if row["import_status"] != "IDENTIFIED":
                return None                    # FAILED / REVIEW_REQUIRED: the operator decides
            if row["encoding_status"] in ("NOT_STARTED", "IN_PROGRESS", "FINISHED"):
                return "POLL_ENCODING"
            if row["encoding_status"] != "VERIFIED":
                return None
            current = self.captions.current(connection, library, row["bunny_video_id"],
                                            self.config.caption_language)
            if current is None or current["publication_status"] != "VERIFIED":
                return "PUBLISH_CAPTION"
            newer = connection.execute(NEWER_CONTENT, (current["source_artifact_id"],
                                                       current["source_content_version"])).fetchone()[0]
            return "PUBLISH_CAPTION" if newer else None


LOCKED = "locked"


class ConflictTolerantLectureRepository:
    """
    LectureSessionRepository.upsert, made safe to run beside QA's cycle.

    Each lecture is ONE short transaction holding ONE registry row:
      * FOR UPDATE NOWAIT on an existing row - a row QA's open cycle
        transaction has written fails at once instead of waiting;
      * a short lock_timeout covers the only other wait, a brand-new lecture
        QA has inserted but not committed yet.
    Either way the answer is LOCKED for that lecture only: QA is persisting
    it, nothing of the publisher's is half-written, and discovery continues
    with the next lecture. Holding a single row per transaction, the
    publisher can never be part of a deadlock with QA.
    """

    def __init__(self, *, repository=None, lock_timeout_ms: int = 250):
        from app.db.repositories.lecture_sessions import LectureSessionRepository
        self.repository = repository or LectureSessionRepository()
        self.lock_timeout_ms = int(lock_timeout_ms)
        self.locked: list[str] = []

    def upsert(self, connection, lecture) -> str:
        import psycopg

        from app.common.errors import PlatformError
        try:
            with connection.transaction():
                connection.execute(f"SET LOCAL lock_timeout = {self.lock_timeout_ms}")
                connection.execute("SELECT 1 FROM public.lecture_sessions WHERE lecture_id = %s FOR UPDATE NOWAIT",
                                   (lecture.lecture_id,))
                return self.repository.upsert(connection, lecture)
        except (psycopg.Error, PlatformError) as exc:
            cause = exc.__cause__ if isinstance(exc, PlatformError) and exc.__cause__ is not None else exc
            if isinstance(cause, psycopg.errors.LockNotAvailable):
                self.locked.append(str(lecture.lecture_id))
                return LOCKED
            raise

    def count_day(self, connection, target_date) -> int:
        return self.repository.count_day(connection, target_date)


class PublisherDiscoveryRunRepository:
    """
    lecture_discovery_runs written by the publisher. The `mode` column keeps Lecture Intelligence's
    own vocabulary (SHADOW = persisted, its CHECK allows only SHADOW / DRY_RUN), so each run's
    metadata also names who ran it and the publisher's Bunny mode - "SHADOW" alone was misleading
    for a STAGING_WRITE publisher. Existing rows are never rewritten.
    """

    def __init__(self, repository, publisher_mode: str):
        self.repository, self.publisher_mode = repository, publisher_mode

    def start(self, connection, target_date):
        return self.repository.start(connection, target_date)

    def complete(self, connection, run_id, summary: dict) -> None:
        metadata = {**(summary.get("metadata") or {}), "runner": "bunny_publisher",
                    "publisher_mode": self.publisher_mode}
        self.repository.complete(connection, run_id, {**summary, "metadata": metadata})


class CalendarDiscovery:
    """
    Lecture Intelligence's own LectureDiscoveryService, assembled here from the
    same parts app.orchestration.factory uses, WITHOUT importing the
    orchestrator (whose import closure includes QA). Aptem is opened per day
    and closed at once.

    Two deliberate differences from QA's assembly, neither of which changes
    which calendar events become lectures:
      * no QA comparison: qa_validation_repository=None, so discovery never
        reads qa_doctors_sessions (that read only fed a diagnostic report);
      * the registry writes go through ConflictTolerantLectureRepository.
    """

    def __init__(self, config, *, service=None, aptem_connect=None, lock_timeout_ms: int = 250):
        self.config = config
        self._service = service
        self._aptem_connect = aptem_connect
        self.lock_timeout_ms = lock_timeout_ms

    @property
    def service(self):
        if self._service is None:
            from app.db.repositories.aptem import AptemRepository
            from app.db.repositories.discovery_runs import DiscoveryRunRepository
            from app.db.repositories.lms import LmsModuleRepository
            from app.graph.auth import build_graph_client
            from app.graph.calendar import CalendarGateway
            from app.graph.meetings import OnlineMeetingGateway
            from app.lectures.service import LectureDiscoveryService
            client = build_graph_client(self.config)
            self._service = LectureDiscoveryService(
                calendar=CalendarGateway(client, self.config.calendar_user_upn),
                meetings=OnlineMeetingGateway(client, self.config.calendar_user_upn),
                aptem_repository=AptemRepository(),
                lecture_repository=ConflictTolerantLectureRepository(lock_timeout_ms=self.lock_timeout_ms),
                run_repository=PublisherDiscoveryRunRepository(DiscoveryRunRepository(), self.config.mode),
                qa_validation_repository=None,
                lms_repository=LmsModuleRepository(self.config.lms_database_url) if self.config.lms_database_url
                else None)
        return self._service

    def discover_day(self, connection, day: date) -> dict:
        import psycopg
        service = self.service
        registry = service.lecture_repository
        if isinstance(registry, ConflictTolerantLectureRepository):
            registry.locked = []
        from app.publisher.config import DB_CONNECT_KWARGS
        connect = self._aptem_connect or (lambda: psycopg.connect(self.config.aptem_database_url,
                                                                  prepare_threshold=None, **DB_CONNECT_KWARGS))
        with connect() as aptem:
            outcome = service.discover_day(connection, aptem, day, persist=True)
        if isinstance(registry, ConflictTolerantLectureRepository):
            outcome["registry_rows_locked"] = len(registry.locked)
        return outcome
