"""
Automatic QA recovery after a manual transcript upload: the durable per-lecture queue (PROPOSED
migration 030) and its worker (app/orchestration/lecture_jobs.py), against the isolated test
database only. No Graph, no real model: the provider is the counting stub of the manual upload
suite, and every model call is counted.

The worker opens its own connections in production. Here its connection factory hands it the
test's own connection, and a COMMIT is a released savepoint - so the worker's transaction
boundaries are exercised and the test still rolls everything back.
"""
from __future__ import annotations

import dataclasses
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.lecture_processing_jobs import (
    COMPLETED,
    FAILED,
    QUEUED,
    RUNNING,
    LectureProcessingJobRepository,
)
from app.orchestration import lecture_jobs
from app.orchestration import runner as runner_module
from app.orchestration.lecture_jobs import ATTEMPTS_EXHAUSTED, LectureJobWorker
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.pipeline_runs import PipelineRunRepository
from app.orchestration.locks import LectureLockManager, SchedulerCycleLock
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import StageRunner
from app.orchestration.stages import ACQUIRE_TRANSCRIPT, RUN_QA
from app.orchestration.state import PipelineStateResolver
from app.orchestration.transcript_recovery import (
    ALREADY_QUEUED,
    ManualTranscriptRecovery,
    NO_MANUAL_UPLOAD,
    NOTHING_TO_PROCESS,
    QUEUE_UNAVAILABLE,
    WAITING_FOR_TRANSCRIPT_PARTS,
)
from app.qa.provider import ProviderError
from app.transcripts.manual_upload import ManualTranscriptUploadService
from tests.integration.test_manual_transcript_upload import (  # noqa: F401  (fixtures)
    CountingProvider,
    StubPreflight,
    _documents,
    _footprint,
    _lecture,
    _recovery,
    _store_graph_content,
    _upload,
    _vtt,
    db,
    provider,
)
from tools.integration_db_guard import assert_isolated

MIGRATION = (Path(__file__).resolve().parents[2] / "app" / "db" / "migrations_proposed"
             / "030_create_lecture_processing_jobs.sql")


def _body(path: Path) -> str:
    """The migration without its own BEGIN/COMMIT, to run inside the test transaction."""
    return re.sub(r"^\s*(BEGIN|COMMIT);\s*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)


@pytest.fixture
def queue(db):
    """Migration 030, applied inside the test's transaction (rolled back with it)."""
    db.execute(_body(MIGRATION))
    return LectureProcessingJobRepository()


@pytest.fixture
def factory(db):
    """
    The worker's connection factory: the test connection, with COMMIT as a released savepoint.
    The patch is undone when this fixture ends - before `db` rolls the test back for real.
    """
    real_commit, real_rollback = psycopg.Connection.commit, psycopg.Connection.rollback

    def commit(self):
        if self is not db:                       # any other connection keeps real semantics
            return real_commit(self)
        self.execute("RELEASE SAVEPOINT worker_tx")
        self.execute("SAVEPOINT worker_tx")

    def rollback(self):
        if self is not db:
            return real_rollback(self)
        self.execute("ROLLBACK TO SAVEPOINT worker_tx")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(psycopg.Connection, "commit", commit)
        patch.setattr(psycopg.Connection, "rollback", rollback)
        yield _factory(db)


def _factory(db):
    @contextmanager
    def connection():
        db.execute("SAVEPOINT worker_tx")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT worker_tx")
            db.execute("RELEASE SAVEPOINT worker_tx")
            raise
        db.execute("RELEASE SAVEPOINT worker_tx")
    return connection


def _production_recovery():
    """
    What build_manual_transcript_service(allow_provider=True) gives the worker: the REAL cycle lock
    and lecture locks (the suite's own helper uses the no-op ones), the stub provider, no Graph.
    """
    settings = dataclasses.replace(Settings.from_environment(), qa_model_api_key="not-a-real-key",
                                   qa_model_base_url="https://provider.invalid/v1")
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository())

    def orchestrator_factory(lecture_id):
        return PipelineOrchestrator(
            resolver=resolver,
            runner=StageRunner(settings=settings, allow_graph=False, allow_provider=True,
                               lecture_scope=[lecture_id]),
            run_repository=PipelineRunRepository(), preflight=StubPreflight(),
            lock_manager=LectureLockManager(), cycle_lock=SchedulerCycleLock())

    return ManualTranscriptRecovery(uploads=ManualTranscriptUploadService(), resolver=resolver,
                                    orchestrator_factory=orchestrator_factory, allow_provider=True)


def _worker(factory, *, quiet=False, **kwargs):
    return LectureJobWorker(recovery=_production_recovery(), connection_factory=factory,
                            in_quiet_window=lambda: quiet, worker_id="test-worker", **kwargs)


def _console():
    """The web console's service: provider OFF; it only ever queues."""
    return _recovery(allow_provider=False)


def _job(db, job_id):
    return LectureProcessingJobRepository().get(db, job_id)


def _jobs(db, lecture_id):
    return db.execute("SELECT status FROM public.lecture_processing_jobs WHERE lecture_id = %s"
                      " ORDER BY created_at", (lecture_id,)).fetchall()


def _qa_rows(db, lecture_id):
    return db.execute("SELECT qa_status FROM public.lecture_qa_evaluations WHERE lecture_id = %s",
                      (lecture_id,)).fetchall()


def _upload_and_queue(db, lecture, raw=None, user="ops.user"):
    """The store route: commit the upload and queue processing in one transaction."""
    result = _upload(db, lecture, raw or _vtt(), user=user)
    result["queue"] = _console().queue_processing(db, lecture["lecture_id"], requested_by=user)
    return result


# =================================================================================================
# 1. a successful upload creates exactly one processable queue entry
# =================================================================================================

def test_a_successful_upload_queues_one_job_and_repeats_queue_nothing_new(db, queue, provider):
    lecture = _lecture(db, 101)
    result = _upload_and_queue(db, lecture)
    queued = result["queue"]
    assert queued["queued"] is True and queued["created"] is True
    job = queued["job"]
    assert (job["status"], job["lecture_id"], job["reason"], job["attempt_count"]) == \
        (QUEUED, lecture["lecture_id"], "MANUAL_TRANSCRIPT_UPLOAD", 0)
    assert job["requested_by"] == "ops.user"
    # Repeated clicks / a re-upload of the same bytes: the one active job, never a second.
    again = _console().queue_processing(db, lecture["lecture_id"], requested_by="someone.else")
    assert again["queued"] is True and again["created"] is False and again["reason"] == ALREADY_QUEUED
    assert again["job"]["job_id"] == job["job_id"]
    assert _upload(db, lecture, _vtt())["status"] == "ALREADY_STORED"
    assert _console().queue_processing(db, lecture["lecture_id"])["job"]["job_id"] == job["job_id"]
    assert _jobs(db, lecture["lecture_id"]) == [(QUEUED,)]
    # The console reports it, and offers no second queueing while it is active.
    status = _console().status(db, lecture["lecture_id"])
    assert status["job"]["job_id"] == job["job_id"]
    assert status["processing"]["available"] is False and status["processing"]["reason"] == ALREADY_QUEUED
    # Queueing ran nothing.
    assert provider.calls_total == 0 and _documents(db, lecture["lecture_id"]) == []


def test_the_unique_index_refuses_a_second_active_job_even_without_the_service(db, queue):
    lecture = _lecture(db, 102)
    first, created = queue.enqueue(db, lecture["lecture_id"])
    second, created_again = queue.enqueue(db, lecture["lecture_id"])
    assert created and not created_again and first["job_id"] == second["job_id"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO public.lecture_processing_jobs (job_id, lecture_id, reason)"
                       " VALUES (gen_random_uuid(), %s, 'MANUAL_TRANSCRIPT_UPLOAD')", (lecture["lecture_id"],))


def test_nothing_is_queued_when_there_is_nothing_to_process(db, queue):
    no_upload = _lecture(db, 103, status="DISCOVERED")
    _store_graph_content(db, no_upload["artifacts"][0]["artifact_id"], _vtt())
    assert _console().queue_processing(db, no_upload["lecture_id"])["reason"] == NO_MANUAL_UPLOAD
    half = _lecture(db, 104, parts=((0, 55), (60, 120)))
    _upload(db, half, _vtt(span_ms=3_300_000), artifact_id=half["artifacts"][0]["artifact_id"])
    assert _console().queue_processing(db, half["lecture_id"])["reason"] == WAITING_FOR_TRANSCRIPT_PARTS
    assert _jobs(db, no_upload["lecture_id"]) == [] and _jobs(db, half["lecture_id"]) == []


def test_before_migration_030_the_service_queues_nothing_and_the_worker_is_idle(db, factory):
    """The service level. The store ROUTE refuses before writing when 030 is missing (API tests)."""
    lecture = _lecture(db, 105)
    result = _upload_and_queue(db, lecture)
    assert result["stored"] is True
    assert result["queue"]["queued"] is False and result["queue"]["reason"] == QUEUE_UNAVAILABLE
    assert _console().status(db, lecture["lecture_id"])["job"] is None
    assert _worker(factory).run_next() is None          # the worker is idle, not failing


# =================================================================================================
# 2-3. the worker consumes it: the whole pipeline, QA included, for this lecture only, any age
# =================================================================================================

def test_the_worker_runs_the_lecture_to_qa_once_and_touches_nothing_else(db, queue, factory, provider):
    lecture = _lecture(db, 106)
    sibling = _lecture(db, 107, start=datetime(2031, 3, 4, 13, tzinfo=timezone.utc),
                       subject="Synthetic Sibling Lecture")
    sibling_before = _footprint(db, sibling["lecture_id"])
    job = _upload_and_queue(db, lecture)["queue"]["job"]

    settled = _worker(factory).run_next()

    assert settled["status"] == COMPLETED and settled["job_id"] == job["job_id"]
    done = _job(db, job["job_id"])
    assert (done["status"], done["attempt_count"], done["last_error_code"]) == (COMPLETED, 1, None)
    assert done["finished_at"] is not None and done["pipeline_run_id"]
    assert done["outcome"]["provider_calls"] == 1 and done["outcome"]["graph_calls"] == 0
    actions = done["outcome"]["actions"]
    for action in ("BUILD_CANONICAL_CUES", RUN_QA, "RENDER_QA"):
        assert action in actions, actions
    # The stored manual transcript was never re-fetched from Graph.
    assert ACQUIRE_TRANSCRIPT not in actions
    assert provider.calls_total == 1 and _qa_rows(db, lecture["lecture_id"]) == [("COMPLETED",)]
    # The upload's provenance records the background run.
    status = _console().status(db, lecture["lecture_id"])
    processing = status["uploads"][-1]["processing"]
    assert processing["entry_point"] == "QUEUE" and processing["run_id"] == done["pipeline_run_id"]
    # Only the intended lecture: the sibling on the same day is exactly as it was.
    assert _footprint(db, sibling["lecture_id"]) == sibling_before
    # Nothing left to do: the queue is empty.
    assert _worker(factory).run_next() is None


def test_a_lecture_far_outside_the_nightly_lookback_is_processed(db, queue, factory, provider):
    old_start = datetime(2026, 1, 12, 9, tzinfo=timezone.utc)      # ten months before "now"
    lecture = _lecture(db, 108, start=old_start)
    db.execute("UPDATE public.lecture_sessions SET session_date = %s WHERE lecture_id = %s",
               (old_start.date(), lecture["lecture_id"]))
    _upload_and_queue(db, lecture)
    settled = _worker(factory).run_next()
    assert settled["status"] == COMPLETED
    assert provider.calls_total == 1 and _qa_rows(db, lecture["lecture_id"]) == [("COMPLETED",)]


# =================================================================================================
# 4-5. an already-uploaded, QA-pending lecture is queued without a re-upload; stages are kept
# =================================================================================================

def test_a_qa_pending_lecture_is_queued_without_reupload_and_resumes_at_qa(db, queue, factory, provider):
    """Exactly the 13 Nov lecture: uploaded, deterministic stages done by the console, QA pending."""
    lecture = _lecture(db, 109)
    _upload(db, lecture, _vtt())
    console_run = _console().process(db, lecture["lecture_id"])      # the earlier console behaviour
    assert console_run["processing"]["final_action"] == RUN_QA and provider.calls_total == 0
    documents_before = _documents(db, lecture["lecture_id"])
    status = _console().status(db, lecture["lecture_id"])
    assert status["processing"]["available"] is True and status["processing"]["next_action"] == RUN_QA

    queued = _console().queue_processing(db, lecture["lecture_id"], requested_by="ops.user")
    assert queued["queued"] is True and queued["created"] is True

    settled = _worker(factory).run_next()

    assert settled["status"] == COMPLETED
    # Resumed at the earliest unfinished stage: QA first, nothing before it re-run.
    actions = settled["outcome"]["actions"]
    assert actions[0] == RUN_QA and "BUILD_CANONICAL_CUES" not in actions
    assert _documents(db, lecture["lecture_id"]) == documents_before
    assert provider.calls_total == 1


def test_a_completed_lecture_is_never_evaluated_again(db, queue, factory, provider):
    lecture = _lecture(db, 110)
    _upload_and_queue(db, lecture)
    assert _worker(factory).run_next()["status"] == COMPLETED
    evaluation = db.execute("SELECT evaluation_id, updated_at FROM public.lecture_qa_evaluations"
                            " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()
    # Another click finds nothing to process and queues nothing.
    again = _console().queue_processing(db, lecture["lecture_id"])
    assert again["queued"] is False and again["reason"] == NOTHING_TO_PROCESS
    # Even a job slipped in directly (or left over) is settled without buying a generation.
    queue.enqueue(db, lecture["lecture_id"])
    settled = _worker(factory).run_next()
    assert settled["status"] == COMPLETED and settled["outcome"]["reason"] == NOTHING_TO_PROCESS
    assert provider.calls_total == 1
    assert db.execute("SELECT evaluation_id, updated_at FROM public.lecture_qa_evaluations"
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall() == evaluation


# =================================================================================================
# 6. concurrency: one claim per job, the nightly cycle always wins
# =================================================================================================

def test_a_running_job_with_a_live_lease_is_never_claimed_twice(db, queue):
    lecture = _lecture(db, 111)
    _upload_and_queue(db, lecture)
    first = queue.claim(db, worker="worker-a", lease_seconds=600)
    assert first["status"] == RUNNING and first["attempt_count"] == 1
    assert queue.claim(db, worker="worker-b", lease_seconds=600) is None


def test_a_running_nightly_cycle_defers_the_job_without_spending_an_attempt(db, queue, factory, provider):
    lecture = _lecture(db, 112)
    job = _upload_and_queue(db, lecture)["queue"]["job"]
    url = Settings.from_environment().database_url
    with psycopg.connect(url, autocommit=True) as nightly:
        assert_isolated(nightly)
        assert SchedulerCycleLock().try_acquire(nightly)        # the 21:00 cycle is running
        settled = _worker(factory).run_next()
    assert settled["status"] == QUEUED and settled["error_code"] == "SCHEDULER_CYCLE_RUNNING"
    deferred = _job(db, job["job_id"])
    assert deferred["attempt_count"] == 0 and deferred["next_attempt_after"] > datetime.now(timezone.utc)
    assert provider.calls_total == 0 and _documents(db, lecture["lecture_id"]) == []
    # Not due yet: nothing is claimed until the deferral passes.
    assert _worker(factory).run_next() is None


def test_no_job_starts_in_the_quiet_window_before_a_nightly_firing(db, queue, factory, provider):
    lecture = _lecture(db, 113)
    job = _upload_and_queue(db, lecture)["queue"]["job"]
    assert _worker(factory, quiet=True).run_next() is None
    untouched = _job(db, job["job_id"])
    assert (untouched["status"], untouched["attempt_count"]) == (QUEUED, 0)
    assert provider.calls_total == 0


def test_the_quiet_window_follows_the_scheduler_cron_in_cairo(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setenv("SCHEDULER_CRON", "0 21,23 * * *")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Africa/Cairo")
    cairo = ZoneInfo("Africa/Cairo")
    at = lambda h, m: (lambda: datetime(2026, 11, 17, h, m, tzinfo=cairo))  # noqa: E731
    assert lecture_jobs.nightly_quiet_window(now=at(20, 45))() is True
    assert lecture_jobs.nightly_quiet_window(now=at(20, 30))() is False
    assert lecture_jobs.nightly_quiet_window(now=at(22, 50))() is True
    assert lecture_jobs.nightly_quiet_window(now=at(14, 0))() is False


# =================================================================================================
# 7. worker restart and retry
# =================================================================================================

def test_a_job_left_running_by_a_dead_worker_is_reclaimed_and_finished(db, queue, factory, provider):
    lecture = _lecture(db, 114)
    job = _upload_and_queue(db, lecture)["queue"]["job"]
    # A worker claimed it and died (SIGKILL, host restart): its transaction rolled back, the row
    # stayed RUNNING with a lease.
    queue.claim(db, worker="dead-worker", lease_seconds=3600)
    assert _worker(factory).run_next() is None                     # the lease is still live
    db.execute("UPDATE public.lecture_processing_jobs SET lease_expires_at = now() - interval '1 minute'"
               " WHERE job_id = %s", (job["job_id"],))
    settled = _worker(factory).run_next()
    assert settled["status"] == COMPLETED
    assert _job(db, job["job_id"])["attempt_count"] == 2 and provider.calls_total == 1


def test_a_job_whose_last_attempt_died_is_failed_not_retried_forever(db, queue, factory, provider):
    lecture = _lecture(db, 115)
    job = _upload_and_queue(db, lecture)["queue"]["job"]
    db.execute("UPDATE public.lecture_processing_jobs SET status = 'RUNNING', attempt_count = max_attempts,"
               " lease_expires_at = now() - interval '1 minute' WHERE job_id = %s", (job["job_id"],))
    settled = _worker(factory).run_next()
    assert settled["status"] == FAILED and settled["error_code"] == ATTEMPTS_EXHAUSTED
    assert provider.calls_total == 0
    # A failed job does not block a later request for the lecture.
    assert _console().queue_processing(db, lecture["lecture_id"])["created"] is True


class FailingProvider(CountingProvider):
    def complete_json(self, **kwargs):
        CountingProvider.calls_total += 1
        raise ProviderError("PROVIDER_HTTP_ERROR", "synthetic outage", http_status=503)


def test_a_failed_evaluation_is_retried_with_backoff_then_failed_and_earlier_stages_are_kept(
        db, queue, factory, provider, monkeypatch):
    monkeypatch.setattr(runner_module, "OpenAIChatProvider", FailingProvider)
    lecture = _lecture(db, 116)
    job = _upload_and_queue(db, lecture)["queue"]["job"]
    worker = _worker(factory)

    first = worker.run_next()

    retried = _job(db, job["job_id"])
    assert first["status"] == QUEUED and retried["status"] == QUEUED and retried["attempt_count"] == 1
    assert retried["last_error_code"] and retried["next_attempt_after"] > datetime.now(timezone.utc)
    # The deterministic stages of that attempt were committed and are kept.
    assert _documents(db, lecture["lecture_id"])[0][0] == "PARSED"
    documents = _documents(db, lecture["lecture_id"])
    calls_after_first = CountingProvider.calls_total
    assert calls_after_first >= 1

    for attempt in (2, 3):
        db.execute("UPDATE public.lecture_processing_jobs SET next_attempt_after = now()"
                   " WHERE job_id = %s", (job["job_id"],))
        worker.run_next()
        assert _job(db, job["job_id"])["attempt_count"] == attempt
    final = _job(db, job["job_id"])
    assert final["status"] == FAILED and final["last_error_code"]
    assert _documents(db, lecture["lecture_id"]) == documents        # never rebuilt
    # Bounded: no fourth attempt is ever claimed.
    db.execute("UPDATE public.lecture_processing_jobs SET next_attempt_after = now() WHERE job_id = %s",
               (job["job_id"],))
    calls = CountingProvider.calls_total
    assert worker.run_next() is None and CountingProvider.calls_total == calls


def test_jobs_are_listed_newest_first_with_their_lecture(db, queue):
    first, second = _lecture(db, 117), _lecture(db, 118, subject="Synthetic Second Lecture")
    earlier = _upload_and_queue(db, first)["queue"]["job"]
    # One test transaction gives both rows the same now(); in production they are separate requests.
    db.execute("UPDATE public.lecture_processing_jobs SET created_at = created_at - interval '1 minute'"
               " WHERE job_id = %s", (earlier["job_id"],))
    _upload_and_queue(db, second, _vtt(offset=4000))
    recent = queue.recent(db, limit=5)
    mine = [job for job in recent if job["lecture_id"] in (first["lecture_id"], second["lecture_id"])]
    assert [job["subject"] for job in mine] == ["Synthetic Second Lecture", "Synthetic Channel Lecture"]
    assert mine[0]["session_date"] == date(2031, 3, 4)
    assert queue.latest_for_lecture(db, first["lecture_id"])["status"] == QUEUED
    assert (datetime.now(timezone.utc) - mine[0]["created_at"]) < timedelta(minutes=5)
