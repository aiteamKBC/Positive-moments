"""
Retry Teams transcript: fetch ONE lecture's transcript from Microsoft Teams again, select it with
the platform's own rules, and let that lecture finish its normal pipeline.

WHEN IT IS OFFERED
------------------
The lecture's own state says its transcript is the problem: it is held at TRANSCRIPT or SELECTION,
or it was closed as "not delivered" because Teams had no usable transcript for it. Never for a
lecture whose quality evaluation is COMPLETE (its results are protected), whose Teams meeting is
not resolved, or that has not ended yet.

2026-11-17 "Marketing Impact and Planning" (94008498) is the reference case: Teams was asked three
minutes after the scheduled end while the class was still live, held only a 52-second pre-class
test, and the 2-hour transcript appeared minutes later on the same call.

WHAT RUNS - EXISTING CODE ONLY
------------------------------
The console only QUEUES (public.lecture_processing_jobs, reason TEAMS_TRANSCRIPT_RETRY, PROPOSED
migration 031). The background worker of the positive-moments-backfill service then:

  1. asks Teams for THIS lecture's transcripts with the Publisher's lecture-scoped transcript
     worker (app/publisher/transcripts.py, TranscriptWorker.run): one listing of the lecture's
     meeting, the occurrence window selection, the selected parts' raw WebVTT, the lecture's
     selection row. Stored content is reused; nothing else in the day is touched;
  2. resumes the lecture with the real PipelineOrchestrator narrowed to it - exactly what
     Manual Transcript Recovery runs (app/orchestration/transcript_recovery.py): canonical cues,
     speakers, attendance, engagement, QA, rendering, Perfect, recording link, each only if it is
     still owed. A COMPLETE stage is never re-run.

DUPLICATE PAID QA
-----------------
The queue's guarantees (one active job per lecture across every reason, leases, attempts), the
scheduler cycle lock and the lecture lock inside the run, and stage resolution: QA runs only when
the resolver says it is owed, so a lecture whose QA is done buys nothing.

THE LECTURE LOCK (2026-11-20, production job 0f25407b)
------------------------------------------------------
The Publisher's transcript worker takes the lecture's SESSION advisory lock on its own autocommit
connection. That is sound on the direct endpoint the Publisher daemon is required to use, but this
worker's connections are the pooled DATABASE_URL: the lock and its unlock reached different server
backends, the lock stayed on one of them, and the lecture's first lecture-scoped stage
(RESOLVE_ATTENDANCE) found it busy.

So the worker holds the lecture's lock as a TRANSACTION advisory lock on the job's own connection,
taken before Teams is asked and kept through the pipeline run. The server releases it at the job's
commit or rollback, whatever happens, and a transaction never leaves its backend, so it cannot
leak through the pooler. The Teams fetch is told the caller holds the lock (LockHeldByCaller) and
takes none of its own. Same namespace and key as every other lecture lock, so the scheduler, the
Publisher daemon and the console still exclude this run; the orchestrator's own per-stage lock on
the same connection is the same session's and is granted.
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, timezone

from app.common.errors import DATABASE_ERROR, PlatformError
from app.db.repositories.lecture_processing_jobs import (
    REASON_TEAMS_TRANSCRIPT_RETRY,
    LectureProcessingJobRepository,
)
from app.orchestration.locks import LOCK_NAMESPACE, LectureBusy, lock_key
from app.orchestration.stages import (
    COMPLETE,
    MEETING,
    QA_EVALUATION,
    RUN_TYPE_MANUAL,
    SELECTION,
    STAGE_ORDER,
    TRANSCRIPT,
)
from app.orchestration.state import NOT_DELIVERED_SELECTION_REASONS
from app.orchestration.transcript_recovery import (
    ALREADY_QUEUED,
    NOTHING_TO_PROCESS,
    QUEUE_UNAVAILABLE,
    ProcessingRefused,
    with_lecture_outcome,
)

TEAMS_TRANSCRIPT_RETRY_VERSION = "teams_transcript_retry_v1"
ACTION = "ACQUIRE_TRANSCRIPT"

# Why the action is not offered.
LECTURE_NOT_FOUND = "LECTURE_NOT_FOUND"
SUPPRESSED_DUPLICATE = "SUPPRESSED_DUPLICATE"
QA_ALREADY_COMPLETE = "QA_ALREADY_COMPLETE"
MEETING_NOT_RESOLVED = "MEETING_NOT_RESOLVED"
LECTURE_NOT_ENDED = "LECTURE_NOT_ENDED"
NO_TRANSCRIPT_PROBLEM = "NO_TRANSCRIPT_PROBLEM"
# The worker's answers.
TEAMS_TRANSCRIPT_UNAVAILABLE = "TEAMS_TRANSCRIPT_UNAVAILABLE"
TEAMS_UNREACHABLE = "TEAMS_UNREACHABLE"

MESSAGES = {
    LECTURE_NOT_FOUND: "This lecture does not exist.",
    SUPPRESSED_DUPLICATE: "This calendar event is a suppressed duplicate; the kept lecture carries the transcript.",
    QA_ALREADY_COMPLETE: ("This lecture's quality evaluation is complete. Its transcript and results are kept "
                          "as they are."),
    MEETING_NOT_RESOLVED: "The lecture's Teams meeting is not resolved, so there is nothing to ask Teams about.",
    LECTURE_NOT_ENDED: "The lecture has not ended yet; Teams produces the transcript after the meeting.",
    NO_TRANSCRIPT_PROBLEM: "This lecture's transcript is not what holds it up.",
    ALREADY_QUEUED: "Processing is already queued or running for this lecture.",
    QUEUE_UNAVAILABLE: ("Retrying the Teams transcript needs a server update that is not installed yet "
                        "(migration 031). Nothing was queued."),
}

# Plain words for the Teams worker's reason codes (app/publisher/transcripts.py).
_TEAMS_REASONS = (
    ("TRANSCRIPT_UNUSABLE", "Teams only has a transcript too short or empty to use"),
    ("NO_TRANSCRIPT_FOR_OCCURRENCE", "Teams has no transcript for this lecture's time"),
    ("NO_TRANSCRIPTS_AVAILABLE", "Teams has no transcript for this meeting"),
    ("GRAPH_REJECTED", "Teams refused to deliver the transcript content"),
    ("GRAPH_ACCESS_BLOCKED", "access to this meeting's transcripts is blocked"),
    ("MEETING_NOT_RESOLVED", "the lecture's Teams meeting is not resolved"),
    ("CONTENT_NOT_YET_STORED", "the transcript content could not be downloaded yet"),
)

LECTURE_END = "SELECT scheduled_end FROM public.lecture_sessions WHERE lecture_id = %s"
TRANSCRIPT_STAGES = (TRANSCRIPT, SELECTION)
LEGACY_SYNC_STAGES = ("LEGACY_QA_SYNC", "PERFECT_SYNC")
TRANSACTION_LOCK = "SELECT pg_try_advisory_xact_lock(%s, %s)"


def lock_for_transaction(connection, lecture_id) -> bool:
    """
    Take the lecture's lock for the caller's open transaction (see THE LECTURE LOCK). False when
    another session holds it. Refused on an autocommit connection, where it would end with the
    statement and protect nothing.
    """
    if getattr(connection, "autocommit", False):
        raise PlatformError(DATABASE_ERROR, "the lecture lock needs the job's transaction, "
                                            "not an autocommit connection")
    try:
        row = connection.execute(TRANSACTION_LOCK, (LOCK_NAMESPACE, lock_key(lecture_id))).fetchone()
    except Exception as exc:
        raise PlatformError(DATABASE_ERROR, "lecture lock acquire failed") from exc
    return bool(row and row[0])


class LockHeldByCaller:
    """
    The Teams fetch's lock manager: the job already holds this lecture's lock in its transaction,
    so the fetch takes no session lock on its own (pooled, autocommit) connection.
    """

    def try_acquire(self, connection, lecture_id) -> bool:
        return True

    def release(self, connection, lecture_id) -> bool:
        return True


def teams_reason_text(reason: str | None) -> str:
    for code, text in _TEAMS_REASONS:
        if reason and code in reason:
            return text
    return f"Teams answered {reason}" if reason else "Teams gave no usable answer"


def transcript_problem(state) -> str | None:
    """The transcript fault this lecture's own state reports, or None."""
    if state.get("not_delivered_reason") in NOT_DELIVERED_SELECTION_REASONS:
        return state["not_delivered_reason"]
    stage = state.get("blocking_stage")
    if stage in TRANSCRIPT_STAGES:
        item = state["stages"].get(stage, {})
        return item.get("reason") or item.get("action") or item.get("state") or stage
    return None


class TeamsTranscriptRetry:
    def __init__(self, *, resolver, orchestrator_factory=None, fetch_transcript=None, jobs=None,
                 clock=None, lock_lecture=None):
        self.resolver = resolver
        # Worker side only: (lecture_id) -> PipelineOrchestrator narrowed to that lecture, and
        # (lecture_id) -> TranscriptOutcome from the Publisher's lecture-scoped Teams worker.
        self.orchestrator_factory = orchestrator_factory
        self.fetch_transcript = fetch_transcript
        # (connection, lecture_id) -> bool: the lecture's lock for the job's transaction.
        self.lock_lecture = lock_lecture or lock_for_transaction
        self.jobs = jobs or LectureProcessingJobRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = logging.getLogger(__name__)

    # -- reads ---------------------------------------------------------------------------------
    def status(self, connection, lecture_id) -> dict:
        """The plan and the lecture's latest background job. Read-only."""
        plan = self.plan(connection, lecture_id)
        job = (self.jobs.latest_for_lecture(connection, str(lecture_id))
               if self.jobs.available(connection) else None)
        # A failed job carries the lecture's current outcome, as in the recovery list.
        job = with_lecture_outcome(connection, self.resolver, [job])[0] if job else None
        return {"lecture_id": str(lecture_id), "plan": plan, "job": job}

    def plan(self, connection, lecture_id, *, worker: bool = False) -> dict:
        """
        Whether the retry is offered now, and what it would do. Read-only. `worker=True` is the
        background worker re-checking its own job: the queue questions no longer apply.
        """
        lecture_id = str(lecture_id)
        row = connection.execute(LECTURE_END, (lecture_id,)).fetchone()
        if row is None:
            return self._refused(LECTURE_NOT_FOUND, lecture_id)
        state = self.resolver.for_lecture(connection, lecture_id)
        stages = state["stages"]
        problem = transcript_problem(state)
        qa_state = stages.get(QA_EVALUATION, {}).get("state")
        reason = None
        if state.get("is_suppressed_duplicate"):
            reason = SUPPRESSED_DUPLICATE
        elif qa_state == COMPLETE:
            reason = QA_ALREADY_COMPLETE
        elif stages.get(MEETING, {}).get("state") != COMPLETE:
            reason = MEETING_NOT_RESOLVED
        elif row[0] is None or row[0] > self.clock():
            reason = LECTURE_NOT_ENDED
        elif not worker:
            if problem is None:
                reason = NO_TRANSCRIPT_PROBLEM
            elif not self.jobs.accepts_reason(connection, REASON_TEAMS_TRANSCRIPT_RETRY):
                reason = QUEUE_UNAVAILABLE
            elif self.jobs.active_for_lecture(connection, lecture_id) is not None:
                reason = ALREADY_QUEUED
        return {
            "available": reason is None,
            "reason": reason,
            "reason_message": MESSAGES.get(reason) if reason else None,
            "lecture_id": state["lecture_id"],
            "subject": state.get("subject"),
            "session_date": state["session_date"],
            "problem": problem,
            "not_delivered": bool(state.get("not_delivered")),
            "action": ACTION,
            "resume_stage": state.get("blocking_stage"),
            "next_action": state["next_executable_action"],
            "qa_state": qa_state,
            "stages_preserved": [name for name in STAGE_ORDER
                                 if stages.get(name, {}).get("state") == COMPLETE],
            "scope": "LECTURE",
            "cost": {
                "calls_microsoft_graph": True,
                # Only if Teams now has a usable transcript: QA is then owed and runs once.
                "buys_model_generation": qa_state != COMPLETE,
                "writes_legacy_production_row": any(
                    stages.get(name, {}).get("state") != COMPLETE for name in LEGACY_SYNC_STAGES),
                "affects_other_lectures_that_day": False,
            },
        }

    def _refused(self, reason, lecture_id) -> dict:
        return {"available": False, "reason": reason, "reason_message": MESSAGES[reason],
                "lecture_id": lecture_id, "problem": None, "action": ACTION}

    # -- the console's one write ------------------------------------------------------------------
    def queue(self, connection, lecture_id, *, requested_by=None) -> dict:
        """
        Queue this lecture for the background worker if the retry is offered. Never runs anything;
        the caller commits. Idempotent: an active job (of any reason) is returned, never duplicated.
        """
        lecture_id = str(lecture_id)
        plan = self.plan(connection, lecture_id)
        if plan["reason"] == ALREADY_QUEUED:
            # A second click: the lecture keeps its one active job, which is returned.
            return {"queued": True, "created": False, "reason": ALREADY_QUEUED,
                    "reason_message": MESSAGES[ALREADY_QUEUED], "plan": plan,
                    "job": self.jobs.active_for_lecture(connection, lecture_id)}
        if not plan["available"]:
            job = (self.jobs.latest_for_lecture(connection, lecture_id)
                   if self.jobs.available(connection) else None)
            return {"queued": False, "created": False, "reason": plan["reason"],
                    "reason_message": plan["reason_message"], "plan": plan, "job": job}
        job, created = self.jobs.enqueue(connection, lecture_id, reason=REASON_TEAMS_TRANSCRIPT_RETRY,
                                         requested_by=requested_by)
        self.log.info("teams transcript retry queued", extra={"fields": {
            "service": "teams_transcript_retry", "lecture_id": lecture_id,
            "job_id": job["job_id"] if job else None, "created": created, "problem": plan["problem"]}})
        return {"queued": True, "created": created,
                "reason": None if created else ALREADY_QUEUED,
                "reason_message": None if created else MESSAGES[ALREADY_QUEUED],
                "plan": plan, "job": job}

    # -- the worker's run -----------------------------------------------------------------------------
    def process(self, connection, lecture_id, *, entry_point: str | None = None) -> dict:
        """
        Ask Teams again (lecture-scoped), then resume the lecture through the real orchestrator.
        Called by the background worker; the caller commits.
        """
        lecture_id = str(lecture_id)
        plan = self.plan(connection, lecture_id, worker=True)
        if not plan["available"]:
            if plan["reason"] == QA_ALREADY_COMPLETE:
                # Done already (the nightly cycle, an upload, an earlier attempt): nothing to buy.
                raise ProcessingRefused(NOTHING_TO_PROCESS, {**plan, "qa_state": COMPLETE})
            raise ProcessingRefused(plan["reason"], plan, detail=plan["reason_message"])

        # Held until the job commits or rolls back; another holder means another process owns
        # the lecture right now - the worker defers without spending an attempt.
        if not self.lock_lecture(connection, lecture_id):
            raise LectureBusy(lecture_id)
        teams = None
        if plan["problem"] is not None:
            teams = self._ask_teams(lecture_id)
            state = self.resolver.for_lecture(connection, lecture_id)
            still = transcript_problem(state)
            if still is not None:
                detail = (f"{teams_reason_text(teams['reason'])} ({teams['reason']}). No processing "
                          f"was run. If Teams lists the transcript but cannot deliver it, use Upload "
                          f"transcript")
                raise ProcessingRefused(TEAMS_TRANSCRIPT_UNAVAILABLE, {
                    **plan, "teams": teams, "problem": still,
                    "next_action": state["next_executable_action"],
                    "qa_state": state["stages"].get(QA_EVALUATION, {}).get("state")}, detail=detail)

        started_at = self.clock()
        orchestrator = self.orchestrator_factory(lecture_id)
        summary = orchestrator.run_window(
            connection, _date.fromisoformat(plan["session_date"]), run_type=RUN_TYPE_MANUAL,
            lecture_ids=[lecture_id])
        item = next((row for row in summary.get("lectures", []) if row["lecture_id"] == lecture_id), {})
        final = self.resolver.for_lecture(connection, lecture_id)
        outcome = {
            "status": summary.get("status"),
            "item_status": item.get("status"),
            "run_id": str(summary["run_id"]) if summary.get("run_id") else None,
            "started_at": started_at.isoformat(),
            "finished_at": self.clock().isoformat(),
            "actions": item.get("actions", []),
            "error_code": item.get("error_code"),
            "final_action": final["next_executable_action"],
            "final_blocking_stage": final["blocking_stage"],
            "qa_state": final["stages"].get(QA_EVALUATION, {}).get("state"),
            "provider_calls": summary.get("provider_calls", 0),
            "graph_calls": summary.get("graph_calls", 0) + (teams or {}).get("graph_calls", 0),
            "entry_point": entry_point or "CLI",
            "teams": teams,
        }
        return {"lecture_id": lecture_id, "plan": plan, "processing": outcome, "run": summary}

    def _ask_teams(self, lecture_id) -> dict:
        from app.publisher.transcripts import DEFERRED, RETRY

        result = self.fetch_transcript(lecture_id)
        teams = {"kind": result.kind, "reason": result.reason, "graph_calls": result.graph_calls,
                 "artifacts_listed": result.detail.get("artifacts_listed"),
                 "parts_fetched": result.detail.get("parts_fetched"),
                 "worker_version": TEAMS_TRANSCRIPT_RETRY_VERSION}
        self.log.info("teams transcript retry asked teams", extra={"fields": {
            "service": "teams_transcript_retry", "lecture_id": lecture_id, **teams}})
        if result.kind == DEFERRED:
            # Another writer holds the lecture or its transcript rows: try again later, free.
            raise LectureBusy(lecture_id)
        if result.kind == RETRY:
            raise PlatformError(TEAMS_UNREACHABLE,
                                f"Microsoft Teams could not be reached ({result.reason}); it is retried "
                                f"automatically.")
        return teams
