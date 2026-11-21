"""
Retry Teams transcript, end to end, on the isolated test database.

The reference case is 2026-11-17 "Marketing Impact and Planning" (94008498), seeded with its real
timings and synthetic text: Teams was asked three minutes after the scheduled end while the class
was still live and held only a 52-second pre-class test; the 2-hour transcript appeared minutes
later on the same call.

Real code throughout: the console's plan and queue, the durable job queue (030 + PROPOSED 031), the
job worker, the Publisher's lecture-scoped Teams transcript worker, the selector, the combiner, the
canonical parser, speakers, attendance, engagement, QA, rendering, the legacy writer and the real
PipelineOrchestrator narrowed to the lecture. Only what would leave the machine is a stub: Graph (a
fake gateway), the model (counted), the n8n precheck. Every connection rolls back.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.lecture_processing_jobs import (
    COMPLETED,
    FAILED,
    QUEUED,
    REASON_TEAMS_TRANSCRIPT_RETRY,
    LectureProcessingJobRepository,
)
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.pipeline_runs import PipelineRunRepository
from app.db.repositories.ready_lectures import ReadyLectureRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository, TranscriptSelectionRunRepository,
)
from app.orchestration.lecture_jobs import LectureJobWorker
from app.orchestration.locks import LOCK_NAMESPACE, LectureLockManager, SchedulerCycleLock, lock_key
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import StageRunner
from app.orchestration.stages import ACQUIRE_TRANSCRIPT, COMPLETE, RUN_QA, SELECTION
from app.orchestration.state import (
    EMPTY_TRANSCRIPT_FOR_OCCURRENCE,
    TRANSCRIPT_LISTING_NOT_SETTLED,
    PipelineStateResolver,
)
from app.orchestration.teams_transcript_retry import (
    QA_ALREADY_COMPLETE,
    TEAMS_TRANSCRIPT_UNAVAILABLE,
    LockHeldByCaller,
    TeamsTranscriptRetry,
    lock_for_transaction,
)
from app.orchestration.transcript_recovery import ALREADY_QUEUED, QUEUE_UNAVAILABLE
from app.publisher.transcripts import TranscriptWorker
from app.transcripts.models import (
    TRANSCRIPTS_FOUND,
    TranscriptArtifact,
    TranscriptContent,
    TranscriptListing,
)
from app.transcripts.selection_service import TranscriptSelectionService
from tests.integration.seeding import TRAINER, graph_id, insert, webvtt
from tests.integration.test_lecture_processing_queue import _body, factory  # noqa: F401  (fixture)
from tools.integration_db_guard import assert_isolated
from tests.integration.test_manual_transcript_upload import (  # noqa: F401  (fixtures)
    CountingProvider,
    StubPreflight,
    _vtt,
    db,
    provider,
)

PROPOSED = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations_proposed"
MIGRATION_030 = PROPOSED / "030_create_lecture_processing_jobs.sql"
MIGRATION_031 = PROPOSED / "031_lecture_processing_jobs_teams_retry.sql"

# The reference lecture's real schedule: 11:00-13:00 Cairo.
DAY = date(2026, 11, 17)
START = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
TEST_PART_START = datetime(2026, 11, 17, 8, 46, 49, tzinfo=timezone.utc)      # 52.7 s pre-class test
FULL_PART_START = datetime(2026, 11, 17, 8, 59, 37, tzinfo=timezone.utc)      # the 2-hour class
FULL_PART_END = datetime(2026, 11, 17, 11, 4, 27, tzinfo=timezone.utc)
EARLY_LISTING = END + timedelta(minutes=3)                                    # 11:03 UTC, still live


@pytest.fixture
def queue(db):
    """Migrations 030 and 031, applied inside the test's transaction (rolled back with it)."""
    db.execute(_body(MIGRATION_030))
    db.execute(_body(MIGRATION_031))
    return LectureProcessingJobRepository()


class SharedConnection:
    """
    The Publisher's worker opens its own autocommit connection in production. Here it is handed
    the test's connection: setting autocommit and closing are no-ops, so its short transactions
    become savepoints and everything still rolls back with the test.
    """

    def __init__(self, connection):
        self._connection = connection

    @property
    def autocommit(self):
        return True

    @autocommit.setter
    def autocommit(self, _value):
        pass

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


class FakeTeams:
    """Graph for the reference meeting. `full` decides whether the 2-hour transcript is listed."""

    def __init__(self, lecture, *, full: bool):
        self.lecture = lecture
        self.full = full
        self.listings = 0
        self.fetches = []

    def list_transcripts(self, user, meeting_id):
        self.listings += 1
        items = [(self.lecture["test_transcript_id"], TEST_PART_START, TEST_PART_START + timedelta(seconds=53))]
        if self.full:
            items.append((self.lecture["full_transcript_id"], FULL_PART_START, FULL_PART_END))
        return TranscriptListing(TRANSCRIPTS_FOUND, artifacts=[
            TranscriptArtifact(provider_transcript_id=tid, provider_created_at=start, provider_end_at=end,
                               provider_call_id="call-a6724baf", content_correlation_id=None,
                               provider_meeting_id=meeting_id, metadata={})
            for tid, start, end in items])

    def fetch_content(self, user, meeting_id, transcript_id):
        self.fetches.append(transcript_id)
        raw = _vtt() if transcript_id == self.lecture["full_transcript_id"] else _short_vtt()
        return TranscriptContent("CONTENT_STORED", raw=raw, content_format="text/vtt", speaker_attribution=True)


def _short_vtt() -> bytes:
    return webvtt([(0, 9_000, TRAINER, "Synthetic mic check.")]).encode("utf-8")


def _seed(db):
    """
    The reference lecture as production held it on 2026-11-18: the 52-second transcript stored,
    the lecture's only Teams listing made at 11:03 UTC, the selection COMBINE_FAILED /
    TRANSCRIPT_UNUSABLE, re-saved after the 12 h grace.
    """
    lecture_id = uuid.uuid4()
    meeting_id = f"MTG-TEAMS-RETRY-{uuid.uuid4().hex[:6]}"
    subject = "Synthetic Marketing Impact"
    insert(db, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn="calendar@example.invalid",
           calendar_event_id=f"evt-{lecture_id}", join_url=f"https://example.invalid/{lecture_id}",
           subject=subject, normalized_subject=subject.lower(), module=subject,
           scheduled_start=START, scheduled_end=END, session_date=DAY,
           calendar_mapping_status="RESOLVED", group_match_status="MATCHED", discovery_status="READY",
           meeting_id=meeting_id, downstream_ready=True, meeting_lookup_user_id="organizer",
           meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
    for student in range(2):
        insert(db, "kbc_users_data", ID=str(771000 + student), FullName=f"Synthetic Student {student}",
               Group=subject, **{"Program-Status": "Active"})
    lecture = {"lecture_id": str(lecture_id), "meeting_id": meeting_id, "subject": subject,
               "test_transcript_id": graph_id(9101), "full_transcript_id": graph_id(9102)}
    # What the Publisher's early listing stored: only the test transcript, with its content.
    early = TranscriptWorker(connect=lambda: SharedConnection(db), gateway=FakeTeams(lecture, full=False),
                             clock=lambda: EARLY_LISTING)
    assert early.run(lecture["lecture_id"]).reason == "TRANSCRIPT_UNUSABLE"
    db.execute("UPDATE public.lecture_transcript_candidates SET last_seen_at = %s WHERE lecture_id = %s",
               (EARLY_LISTING, lecture["lecture_id"]))
    # The nightly re-selection after the grace (DB only), exactly as on 2026-11-18 19:00 UTC.
    TranscriptSelectionService(
        lecture_repository=ReadyLectureRepository(), selection_repository=TranscriptSelectionRepository(),
        run_repository=TranscriptSelectionRunRepository()).select_day(db, DAY, lecture_ids=[lecture["lecture_id"]])
    return lecture


def _resolver():
    return PipelineStateResolver(legacy_observations=LegacyObservationRepository())


def _service(db, teams: FakeTeams | None = None, *, teams_locks=None, lock_lecture=None):
    """
    build_teams_transcript_retry(worker=True), with the stubs and the real locks: the Teams fetch
    gets LockHeldByCaller, the job takes the lecture's transaction lock. `teams_locks` and
    `lock_lecture` override them to reproduce the wiring production ran on 2026-11-20.
    """
    settings = dataclasses.replace(Settings.from_environment(), qa_model_api_key="not-a-real-key",
                                   qa_model_base_url="https://provider.invalid/v1")
    resolver = _resolver()

    def orchestrator_factory(lecture_id):
        return PipelineOrchestrator(
            resolver=resolver,
            runner=StageRunner(settings=settings, allow_graph=False, allow_provider=True,
                               lecture_scope=[lecture_id]),
            run_repository=PipelineRunRepository(), preflight=StubPreflight(),
            lock_manager=LectureLockManager(), cycle_lock=SchedulerCycleLock())

    fetch = None
    if teams is not None:
        fetch = TranscriptWorker(connect=lambda: SharedConnection(db), gateway=teams,
                                 lock_manager=teams_locks or LockHeldByCaller()).run
    return TeamsTranscriptRetry(resolver=resolver, orchestrator_factory=orchestrator_factory,
                                fetch_transcript=fetch, lock_lecture=lock_lecture)


def _worker(service, factory):
    return LectureJobWorker(recovery=None, recoveries={REASON_TEAMS_TRANSCRIPT_RETRY: service},
                            connection_factory=factory)


def _state(db, lecture):
    return _resolver().for_lecture(db, lecture["lecture_id"])


def _qa_rows(db, lecture):
    return db.execute("SELECT qa_status FROM public.lecture_qa_evaluations WHERE lecture_id = %s",
                      (lecture["lecture_id"],)).fetchall()


# =================================================================================================

def test_the_reference_lecture_is_offered_the_retry_and_the_permanent_fix_holds_it_open(db, queue):
    lecture = _seed(db)
    state = _state(db, lecture)
    # The permanent fix: an unusable transcript seen before the meeting settled does not close it.
    assert state["not_delivered"] is False
    assert state["stages"][SELECTION]["action"] == ACQUIRE_TRANSCRIPT
    assert state["stages"][SELECTION]["reason"] == TRANSCRIPT_LISTING_NOT_SETTLED

    plan = _service(db).plan(db, lecture["lecture_id"])
    assert plan["available"] is True, plan
    assert plan["problem"] == TRANSCRIPT_LISTING_NOT_SETTLED
    assert plan["cost"]["calls_microsoft_graph"] is True
    assert plan["cost"]["affects_other_lectures_that_day"] is False


def test_retry_recovers_the_lecture_once_and_a_second_click_buys_nothing(db, queue, factory, provider):
    lecture = _seed(db)
    teams = FakeTeams(lecture, full=True)
    service = _service(db, teams)

    first = service.queue(db, lecture["lecture_id"], requested_by="ops.user")
    again = service.queue(db, lecture["lecture_id"], requested_by="ops.user")
    assert first["queued"] and first["created"]
    assert again["queued"] and not again["created"] and again["reason"] == ALREADY_QUEUED, again
    assert again["job"]["job_id"] == first["job"]["job_id"]                  # one job, never two
    assert first["job"]["reason"] == REASON_TEAMS_TRANSCRIPT_RETRY and first["job"]["status"] == QUEUED

    settled = _worker(service, factory).run_next()

    assert settled["status"] == COMPLETED, settled
    assert teams.listings == 1                                               # Teams asked once
    assert teams.fetches == [lecture["full_transcript_id"]]                  # stored test part reused
    outcome = settled["outcome"]
    assert outcome["teams"]["kind"] == "READY"
    assert RUN_QA in outcome["actions"] and ACQUIRE_TRANSCRIPT not in outcome["actions"]
    assert provider.calls_total == 1 and outcome["provider_calls"] == 1
    state = _state(db, lecture)
    assert state["not_delivered"] is False
    assert state["stages"][SELECTION]["state"] == COMPLETE
    assert state["stages"]["QA_EVALUATION"]["state"] == COMPLETE
    assert _qa_rows(db, lecture) == [("COMPLETED",)]

    # The results are protected: the retry is no longer offered, and nothing is queued or bought.
    after = service.queue(db, lecture["lecture_id"], requested_by="ops.user")
    assert after["queued"] is False and after["reason"] == QA_ALREADY_COMPLETE
    assert _worker(service, factory).run_next() is None
    assert provider.calls_total == 1 and teams.listings == 1


def test_teams_still_without_a_usable_transcript_fails_clearly_and_closes_on_real_evidence(
        db, queue, factory, provider):
    lecture = _seed(db)
    teams = FakeTeams(lecture, full=False)
    service = _service(db, teams)
    service.queue(db, lecture["lecture_id"], requested_by="ops.user")

    settled = _worker(service, factory).run_next()

    assert settled["status"] == FAILED and settled["error_code"] == TEAMS_TRANSCRIPT_UNAVAILABLE
    assert settled["outcome"]["teams"]["reason"] == "TRANSCRIPT_UNUSABLE"
    assert teams.listings == 1 and provider.calls_total == 0
    job = LectureProcessingJobRepository().latest_for_lecture(db, lecture["lecture_id"])
    assert "Upload transcript" in job["last_error_message"]
    assert _qa_rows(db, lecture) == []
    # In production the Teams worker commits on its own connection, so its answer outlives the job
    # worker's rollback of the refused run. Here they share one connection, so that answer is taken
    # again directly: Teams has now been asked after the meeting settled, and "not delivered"
    # rests on that evidence (the permanent fix's rule, unchanged).
    TranscriptWorker(connect=lambda: SharedConnection(db), gateway=teams).run(lecture["lecture_id"])
    state = _state(db, lecture)
    assert state["not_delivered_reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE
    assert _service(db).plan(db, lecture["lecture_id"])["available"] is True   # still offered: Teams may change


def test_before_migration_031_the_console_queues_nothing(db):
    db.execute(_body(MIGRATION_030))
    lecture = _seed(db)
    result = _service(db).queue(db, lecture["lecture_id"], requested_by="ops.user")
    assert result["queued"] is False and result["reason"] == QUEUE_UNAVAILABLE
    assert db.execute("SELECT count(*) FROM public.lecture_processing_jobs").fetchone()[0] == 0


def test_031_rollback_refuses_while_teams_retry_jobs_exist(db, queue):
    lecture = _seed(db)
    _service(db).queue(db, lecture["lecture_id"], requested_by="ops.user")
    rollback = (PROPOSED / "031_lecture_processing_jobs_teams_retry.rollback.sql")
    db.execute("SAVEPOINT rollback_031")
    with pytest.raises(Exception, match="031 rollback refused"):
        db.execute(_body(rollback))
    db.execute("ROLLBACK TO SAVEPOINT rollback_031")


# =================================================================================================
# The lecture lock (production job 0f25407b, 2026-11-20). Real second sessions stand in for the
# pooled backends: production's lock was taken on one backend, its unlock ran on another, and the
# lock stayed on the first (pid 1099, idle) holding the lecture.

@pytest.fixture
def sessions():
    """Extra real sessions on the approved test database, each closed (locks released) at the end."""
    opened = []

    def open_session(*, autocommit=True):
        connection = psycopg.connect(Settings.from_environment().database_url, autocommit=autocommit)
        assert_isolated(connection)
        opened.append(connection)
        return connection
    yield open_session
    for connection in opened:
        connection.close()


class PooledLocks:
    """
    The pooler's effect on the Publisher worker's session lock: the lock lands on one backend and
    the unlock on another, which holds nothing - so the lock stays where it was taken.
    """

    def __init__(self, lock_backend, unlock_backend):
        self.lock_backend, self.unlock_backend = lock_backend, unlock_backend

    def try_acquire(self, _connection, lecture_id):
        return LectureLockManager().try_acquire(self.lock_backend, lecture_id)

    def release(self, _connection, lecture_id):
        return LectureLockManager().release(self.unlock_backend, lecture_id)


def _holders(session, lecture):
    return session.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND granted",
        (LOCK_NAMESPACE, lock_key(lecture["lecture_id"]) & 0xFFFFFFFF)).fetchall()


def _job(db, lecture):
    return LectureProcessingJobRepository().latest_for_lecture(db, lecture["lecture_id"])


def _make_due(db, job_id):
    db.execute("UPDATE public.lecture_processing_jobs SET next_attempt_after = now() WHERE job_id = %s",
               (job_id,))


def test_production_reproduced_a_leaked_lock_stops_the_run_at_attendance_and_the_job_now_waits(
        db, queue, factory, provider, sessions):
    """The wiring production ran: the Teams fetch's session lock leaks onto another backend."""
    lecture = _seed(db)
    backend_1099, other_backend = sessions(), sessions()
    teams = FakeTeams(lecture, full=True)
    service = _service(db, teams, teams_locks=PooledLocks(backend_1099, other_backend),
                       lock_lecture=lambda _c, _l: True)               # the job took no lock then
    service.queue(db, lecture["lecture_id"], requested_by="ops.user")

    settled = _worker(service, factory).run_next()

    outcome = settled["outcome"]
    assert outcome["item_status"] == "LOCKED" and outcome["final_blocking_stage"] == "ATTENDANCE"
    assert outcome["actions"] == ["BUILD_CANONICAL_CUES", "RESOLVE_SPEAKERS"]   # day-scoped: no lock
    assert len(_holders(other_backend, lecture)) == 1                          # still held, idle
    assert provider.calls_total == 0
    # Fixed: a temporary lock is no longer a failed job. It waits, without spending the attempt.
    assert settled["status"] == QUEUED and settled["error_code"] == "LECTURE_LOCKED"
    assert _job(db, lecture)["attempt_count"] == 0                             # refunded
    # Completed work is kept; the lecture resumes at attendance once the lock is gone.
    state = _state(db, lecture)
    assert state["stages"]["SPEAKERS"]["state"] == COMPLETE
    assert state["next_executable_action"] == "RESOLVE_ATTENDANCE"


def test_a_lock_held_by_another_backend_defers_the_job_before_teams_is_asked(
        db, queue, factory, provider, sessions):
    lecture = _seed(db)
    holder = sessions()
    assert LectureLockManager().try_acquire(holder, lecture["lecture_id"])
    teams = FakeTeams(lecture, full=True)
    service = _service(db, teams)
    job = service.queue(db, lecture["lecture_id"], requested_by="ops.user")["job"]

    settled = _worker(service, factory).run_next()

    assert settled["status"] == QUEUED and settled["error_code"] == "LECTURE_LOCKED"
    assert _job(db, lecture)["attempt_count"] == 0                             # refunded
    assert teams.listings == 0 and provider.calls_total == 0                   # nothing asked or bought

    # The holder lets go (its session ends, or an operator ends it): the next attempt finishes.
    holder.close()
    _make_due(db, job["job_id"])
    settled = _worker(service, factory).run_next()
    assert settled["status"] == COMPLETED, settled
    assert teams.listings == 1 and provider.calls_total == 1
    assert _qa_rows(db, lecture) == [("COMPLETED",)]


def test_the_fixed_run_leaves_no_session_lock_behind(db, queue, factory, provider, sessions):
    lecture = _seed(db)
    observer = sessions()
    service = _service(db, FakeTeams(lecture, full=True))
    service.queue(db, lecture["lecture_id"], requested_by="ops.user")

    assert _worker(service, factory).run_next()["status"] == COMPLETED

    # Only the job's TRANSACTION lock remains (the test's transaction is still open), held by the
    # job's own backend; no session lock exists that a commit would leave behind.
    assert _holders(observer, lecture) == [(db.info.backend_pid,)]
    assert db.execute("SELECT pg_advisory_unlock(%s, %s)",
                      (LOCK_NAMESPACE, lock_key(lecture["lecture_id"]))).fetchone()[0] is False


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_the_job_lock_ends_with_its_transaction_whatever_the_outcome(sessions, end):
    lecture = {"lecture_id": str(uuid.uuid4())}
    job, other = sessions(autocommit=False), sessions()
    assert lock_for_transaction(job, lecture["lecture_id"]) is True
    assert LectureLockManager().try_acquire(other, lecture["lecture_id"]) is False   # excluded
    # The orchestrator's per-stage lock on the job's own connection is the same session's: granted.
    with LectureLockManager().hold(job, lecture["lecture_id"]):
        pass
    getattr(job, end)()
    assert _holders(other, lecture) == []                                        # nothing leaked
    assert LectureLockManager().try_acquire(other, lecture["lecture_id"]) is True
    assert LectureLockManager().release(other, lecture["lecture_id"]) is True
