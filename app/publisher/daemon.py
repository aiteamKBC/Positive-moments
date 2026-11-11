"""
The continuous publisher daemon: one supervised process, 24/7.

    scheduler thread   heartbeat -> final-attempt sweep -> due periodic tasks
                         DISCOVERY        every 5 min (planner tick: calendar, LMS, plan jobs)
                         AUTO_RELEASE     every 5 min, only when PUBLISHER_AUTO_RELEASE=true (auto_release.py)
                         RECONCILE_LIGHT  every 6 h
                         RECONCILE_FULL   daily at 05:00 Africa/Cairo (+ catch-up, Bunny scan, digest)
                         HISTORICAL_BACKFILL every minute, LAST in the tick: advances an operator-confirmed
                                          historical run one bounded step (backfill.py); idle without one
    N worker threads   claim one job (FOR UPDATE SKIP LOCKED + lease) -> its handler -> repeat

Periodic tasks are due by what bunny_publisher_schedule says ran last, so a daemon
restarted after a day offline runs a missed FULL reconciliation at once, and
its first discovery tick re-discovers every day it missed (bounded by the
lookback). Each task runs under a session advisory lock, so two daemons never
run the same task concurrently; jobs are leased, so two workers never run one job.

SHUTDOWN. SIGTERM / SIGINT set an event: idle waits return at once, every
thread finishes the unit it is in, and the process exits within the grace
period. A job still running at exit keeps its lease and is reclaimed after
the lease expires - every worker is idempotent and reconciles external
side effects before repeating them.

The nightly QA scheduler is a different service and is not touched.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import zlib
from datetime import datetime, timedelta, timezone

from app.common.time import CAIRO
from app.db.repositories.publisher_ops import PublisherScheduleRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository

log = logging.getLogger(__name__)
DAEMON_VERSION = "publisher_daemon_v1"
DISCOVERY, RECONCILE_LIGHT, RECONCILE_FULL = "DISCOVERY", "RECONCILE_LIGHT", "RECONCILE_FULL"
AUTO_RELEASE = "AUTO_RELEASE"                 # only when an AutoReleaser is configured (off by default)
HISTORICAL_BACKFILL = "HISTORICAL_BACKFILL"   # only when a HistoricalBackfill coordinator is configured
TASK_LOCK_NAMESPACE = 0x4B42_5054            # "KBPT"
TRACK_KINDS = ("ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING")
BUNNY_KINDS = ("IMPORT_VIDEO", "POLL_ENCODING", "PUBLISH_CAPTION")


def last_full_boundary(now: datetime, at) -> datetime:
    """The most recent `at` (Cairo wall clock) at or before now, in UTC."""
    local = now.astimezone(CAIRO)
    boundary = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary.astimezone(timezone.utc)


class PublisherDaemon:
    def __init__(self, *, config, connect, handlers: dict, planner, reconciler, queue=None, clock=None,
                 identity: str | None = None, auto_releaser=None, backfill=None):
        self.config = config
        self.connect = connect
        self.handlers = dict(handlers)
        self.planner = planner
        self.reconciler = reconciler
        self.auto_releaser = auto_releaser
        self.backfill = backfill
        self.queue = queue or PublisherQueueRepository()
        self.schedule = PublisherScheduleRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.identity = identity or f"{socket.gethostname()}:{os.getpid()}"
        self.stop_event = threading.Event()
        allowed = TRACK_KINDS + (BUNNY_KINDS if config.writes_bunny else ())
        self.kinds = [k for k in allowed if k in self.handlers]

    # -- lifecycle ------------------------------------------------------------------------
    def request_stop(self, *_):
        self.stop_event.set()

    def run_forever(self, *, max_iterations: int | None = None) -> dict:
        threads = [threading.Thread(target=self._worker_loop, args=(f"{self.identity}:w{n}",), daemon=True,
                                    name=f"publisher-worker-{n}") for n in range(self.config.workers)]
        for thread in threads:
            thread.start()
        iterations = 0
        try:
            while not self.stop_event.is_set():
                if max_iterations is not None and iterations >= max_iterations:
                    break
                iterations += 1
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the scheduler loop must survive
                    log.error("publisher scheduler tick failed", extra={"error_type": type(exc).__name__})
                self.stop_event.wait(self.config.poll_seconds)
        finally:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=self.config.shutdown_grace_seconds)
            self._heartbeat(f"{self.identity}:scheduler", None, stopped=True)
        return {"iterations": iterations, "identity": self.identity, "kinds": self.kinds}

    def run_once(self, *, max_jobs: int = 100) -> dict:
        """One scheduler tick, then jobs until the queue is idle (tests, `run --once`)."""
        ran = self.tick()
        processed = []
        for _ in range(max_jobs):
            kind = self.work_once(f"{self.identity}:w0")
            if kind == "IDLE":
                break
            processed.append(kind)
        return {"tasks": ran, "jobs": processed}

    # -- scheduler ----------------------------------------------------------------------------
    def tick(self) -> dict:
        now = self.clock()
        self._heartbeat(f"{self.identity}:scheduler", None)
        connection = self._autocommit()
        try:
            with connection.transaction():
                self.queue.expire_final_attempts(connection, lease_seconds=self.config.job_lease_seconds)
            due = self.due_tasks(connection, now)
        finally:
            connection.close()
        ran = {}
        for task in due:
            if self.stop_event.is_set():
                break
            ran[task] = self.run_task(task)
        return ran

    def due_tasks(self, connection, now) -> list[str]:
        rows = {r["task"]: r for r in self.schedule.all(connection)}

        def started(task):
            return (rows.get(task) or {}).get("last_started_at")

        def succeeded(task):
            return (rows.get(task) or {}).get("last_succeeded_at")

        due = []
        boundary = last_full_boundary(now, self.config.full_reconcile_at)
        full_ok = succeeded(RECONCILE_FULL)
        if (full_ok is None or full_ok < boundary) and (started(RECONCILE_FULL) is None or started(RECONCILE_FULL) < boundary
                                                        or started(RECONCILE_FULL) < now - timedelta(hours=1)):
            due.append(RECONCILE_FULL)
        if started(DISCOVERY) is None or now - started(DISCOVERY) >= self.config.discovery_interval:
            due.append(DISCOVERY)
        if self.auto_releaser is not None and (
                started(AUTO_RELEASE) is None or now - started(AUTO_RELEASE) >= self.config.discovery_interval):
            due.append(AUTO_RELEASE)                 # after DISCOVERY: it sees this tick's matches
        if started(RECONCILE_LIGHT) is None or now - started(RECONCILE_LIGHT) >= self.config.light_reconcile_interval:
            if RECONCILE_FULL not in due:            # FULL includes everything LIGHT checks
                due.append(RECONCILE_LIGHT)
        if self.backfill is not None and (started(HISTORICAL_BACKFILL) is None or now - started(HISTORICAL_BACKFILL)
                                          >= self.config.backfill_interval):
            due.append(HISTORICAL_BACKFILL)          # last: daily discovery, release and reconciliation first
        return due

    def run_task(self, task: str) -> dict:
        lock = self._autocommit()
        key = zlib.crc32(task.encode()) & 0x7FFFFFFF
        try:
            if not lock.execute("SELECT pg_try_advisory_lock(%s, %s)", (TASK_LOCK_NAMESPACE, key)).fetchone()[0]:
                return {"status": "SKIPPED", "reason": "ANOTHER_DAEMON_RUNS_IT"}
            try:
                now = self.clock()
                with lock.transaction():
                    previous = self.schedule.get(lock, task)
                    self.schedule.started(lock, task, now)
                try:
                    detail = self._execute(task, previous, now)
                    status = "SUCCEEDED"
                except Exception as exc:  # noqa: BLE001 - recorded; retried at the next due time
                    log.error("publisher task %s failed", task, extra={"error_type": type(exc).__name__})
                    detail, status = {"error": type(exc).__name__, "code": getattr(exc, "code", None)}, "FAILED"
                with lock.transaction():
                    self.schedule.finished(lock, task, self.clock(), status=status, detail=_compact(detail))
                return {"status": status, **_compact(detail)}
            finally:
                lock.execute("SELECT pg_advisory_unlock(%s, %s)", (TASK_LOCK_NAMESPACE, key))
        finally:
            lock.close()

    def _execute(self, task, previous, now) -> dict:
        if task == DISCOVERY:
            return self.planner.tick(calendar_days=self._discovery_days(previous, now))
        if task == AUTO_RELEASE:
            return self.auto_releaser.run()
        if task == HISTORICAL_BACKFILL:
            return self.backfill.run()
        if task == RECONCILE_LIGHT:
            return self.reconciler.run("LIGHT")
        return self.reconciler.run("FULL")

    def _discovery_days(self, previous, now):
        """Today; after downtime, every day since the last successful discovery (within the lookback)."""
        start, today = self.planner.window(now)
        last = (previous or {}).get("last_succeeded_at")
        if last is None or now - last > timedelta(hours=2):
            first = max(start, last.astimezone(CAIRO).date()) if last else start
            return [first + timedelta(days=n) for n in range((today - first).days + 1)]
        return [today]

    # -- workers ------------------------------------------------------------------------------
    def _worker_loop(self, worker_id: str) -> None:
        while not self.stop_event.is_set():
            try:
                kind = self.work_once(worker_id)
            except Exception as exc:  # noqa: BLE001 - a worker thread must survive
                log.error("publisher worker iteration failed", extra={"error_type": type(exc).__name__})
                kind = "IDLE"
            if kind == "IDLE":
                self._heartbeat(worker_id, None)
                self.stop_event.wait(self.config.poll_seconds)
        self._heartbeat(worker_id, None, stopped=True)

    def work_once(self, worker_id: str) -> str:
        connection = self._autocommit()
        try:
            job = self.queue.claim(connection, worker_id, self.kinds, lease_seconds=self.config.job_lease_seconds)
        finally:
            connection.close()
        if job is None:
            return "IDLE"
        self._heartbeat(worker_id, job["job_id"])
        try:
            self.handlers[job["job_kind"]].handle(job, worker_id)
        except Exception as exc:  # noqa: BLE001 - park the job for a bounded retry instead of leaving it RUNNING
            log.error("publisher job handler failed", extra={"error_type": type(exc).__name__,
                                                             "job_kind": job["job_kind"]})
            connection = self._autocommit()
            try:
                with connection.transaction():
                    if self.queue.retry(connection, job["job_id"], worker_id, error_code="WORKER_EXCEPTION",
                                        error=type(exc).__name__) is not None:
                        self.queue.event(connection, "JOB_WORKER_EXCEPTION", job_id=job["job_id"],
                                         lecture_id=job["lecture_id"], detail={"error": type(exc).__name__})
            finally:
                connection.close()
        return job["job_kind"]

    # -- helpers ------------------------------------------------------------------------------
    def _autocommit(self):
        connection = self.connect()
        connection.autocommit = True
        return connection

    def _heartbeat(self, worker_id, job_id, *, stopped: bool = False) -> None:
        try:
            connection = self._autocommit()
        except Exception:  # noqa: BLE001 - a missed heartbeat is what the monitor is for
            return
        try:
            self.queue.heartbeat(connection, worker_id, version=DAEMON_VERSION, current_job_id=job_id,
                                 detail={"mode": self.config.mode, "stopped": stopped,
                                         "library_id": self.config.library_id})
        except Exception:  # noqa: BLE001
            pass
        finally:
            connection.close()


def _compact(detail) -> dict:
    """Counts and codes only, small enough for bunny_publisher_schedule.last_detail."""
    if not isinstance(detail, dict):
        return {}
    out = {}
    for key, value in detail.items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if isinstance(v, (int, float, str, bool))}
        elif isinstance(value, list):
            out[key] = len(value)
    return out
