"""
Queue handlers for lecture-scoped publisher jobs: ACQUIRE_TRANSCRIPT (P0-B),
RESOLVE_RECORDING (P0-C), and the per-part Bunny tracks IMPORT_VIDEO,
POLL_ENCODING and PUBLISH_CAPTION (P0-D).

The future publisher daemon claims a job (PublisherQueueRepository.claim) and
calls `handler.handle(job, worker_id)`. The handler runs the worker - which
commits its own results in its own short transactions - and then records the
job transition and one event in ONE further short transaction. If that final
transaction cannot run (database down), the job stays RUNNING and is
reclaimed when its lease expires; re-running is safe because both workers
are idempotent.

Outcome -> job transition
    READY / LOCATED  SUCCEEDED (+ a follow-up job not before the next refresh / verify point)
    DONE (Bunny)     SUCCEEDED (+ the next track's job, enqueued in the SAME transaction)
    NOT_APPLICABLE   SUCCEEDED (nothing to do: cancelled, LI duplicate)
    PERMANENT        FAILED
    WAITING/RETRY/
    DEFERRED         RETRY_WAIT after the outcome's delay
    REVIEW           REVIEW_REQUIRED
"""
from __future__ import annotations

from app.db.repositories.publisher_queue import PublisherQueueRepository

LIVE_PRIORITY = 100
REFRESH_PRIORITY = 20
# Waiting for Teams can take days of polling; each worker's own deadline (72 h
# after the lecture) ends the loop, not the attempt counter.
MAX_ATTEMPTS = 100


def subject_key(lecture_id) -> str:
    return f"lecture:{lecture_id}"


def part_subject_key(lecture_id, part_index: int) -> str:
    return f"lecture:{lecture_id}:part:{int(part_index)}"


def parse_subject(subject: str) -> tuple[str, int | None]:
    """'lecture:<id>' or 'lecture:<id>:part:<n>' -> (lecture id, part or None)."""
    pieces = subject.split(":")
    if len(pieces) == 4 and pieces[0] == "lecture" and pieces[2] == "part":
        return pieces[1], int(pieces[3])
    if len(pieces) == 2 and pieces[0] == "lecture":
        return pieces[1], None
    raise ValueError(f"unrecognised subject key {subject!r}")


class _LectureJobHandler:
    job_kind: str
    event_prefix: str
    success_kind: str
    follow_up_flag: str

    def __init__(self, *, worker, connect, queue: PublisherQueueRepository | None = None):
        self.worker = worker
        self.connect = connect
        self.queue = queue or PublisherQueueRepository()

    def enqueue(self, connection, lecture_id, *, not_before=None, refresh: bool = False):
        return self.queue.enqueue(
            connection, job_kind=self.job_kind, subject_key=subject_key(lecture_id), lecture_id=lecture_id,
            priority=REFRESH_PRIORITY if refresh else LIVE_PRIORITY, max_attempts=MAX_ATTEMPTS,
            payload={self.follow_up_flag: refresh}, not_before=not_before)

    def handle(self, job: dict, worker_id: str):
        lecture_id = job["lecture_id"] or job["subject_key"].split(":", 1)[1]
        outcome = self._run(lecture_id, job)
        connection = self.connect()
        try:
            connection.autocommit = True
            with connection.transaction():
                self._transition(connection, job, worker_id, outcome)
        finally:
            connection.close()
        return outcome

    def _run(self, lecture_id, job):
        return self.worker.run(lecture_id, attempt=job["attempt_count"])

    def _event(self, kind: str) -> str:
        suffix = {self.success_kind: self.success_kind, "NOT_APPLICABLE": "NOT_APPLICABLE",
                  "PERMANENT": "FAILED", "REVIEW": "REVIEW_REQUIRED", "WAITING": "WAITING", "RETRY": "RETRY",
                  "DEFERRED": "DEFERRED"}[kind]
        return f"{self.event_prefix}_{suffix}"

    def _transition(self, connection, job, worker_id, outcome) -> None:
        job_id = job["job_id"]
        event = self._event(outcome.kind)
        if outcome.kind in (self.success_kind, "NOT_APPLICABLE"):
            owned = self.queue.succeed(connection, job_id, worker_id)
        elif outcome.kind == "PERMANENT":
            owned = self.queue.fail(connection, job_id, worker_id, error_code=outcome.reason, error=outcome.reason)
        elif outcome.kind == "REVIEW":
            owned = self.queue.review(connection, job_id, worker_id, error_code=outcome.reason, error=outcome.reason)
        else:
            owned = self.queue.retry(connection, job_id, worker_id, error_code=outcome.reason,
                                     error=outcome.reason, delay=outcome.retry_after) is not None
        if not owned:
            # The lease was taken over; the other worker now owns the job.
            event = f"{self.event_prefix}_LEASE_LOST"
        self.queue.event(connection, event, job_id=job_id, lecture_id=outcome.lecture_id
                         if outcome.kind != "PERMANENT" else None, detail=outcome.as_event())
        if owned and outcome.kind == self.success_kind and outcome.next_refresh_at is not None:
            self.enqueue(connection, outcome.lecture_id, not_before=outcome.next_refresh_at, refresh=True)
        if owned and outcome.kind == self.success_kind:
            for job_kind, part in getattr(outcome, "follow_ups", None) or ():
                priority, payload = follow_up(job, job_kind)
                self.queue.enqueue(connection, job_kind=job_kind, subject_key=part_subject_key(outcome.lecture_id, part),
                                   lecture_id=outcome.lecture_id, priority=priority, payload=payload,
                                   max_attempts=MAX_ATTEMPTS)


class TranscriptJobHandler(_LectureJobHandler):
    job_kind = "ACQUIRE_TRANSCRIPT"
    event_prefix = "TRANSCRIPT"
    success_kind = "READY"
    follow_up_flag = "refresh"


class RecordingJobHandler(_LectureJobHandler):
    """
    RESOLVE_RECORDING. A follow-up job carries {"verify": true} - the bounded
    re-check; an operator's job may carry {"rediscover": true} to re-open a
    lecture in review.
    """
    job_kind = "RESOLVE_RECORDING"
    event_prefix = "RECORDING"
    success_kind = "LOCATED"
    follow_up_flag = "verify"

    def _run(self, lecture_id, job):
        payload = job.get("payload") or {}
        return self.worker.run(lecture_id, attempt=job["attempt_count"], verify=bool(payload.get("verify")),
                               rediscover=bool(payload.get("rediscover")))


BUNNY_PRIORITY = {"IMPORT_VIDEO": 80, "POLL_ENCODING": 60, "PUBLISH_CAPTION": 90}
# Historical Backfill work (app/publisher/backfill.py): below EVERY daily priority (live 100, Bunny 60-90,
# refresh 20), so a worker always claims due daily work first. The run's id rides in the payload and is
# carried to each follow-up, so the whole chain keeps the lower priority; daily jobs carry no tag and
# their follow-ups are unchanged.
HISTORICAL_TAG = "historical_run_id"
HISTORICAL_PRIORITY = {"ACQUIRE_TRANSCRIPT": 10, "RESOLVE_RECORDING": 12, "IMPORT_VIDEO": 8, "POLL_ENCODING": 6,
                       "PUBLISH_CAPTION": 9}


def follow_up(job: dict, job_kind: str) -> tuple[int, dict | None]:
    """(priority, payload) of the job a finished `job` enqueues next."""
    tag = (job.get("payload") or {}).get(HISTORICAL_TAG)
    if tag:
        return HISTORICAL_PRIORITY.get(job_kind, min(HISTORICAL_PRIORITY.values())), {HISTORICAL_TAG: str(tag)}
    return BUNNY_PRIORITY.get(job_kind, LIVE_PRIORITY), None
BUNNY_EVENT_PREFIX = {"IMPORT_VIDEO": "IMPORT", "POLL_ENCODING": "ENCODING", "PUBLISH_CAPTION": "CAPTION"}


class BunnyJobHandler(_LectureJobHandler):
    """One per Bunny job kind. Subjects are per recording part: lecture:<id>:part:<n>."""
    success_kind = "DONE"
    follow_up_flag = "operator_retry"

    def __init__(self, job_kind: str, *, worker, connect, queue: PublisherQueueRepository | None = None):
        super().__init__(worker=worker, connect=connect, queue=queue)
        self.job_kind = job_kind
        self.event_prefix = BUNNY_EVENT_PREFIX[job_kind]

    def enqueue(self, connection, lecture_id, *, part_index: int = 1, not_before=None, refresh: bool = False):
        return self.queue.enqueue(
            connection, job_kind=self.job_kind, subject_key=part_subject_key(lecture_id, part_index),
            lecture_id=lecture_id, priority=BUNNY_PRIORITY[self.job_kind], max_attempts=MAX_ATTEMPTS,
            not_before=not_before)

    def handle(self, job: dict, worker_id: str):
        return super().handle({**job, "lecture_id": job["lecture_id"] or parse_subject(job["subject_key"])[0]},
                              worker_id)

    def _run(self, lecture_id, job):
        _, part = parse_subject(job["subject_key"])
        payload = job.get("payload") or {}
        return self.worker.run(lecture_id, part, attempt=job["attempt_count"],
                               operator=bool(payload.get("operator_retry")))


# P0-B's module-level names, unchanged.
JOB_KIND = TranscriptJobHandler.job_kind
