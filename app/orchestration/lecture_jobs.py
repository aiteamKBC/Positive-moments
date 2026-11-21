"""
The background worker for durable per-lecture processing jobs (PROPOSED migration 030).

WHERE IT RUNS
-------------
Inside the EXISTING positive-moments-backfill service (`python -m app.cli.main backfill-runner`):
its poll loop drains due lecture jobs before it looks for a backfill run, and again at every
backfill day boundary (app/orchestration/backfill.py). That service already has what this work
needs - a write connection, the QA provider configuration, systemd restart, SIGTERM handling -
and adds no new process, timer or external service.

WHAT ONE JOB DOES
-----------------
Exactly what `process-manual-transcript` does on the server: ManualTranscriptRecovery.process,
the real PipelineOrchestrator.run_window narrowed to the one lecture (lecture_scope), with the QA
provider allowed. So:
  * it resumes from the earliest unfinished stage; COMPLETE stages are never re-run;
  * the day-wide Graph transcript sweep is refused in lecture scope, so the stored manual
    transcript is never re-fetched from the failing Graph endpoint;
  * no other lecture is touched; the lecture's age does not matter (no lookback window);
  * it holds the scheduler cycle lock and the lecture lock for its duration, like any cycle.

DUPLICATE PAID QA
-----------------
  * one active job per lecture (partial unique index) - repeated clicks queue nothing new;
  * claim FOR UPDATE SKIP LOCKED with a lease - two workers never hold one job;
  * the cycle lock - a second processing run cannot start while one is in flight;
  * stage resolution - once QA is COMPLETE the next attempt has nothing to buy;
  * at most max_attempts claims, each counted BEFORE the work starts.
A worker killed mid-evaluation loses that attempt's uncommitted work (the whole run is one
transaction, exactly as for the nightly cycle) and the job is retried after its lease expires,
within max_attempts.

NIGHTLY CYCLE
-------------
A job holds the cycle lock, and a nightly firing that finds it held stands down for that firing.
So no job is STARTED within `quiet_minutes` before a scheduled firing (SCHEDULER_CRON, Africa/Cairo),
and a job that finds the cycle running is deferred without spending an attempt.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta

from app.common.errors import PlatformError
from app.db.repositories.lecture_processing_jobs import (
    REASON_MANUAL_TRANSCRIPT_UPLOAD,
    LectureProcessingJobRepository,
)
from app.orchestration.locks import LectureBusy, SchedulerCycleBusy
from app.orchestration.runner import PROVIDER_ACTIONS
from app.orchestration.stages import COMPLETE, ITEM_LOCKED, NOT_APPLICABLE
from app.orchestration.transcript_recovery import NOTHING_TO_PROCESS, ProcessingRefused

LECTURE_JOB_WORKER_VERSION = "lecture_job_worker_v1"

# Longer than one run can take: QA is at most 3 x 300 s plus waits; the rest is seconds. A lease
# that expires while the worker still runs would only let a second worker try - and the cycle
# lock would turn it away.
LEASE_SECONDS = 45 * 60
# Deferred because the nightly cycle (or another processing run) held the lock.
BUSY_RETRY_SECONDS = 10 * 60
# Back-off after a failed attempt: attempt n waits n x this.
RETRY_BACKOFF_SECONDS = 15 * 60
# No job starts this close before a scheduled nightly firing.
QUIET_MINUTES_BEFORE_CYCLE = 20

ENTRY_POINT = "QUEUE"
QA_NOT_COMPLETED = "QA_NOT_COMPLETED"
QA_SETTLED = (COMPLETE, NOT_APPLICABLE)
ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
UNKNOWN_JOB_REASON = "UNKNOWN_JOB_REASON"


def nightly_quiet_window(*, quiet_minutes: int = QUIET_MINUTES_BEFORE_CYCLE, now=None):
    """
    A callable answering "is a nightly firing due within `quiet_minutes`?" from the scheduler's
    own configuration (SCHEDULER_CRON in SCHEDULER_TIMEZONE). If the configuration cannot be
    read, the answer is always False: the cycle lock still prevents any overlap.
    """
    from app.orchestration.daemon import next_fire_time
    from app.orchestration.scheduler import SchedulerConfig

    try:
        config = SchedulerConfig.from_environment()
        zone, cron = config.zone, config.cron
    except Exception:                                            # noqa: BLE001
        logging.getLogger(__name__).warning("scheduler configuration unreadable; no quiet window")
        return lambda: False
    clock = now or (lambda: datetime.now(zone))

    def in_quiet_window() -> bool:
        current = clock()
        return next_fire_time(cron, after=current) - current <= timedelta(minutes=quiet_minutes)
    return in_quiet_window


class LectureJobWorker:
    def __init__(self, *, recovery, connection_factory, repository=None, in_quiet_window=None,
                 worker_id: str | None = None, lease_seconds: int = LEASE_SECONDS,
                 busy_retry_seconds: int = BUSY_RETRY_SECONDS,
                 retry_backoff_seconds: int = RETRY_BACKOFF_SECONDS, recoveries=None):
        self.recovery = recovery                    # ManualTranscriptRecovery, allow_provider=True
        # Which service processes a job, by the job's reason. A manual upload is the default; a
        # Teams transcript retry (app/orchestration/teams_transcript_retry.py) is the other.
        self.recoveries = {REASON_MANUAL_TRANSCRIPT_UPLOAD: recovery, **(recoveries or {})}
        self.connection_factory = connection_factory
        self.repository = repository or LectureProcessingJobRepository()
        self.in_quiet_window = in_quiet_window or (lambda: False)
        self.worker_id = worker_id or f"{LECTURE_JOB_WORKER_VERSION}@{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.busy_retry_seconds = busy_retry_seconds
        self.retry_backoff_seconds = retry_backoff_seconds
        self._available: bool | None = None
        self.log = logging.getLogger(__name__)

    # -- the loop's entry point ------------------------------------------------------------------
    def drain(self, *, should_stop=lambda: False, max_jobs: int | None = None) -> int:
        """Run due jobs one at a time until none is due. Never raises: the caller is a poll loop."""
        handled = 0
        while not should_stop() and (max_jobs is None or handled < max_jobs):
            try:
                result = self.run_next()
            except Exception as exc:                             # noqa: BLE001
                self.log.exception("lecture job worker error", extra={"fields": {
                    "service": "lecture_job_worker", "error": type(exc).__name__}})
                return handled
            if result is None:
                return handled
            handled += 1
        return handled

    def run_next(self) -> dict | None:
        """Claim one due job and process it. None when there is nothing to do now."""
        if not self._queue_installed():
            return None
        if self.in_quiet_window():
            # Not claimed at all: the job keeps its place and its attempts.
            return None
        with self.connection_factory() as connection:
            job = self.repository.claim(connection, worker=self.worker_id,
                                        lease_seconds=self.lease_seconds)
            connection.commit()
        if job is None:
            return None
        self.log.info("lecture job claimed", extra={"fields": {
            "service": "lecture_job_worker", "job_id": job["job_id"], "lecture_id": job["lecture_id"],
            "attempt": job["attempt_count"], "max_attempts": job["max_attempts"]}})
        if job["attempt_count"] > job["max_attempts"]:
            # Only reachable through an expired lease: the last attempt's worker died.
            return self._settle(job, "fail", error_code=ATTEMPTS_EXHAUSTED,
                                error_message="the last attempt did not finish (worker stopped)")
        return self._process(job)

    # -- one job -----------------------------------------------------------------------------------
    def _process(self, job: dict) -> dict:
        lecture_id = job["lecture_id"]
        recovery = self.recoveries.get(job.get("reason") or REASON_MANUAL_TRANSCRIPT_UPLOAD)
        if recovery is None:
            return self._settle(job, "fail", error_code=UNKNOWN_JOB_REASON,
                                error_message=f"no processor for job reason {job.get('reason')}")
        try:
            with self.connection_factory() as connection:
                try:
                    result = recovery.process(connection, lecture_id, entry_point=ENTRY_POINT)
                except BaseException:
                    connection.rollback()
                    raise
                connection.commit()
        except ProcessingRefused as exc:
            if exc.code == NOTHING_TO_PROCESS and exc.plan.get("qa_state") in QA_SETTLED:
                # Already evaluated (an earlier attempt, the nightly cycle, the CLI): done.
                return self._settle(job, "complete", outcome={"final_action": exc.plan.get("next_action"),
                                                              "qa_state": exc.plan.get("qa_state"),
                                                              "reason": exc.code, "provider_calls": 0})
            # Nothing a retry can change (no upload, parts missing, manual review).
            # Or nothing can run before QA (a stage is waiting or needs review).
            outcome = {"final_action": exc.plan.get("next_action"),
                       "qa_state": exc.plan.get("qa_state"), "reason": exc.code}
            if exc.plan.get("teams"):
                outcome["teams"] = exc.plan["teams"]
            return self._settle(job, "fail", error_code=exc.code, error_message=exc.detail,
                                outcome=outcome)
        except (SchedulerCycleBusy, LectureBusy) as exc:
            code = "SCHEDULER_CYCLE_RUNNING" if isinstance(exc, SchedulerCycleBusy) else "LECTURE_LOCKED"
            return self._settle(job, "defer", error_code=code, delay=self.busy_retry_seconds,
                                refund=True)
        except PlatformError as exc:
            return self._retry_or_fail(job, exc.code, str(exc))
        except Exception as exc:                                 # noqa: BLE001
            self.log.exception("lecture job failed", extra={"fields": {
                "service": "lecture_job_worker", "job_id": job["job_id"], "lecture_id": lecture_id}})
            return self._retry_or_fail(job, type(exc).__name__, str(exc))

        processing = result["processing"]
        outcome = {key: processing.get(key) for key in (
            "status", "item_status", "final_action", "final_blocking_stage", "qa_state", "error_code",
            "provider_calls", "graph_calls", "actions")}
        if processing.get("teams"):
            outcome["teams"] = processing["teams"]
        run_id = processing.get("run_id")
        if processing.get("qa_state") in QA_SETTLED:
            # The job's purpose is met. Later stages that wait on an external source (attendance,
            # a recording) are the nightly cycle's and the console's, as for any lecture.
            return self._settle(job, "complete", outcome=outcome, pipeline_run_id=run_id)
        if processing.get("item_status") == ITEM_LOCKED:
            # Another process held the lecture when a stage needed it (the orchestrator records
            # LOCKED, retryable). Temporary, like LectureBusy above: what ran is kept, the job
            # waits its turn without spending an attempt.
            return self._settle(job, "defer", error_code="LECTURE_LOCKED",
                                error_message=(f"lecture busy at {processing.get('final_blocking_stage')}: "
                                               f"{processing.get('final_action')}"),
                                outcome=outcome, pipeline_run_id=run_id,
                                delay=self.busy_retry_seconds, refund=True)
        if processing.get("final_action") in PROVIDER_ACTIONS:
            # The run was committed, but the paid step is still ahead: the provider failed or its
            # circuit is open. Everything before it is kept; only QA is attempted again.
            return self._retry_or_fail(job, processing.get("error_code") or QA_NOT_COMPLETED,
                                       "the quality evaluation did not complete", outcome=outcome,
                                       pipeline_run_id=run_id)
        # QA cannot run without a person or another source (review required, a waiting stage).
        return self._settle(job, "fail", error_code=f"QA_{processing.get('qa_state') or 'NOT_REACHED'}",
                            error_message=(f"stopped at {processing.get('final_blocking_stage')}: "
                                           f"{processing.get('final_action')}"),
                            outcome=outcome, pipeline_run_id=run_id)

    def _retry_or_fail(self, job, code, message, *, outcome=None, pipeline_run_id=None) -> dict:
        if job["attempt_count"] < job["max_attempts"]:
            return self._settle(job, "defer", error_code=code, error_message=message, outcome=outcome,
                                pipeline_run_id=pipeline_run_id,
                                delay=self.retry_backoff_seconds * job["attempt_count"])
        return self._settle(job, "fail", error_code=code, error_message=message, outcome=outcome,
                            pipeline_run_id=pipeline_run_id)

    def _settle(self, job, verdict, *, error_code=None, error_message=None, outcome=None,
                pipeline_run_id=None, delay=0, refund=False) -> dict:
        with self.connection_factory() as connection:
            if verdict == "complete":
                self.repository.complete(connection, job["job_id"], outcome=outcome or {},
                                         pipeline_run_id=pipeline_run_id)
            elif verdict == "fail":
                self.repository.fail(connection, job["job_id"], error_code=error_code,
                                     error_message=error_message, outcome=outcome,
                                     pipeline_run_id=pipeline_run_id)
            else:
                self.repository.retry_later(connection, job["job_id"], delay_seconds=delay,
                                            error_code=error_code, error_message=error_message,
                                            outcome=outcome, pipeline_run_id=pipeline_run_id,
                                            refund_attempt=refund)
            connection.commit()
        status = {"complete": "COMPLETED", "fail": "FAILED", "defer": "QUEUED"}[verdict]
        self.log.info("lecture job settled", extra={"fields": {
            "service": "lecture_job_worker", "job_id": job["job_id"], "lecture_id": job["lecture_id"],
            "status": status, "error_code": error_code, "attempt": job["attempt_count"]}})
        return {"job_id": job["job_id"], "lecture_id": job["lecture_id"], "status": status,
                "error_code": error_code, "outcome": outcome}

    def _queue_installed(self) -> bool:
        """Before migration 030 the worker is idle; once the table exists it is remembered."""
        if self._available:
            return True
        try:
            with self.connection_factory() as connection:
                found = self.repository.available(connection)
                connection.rollback()
        except Exception:                                        # noqa: BLE001
            self.log.exception("lecture job queue check failed")
            return False
        if not found and self._available is None:
            self.log.warning("lecture_processing_jobs is absent (migration 030); "
                             "the lecture job worker is idle")
        self._available = found or False
        return found
