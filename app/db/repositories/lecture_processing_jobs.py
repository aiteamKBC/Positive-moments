"""
Durable per-lecture processing jobs (PROPOSED migration 030).

INTENT and PROGRESS only - which lecture should be processed, and how far that request has got.
The work is the existing lecture-scoped orchestrator run (app/orchestration/transcript_recovery.py)
and its results live where every pipeline result lives. Every method takes an open connection and
commits nothing: the caller owns the transaction, so an enqueue commits atomically with the
transcript upload that caused it.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError

QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
ACTIVE = (QUEUED, RUNNING)

REASON_MANUAL_TRANSCRIPT_UPLOAD = "MANUAL_TRANSCRIPT_UPLOAD"
# An operator asked for the lecture's transcript to be fetched from Teams again (PROPOSED migration 031).
REASON_TEAMS_TRANSCRIPT_RETRY = "TEAMS_TRANSCRIPT_RETRY"

REASON_CONSTRAINT = """
SELECT pg_get_constraintdef(c.oid)
  FROM pg_constraint c
 WHERE c.conrelid = to_regclass('public.lecture_processing_jobs')
   AND c.conname = 'lecture_processing_jobs_reason_known'
"""

DEFAULT_MAX_ATTEMPTS = 3

JOB_FIELDS = (
    "job_id", "lecture_id", "reason", "status", "requested_by", "attempt_count", "max_attempts",
    "next_attempt_after", "claimed_by", "lease_expires_at", "heartbeat_at", "pipeline_run_id",
    "last_error_code", "last_error_message", "outcome", "created_at", "started_at", "finished_at",
    "updated_at")
_SELECT = f"SELECT {', '.join('j.' + name for name in JOB_FIELDS)} FROM public.lecture_processing_jobs j"

ENQUEUE = """
INSERT INTO public.lecture_processing_jobs (job_id, lecture_id, reason, status, requested_by, max_attempts)
VALUES (%(job_id)s, %(lecture_id)s, %(reason)s, 'QUEUED', %(requested_by)s, %(max_attempts)s)
ON CONFLICT (lecture_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
RETURNING job_id
"""

# Due QUEUED jobs first-come first-served, and RUNNING jobs whose lease ran out (a dead worker).
# SKIP LOCKED: two workers can never claim one job. Every claim is an attempt.
CLAIM = f"""
WITH candidate AS (
    SELECT job_id FROM public.lecture_processing_jobs
     WHERE (status = 'QUEUED' AND next_attempt_after <= now())
        OR (status = 'RUNNING' AND lease_expires_at < now())
     ORDER BY created_at ASC
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE public.lecture_processing_jobs j
   SET status = 'RUNNING', attempt_count = j.attempt_count + 1,
       claimed_by = %(worker)s, started_at = coalesce(j.started_at, now()),
       heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
       updated_at = now()
  FROM candidate c
 WHERE j.job_id = c.job_id
RETURNING {', '.join('j.' + name for name in JOB_FIELDS)}
"""


def _job(row) -> dict | None:
    if row is None:
        return None
    job = dict(zip(JOB_FIELDS, row))
    for key in ("job_id", "lecture_id", "pipeline_run_id"):
        if job[key] is not None:
            job[key] = str(job[key])
    return job


class LectureProcessingJobRepository:
    def available(self, connection) -> bool:
        """Whether migration 030 is applied. The queue is optional until it is."""
        try:
            row = connection.execute(
                "SELECT to_regclass('public.lecture_processing_jobs') IS NOT NULL").fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing queue lookup failed") from exc
        return bool(row and row[0])

    def accepts_reason(self, connection, reason: str) -> bool:
        """Whether the table exists and its reason constraint admits `reason` (031 for a Teams retry)."""
        if not self.available(connection):
            return False
        try:
            row = connection.execute(REASON_CONSTRAINT).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing queue lookup failed") from exc
        return row is None or f"'{reason}'" in (row[0] or "")

    # -- the console's side ----------------------------------------------------------------------
    def enqueue(self, connection, lecture_id, *, reason=REASON_MANUAL_TRANSCRIPT_UPLOAD,
                requested_by=None, max_attempts=DEFAULT_MAX_ATTEMPTS) -> tuple[dict, bool]:
        """(the lecture's active job, whether this call created it). Never a second active job."""
        try:
            created = connection.execute(ENQUEUE, {
                "job_id": uuid.uuid4(), "lecture_id": str(lecture_id), "reason": reason,
                "requested_by": (requested_by or "")[:150] or None,
                "max_attempts": int(max_attempts)}).fetchone()
            job = self.active_for_lecture(connection, lecture_id)
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing job enqueue failed") from exc
        return job, created is not None

    def active_for_lecture(self, connection, lecture_id) -> dict | None:
        return self._one(connection, _SELECT + " WHERE j.lecture_id = %s AND j.status IN ('QUEUED', 'RUNNING')",
                         (str(lecture_id),))

    def latest_for_lecture(self, connection, lecture_id) -> dict | None:
        return self._one(connection, _SELECT + " WHERE j.lecture_id = %s ORDER BY j.created_at DESC LIMIT 1",
                         (str(lecture_id),))

    def get(self, connection, job_id) -> dict | None:
        return self._one(connection, _SELECT + " WHERE j.job_id = %s", (str(job_id),))

    def recent(self, connection, limit: int = 20) -> list[dict]:
        """Newest first, with the lecture's subject and date for the console."""
        try:
            rows = connection.execute(
                f"SELECT {', '.join('j.' + name for name in JOB_FIELDS)}, l.subject, l.session_date"
                " FROM public.lecture_processing_jobs j"
                " JOIN public.lecture_sessions l ON l.lecture_id = j.lecture_id"
                " ORDER BY j.created_at DESC LIMIT %s", (int(limit),)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing job read failed") from exc
        jobs = []
        for row in rows:
            job = _job(row[:len(JOB_FIELDS)])
            job["subject"], job["session_date"] = row[len(JOB_FIELDS)], row[len(JOB_FIELDS) + 1]
            jobs.append(job)
        return jobs

    # -- the worker's side -----------------------------------------------------------------------
    def claim(self, connection, *, worker: str, lease_seconds: int) -> dict | None:
        try:
            row = connection.execute(CLAIM, {"worker": worker[:150],
                                             "lease_seconds": int(lease_seconds)}).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing job claim failed") from exc
        return _job(row)

    def complete(self, connection, job_id, *, outcome: dict, pipeline_run_id=None) -> None:
        self._finish(connection, job_id, COMPLETED, outcome=outcome, pipeline_run_id=pipeline_run_id)

    def fail(self, connection, job_id, *, error_code, error_message=None, outcome=None,
             pipeline_run_id=None) -> None:
        self._finish(connection, job_id, FAILED, outcome=outcome or {}, pipeline_run_id=pipeline_run_id,
                     error_code=error_code, error_message=error_message)

    def retry_later(self, connection, job_id, *, delay_seconds: int, error_code, error_message=None,
                    outcome=None, pipeline_run_id=None, refund_attempt: bool = False) -> None:
        """
        Back to QUEUED, not before `delay_seconds`. `refund_attempt` for a deferral that did no
        work (the nightly cycle held the lock): waiting is not a failed attempt.
        """
        self._execute(connection, """
            UPDATE public.lecture_processing_jobs
               SET status = 'QUEUED',
                   attempt_count = CASE WHEN %(refund)s THEN greatest(attempt_count - 1, 0)
                                        ELSE attempt_count END,
                   next_attempt_after = now() + make_interval(secs => %(delay)s),
                   claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = now(),
                   last_error_code = %(code)s, last_error_message = %(message)s,
                   outcome = coalesce(%(outcome)s::jsonb, outcome),
                   pipeline_run_id = coalesce(%(run)s, pipeline_run_id),
                   updated_at = now()
             WHERE job_id = %(job_id)s AND status = 'RUNNING'""",
            {"job_id": str(job_id), "refund": refund_attempt, "delay": int(delay_seconds),
             "code": error_code, "message": (error_message or "")[:500] or None,
             "outcome": json.dumps(outcome) if outcome is not None else None,
             "run": pipeline_run_id})

    # -- plumbing ---------------------------------------------------------------------------------
    def _finish(self, connection, job_id, status, *, outcome, pipeline_run_id=None, error_code=None,
                error_message=None) -> None:
        self._execute(connection, """
            UPDATE public.lecture_processing_jobs
               SET status = %(status)s, finished_at = now(), updated_at = now(), heartbeat_at = now(),
                   claimed_by = NULL, lease_expires_at = NULL,
                   last_error_code = %(code)s, last_error_message = %(message)s,
                   outcome = %(outcome)s::jsonb,
                   pipeline_run_id = coalesce(%(run)s, pipeline_run_id)
             WHERE job_id = %(job_id)s AND status = 'RUNNING'""",
            {"job_id": str(job_id), "status": status, "code": error_code,
             "message": (error_message or "")[:500] or None, "outcome": json.dumps(outcome or {}),
             "run": pipeline_run_id})

    @staticmethod
    def _execute(connection, statement, params) -> None:
        try:
            connection.execute(statement, params)
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing job update failed") from exc

    @staticmethod
    def _one(connection, statement, params) -> dict | None:
        try:
            row = connection.execute(statement, params).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "processing job read failed") from exc
        return _job(row)
