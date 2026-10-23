"""
Persistence for the Continuous Bunny Publisher queue (migration 026).

Same concurrency model as the Positive Moments media platform (023):

* enqueue is idempotent: one LIVE job per (job_kind, subject_key) is a
  partial unique index, so a duplicate enqueue returns the existing job;
* claim is `FOR UPDATE SKIP LOCKED` plus a lease - a crashed worker's RUNNING
  job becomes claimable again once its lease expires;
* every transition is compare-and-set on (status, locked_by), so a worker
  whose lease was taken over cannot finish a job it no longer owns.

* a RUNNING job whose lease expired on its FINAL attempt can never be claimed
  again (claim requires attempt_count < max_attempts); expire_final_attempts
  moves it to REVIEW_REQUIRED - its last attempt's external outcome is
  unknown, so a human decides, never a silent FAILED or an endless RUNNING.

Payloads and details must be identity only; the database refuses URL-like text.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

LIVE_STATUSES = ("QUEUED", "RUNNING", "RETRY_WAIT")
JOB_LEASE_SECONDS = 15 * 60
JOB_FIELDS = ("job_id", "job_kind", "subject_key", "lecture_id", "priority", "status", "payload",
              "attempt_count", "max_attempts", "next_attempt_at", "locked_by", "locked_at",
              "last_error_code", "last_error", "created_at", "updated_at", "finished_at")
_COLUMNS = ", ".join(JOB_FIELDS)

ENQUEUE = f"""
INSERT INTO public.bunny_publisher_jobs (job_id, job_kind, subject_key, lecture_id, priority, payload,
                                   max_attempts, next_attempt_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (job_kind, subject_key) WHERE status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') DO NOTHING
RETURNING {_COLUMNS}
"""
LIVE_JOB = f"""
SELECT {_COLUMNS} FROM public.bunny_publisher_jobs
 WHERE job_kind = %s AND subject_key = %s AND status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT')
"""
CLAIM = f"""
UPDATE public.bunny_publisher_jobs j
   SET status = 'RUNNING', locked_by = %(worker)s, locked_at = now(),
       attempt_count = j.attempt_count + 1, updated_at = now()
 WHERE j.job_id = (
        SELECT job_id FROM public.bunny_publisher_jobs
         WHERE job_kind = ANY(%(kinds)s)
           AND ((status = 'QUEUED' AND coalesce(next_attempt_at, now()) <= now())
                OR (status = 'RETRY_WAIT' AND next_attempt_at <= now())
                OR (status = 'RUNNING' AND locked_at < now() - make_interval(secs => %(lease)s)))
           AND attempt_count < max_attempts
         ORDER BY priority DESC, next_attempt_at NULLS FIRST, created_at, job_id
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
RETURNING {', '.join('j.' + f for f in JOB_FIELDS)}
"""


ENQUEUE_ONCE = f"""
INSERT INTO public.bunny_publisher_jobs (job_id, job_kind, subject_key, lecture_id, priority, payload,
                                   max_attempts, next_attempt_at)
SELECT %s, %s, %s, %s, %s, %s, %s, %s
 WHERE NOT EXISTS (SELECT 1 FROM public.bunny_publisher_jobs WHERE job_kind = %s AND subject_key = %s)
ON CONFLICT (job_kind, subject_key) WHERE status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') DO NOTHING
RETURNING {_COLUMNS}
"""
EXPIRE_FINAL = """
UPDATE public.bunny_publisher_jobs
   SET status = 'REVIEW_REQUIRED', locked_by = NULL, locked_at = NULL, finished_at = now(), updated_at = now(),
       last_error_code = 'LEASE_EXPIRED_ON_FINAL_ATTEMPT',
       last_error = 'the worker stopped during the final attempt; its external outcome is unknown'
 WHERE status = 'RUNNING' AND attempt_count >= max_attempts
   AND locked_at < now() - make_interval(secs => %s)
RETURNING job_id, job_kind, subject_key, lecture_id
"""
OPERATOR_RETRY = f"""
UPDATE public.bunny_publisher_jobs
   SET status = 'QUEUED', attempt_count = 0, next_attempt_at = now(), finished_at = NULL,
       locked_by = NULL, locked_at = NULL, updated_at = now(), payload = payload || %s
 WHERE job_id = %s AND status IN ('FAILED', 'REVIEW_REQUIRED')
   AND NOT EXISTS (SELECT 1 FROM public.bunny_publisher_jobs o
                    WHERE o.job_kind = bunny_publisher_jobs.job_kind AND o.subject_key = bunny_publisher_jobs.subject_key
                      AND o.status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT'))
RETURNING {_COLUMNS}
"""


def retry_delay(attempt_count: int) -> timedelta:
    """1 min, 5 min, 15 min, 1 h, then every 3 h (audit section 15)."""
    steps = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
    return steps[attempt_count - 1] if 1 <= attempt_count <= len(steps) else timedelta(hours=3)


def _row(cursor_row):
    return dict(zip(JOB_FIELDS, cursor_row)) if cursor_row else None


class PublisherQueueRepository:

    def enqueue(self, connection, *, job_kind: str, subject_key: str, lecture_id=None, priority: int = 100,
                payload: dict | None = None, max_attempts: int = 8, not_before=None) -> tuple[dict, bool]:
        """(job, created). A live job for the same thing is returned, not duplicated."""
        row = connection.execute(ENQUEUE, (uuid.uuid4(), job_kind, subject_key, lecture_id, priority,
                                           Jsonb(payload or {}), max_attempts, not_before)).fetchone()
        if row:
            return _row(row), True
        existing = connection.execute(LIVE_JOB, (job_kind, subject_key)).fetchone()
        return _row(existing), False

    def enqueue_once(self, connection, *, job_kind: str, subject_key: str, lecture_id=None, priority: int = 100,
                     payload: dict | None = None, max_attempts: int = 8, not_before=None) -> dict | None:
        """
        Create the job only if NO job of this kind ever existed for the subject.
        The planner's way to start a track: a finished (or reviewed) track is
        never restarted by planning - only by its own follow-ups or an operator.
        """
        row = connection.execute(ENQUEUE_ONCE, (uuid.uuid4(), job_kind, subject_key, lecture_id, priority,
                                                Jsonb(payload or {}), max_attempts, not_before,
                                                job_kind, subject_key)).fetchone()
        return _row(row)

    def expire_final_attempts(self, connection, *, lease_seconds: int = JOB_LEASE_SECONDS) -> list[dict]:
        """RUNNING jobs whose lease expired on their final attempt -> REVIEW_REQUIRED (+ one event each)."""
        expired = [dict(zip(("job_id", "job_kind", "subject_key", "lecture_id"), r))
                   for r in connection.execute(EXPIRE_FINAL, (lease_seconds,)).fetchall()]
        for job in expired:
            self.event(connection, "JOB_LEASE_EXPIRED_ON_FINAL_ATTEMPT", job_id=job["job_id"],
                       lecture_id=job["lecture_id"], detail={"job_kind": job["job_kind"]})
        return expired

    def operator_retry(self, connection, job_id, *, actor: str, reason: str, payload: dict | None = None) -> dict | None:
        """
        Re-open a FAILED / REVIEW_REQUIRED job for an authenticated operator,
        audited. None when the job is not in a retryable state or another live
        job already covers its subject.
        """
        job = _row(connection.execute(OPERATOR_RETRY, (Jsonb({**(payload or {}), "operator_retry": True}),
                                                       job_id)).fetchone())
        if job is not None:
            self.event(connection, "OPERATOR_RETRY", job_id=job_id, lecture_id=job["lecture_id"], actor=actor,
                       detail={"job_kind": job["job_kind"], "reason": reason[:300]})
        return job

    def lock_job(self, connection, job_id) -> dict | None:
        """The job row, locked FOR UPDATE NOWAIT inside the caller's transaction (LockNotAvailable if a
        worker's claim holds it). A claim (FOR UPDATE SKIP LOCKED) skips the row while it is held."""
        return _row(connection.execute(f"SELECT {_COLUMNS} FROM public.bunny_publisher_jobs WHERE job_id = %s "
                                       "FOR UPDATE NOWAIT", (job_id,)).fetchone())

    def operator_cancel(self, connection, job_id, *, actor: str, reason: str, detail: dict | None = None) -> dict | None:
        """
        QUEUED / RETRY_WAIT and unleased -> CANCELLED (terminal: never claimed, never re-enqueued),
        with one OPERATOR_CANCEL event. None when the job is not in a cancellable state. The caller
        decides eligibility and holds the row lock (lock_job) in the same transaction.
        """
        job = _row(connection.execute(f"""
            UPDATE public.bunny_publisher_jobs
               SET status = 'CANCELLED', finished_at = now(), updated_at = now(), next_attempt_at = NULL,
                   locked_by = NULL, locked_at = NULL
             WHERE job_id = %s AND status IN ('QUEUED', 'RETRY_WAIT') AND locked_by IS NULL
         RETURNING {_COLUMNS}""", (job_id,)).fetchone())
        if job is not None:
            self.event(connection, "OPERATOR_CANCEL", job_id=job_id, lecture_id=job["lecture_id"], actor=actor,
                       detail={"job_kind": job["job_kind"], "reason": reason[:300], **(detail or {})})
        return job

    def latest(self, connection, job_kind: str, subject_key: str) -> dict | None:
        """The most recent job of this kind for the subject, whatever its status."""
        return _row(connection.execute(
            f"SELECT {_COLUMNS} FROM public.bunny_publisher_jobs WHERE job_kind = %s AND subject_key = %s "
            "ORDER BY created_at DESC, job_id LIMIT 1", (job_kind, subject_key)).fetchone())

    def history(self, connection, *, job_id=None, lecture_id=None, limit: int = 200) -> list[dict]:
        fields = ("event_id", "job_id", "lecture_id", "event", "actor", "detail", "at")
        if job_id is not None:
            rows = connection.execute(f"SELECT {', '.join(fields)} FROM public.bunny_publisher_events WHERE job_id = %s "
                                      "ORDER BY event_id LIMIT %s", (job_id, limit)).fetchall()
        else:
            rows = connection.execute(f"SELECT {', '.join(fields)} FROM public.bunny_publisher_events WHERE lecture_id = %s "
                                      "ORDER BY event_id LIMIT %s", (lecture_id, limit)).fetchall()
        return [dict(zip(fields, r)) for r in rows]

    def claim(self, connection, worker: str, kinds, *, lease_seconds: int = JOB_LEASE_SECONDS) -> dict | None:
        return _row(connection.execute(CLAIM, {"worker": worker, "kinds": list(kinds),
                                               "lease": lease_seconds}).fetchone())

    def job(self, connection, job_id) -> dict | None:
        return _row(connection.execute(f"SELECT {_COLUMNS} FROM public.bunny_publisher_jobs WHERE job_id = %s",
                                       (job_id,)).fetchone())

    def _finish(self, connection, job_id, worker, status, *, error_code=None, error=None, clear_error=False) -> bool:
        row = connection.execute(
            """UPDATE public.bunny_publisher_jobs
                  SET status = %s, locked_by = NULL, locked_at = NULL, finished_at = now(), updated_at = now(),
                      last_error_code = CASE WHEN %s THEN NULL ELSE coalesce(%s, last_error_code) END,
                      last_error = CASE WHEN %s THEN NULL ELSE coalesce(%s, last_error) END
                WHERE job_id = %s AND status = 'RUNNING' AND locked_by = %s
            RETURNING job_id""", (status, clear_error, error_code, clear_error, error, job_id, worker)).fetchone()
        return row is not None

    def succeed(self, connection, job_id, worker) -> bool:
        # A SUCCEEDED job carries no error: every earlier retry/review reason is already in
        # bunny_publisher_events (each transition writes one), so the history is not lost.
        return self._finish(connection, job_id, worker, "SUCCEEDED", clear_error=True)

    def review(self, connection, job_id, worker, *, error_code: str, error: str) -> bool:
        return self._finish(connection, job_id, worker, "REVIEW_REQUIRED", error_code=error_code, error=error)

    def fail(self, connection, job_id, worker, *, error_code: str, error: str) -> bool:
        """A permanent failure: no retry can change the answer (e.g. the lecture does not exist)."""
        return self._finish(connection, job_id, worker, "FAILED", error_code=error_code, error=error)

    def retry(self, connection, job_id, worker, *, error_code: str, error: str, delay: timedelta | None = None) -> str | None:
        """RETRY_WAIT with backoff, or FAILED once attempts are exhausted. None if not ours."""
        job = self.job(connection, job_id)
        if job is None or job["status"] != "RUNNING" or job["locked_by"] != worker:
            return None
        if job["attempt_count"] >= job["max_attempts"]:
            return "FAILED" if self._finish(connection, job_id, worker, "FAILED",
                                            error_code=error_code, error=error) else None
        wait = delay if delay is not None else retry_delay(job["attempt_count"])
        row = connection.execute(
            """UPDATE public.bunny_publisher_jobs
                  SET status = 'RETRY_WAIT', locked_by = NULL, locked_at = NULL, updated_at = now(),
                      next_attempt_at = now() + make_interval(secs => %s),
                      last_error_code = %s, last_error = %s
                WHERE job_id = %s AND status = 'RUNNING' AND locked_by = %s
            RETURNING job_id""", (wait.total_seconds(), error_code, error, job_id, worker)).fetchone()
        return "RETRY_WAIT" if row else None

    def event(self, connection, event: str, *, job_id=None, lecture_id=None, detail: dict | None = None,
              actor: str = "publisher") -> int:
        return connection.execute(
            "INSERT INTO public.bunny_publisher_events (job_id, lecture_id, event, actor, detail) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING event_id",
            (job_id, lecture_id, event, actor, Jsonb(detail or {}))).fetchone()[0]

    def heartbeat(self, connection, worker_id: str, *, version: str, current_job_id=None,
                  detail: dict | None = None) -> None:
        connection.execute(
            """INSERT INTO public.bunny_publisher_heartbeats (worker_id, version, current_job_id, detail)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (worker_id) DO UPDATE SET seen_at = now(), version = EXCLUDED.version,
                   current_job_id = EXCLUDED.current_job_id, detail = EXCLUDED.detail""",
            (worker_id, version, current_job_id, Jsonb(detail or {})))
