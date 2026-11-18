"""
Lecture-scoped recovery after a manual transcript upload.

WHAT RUNS
---------
The real `PipelineOrchestrator.run_window`, narrowed to the one lecture, with a runner whose
`lecture_scope` is that lecture (app/orchestration/runner.py). That is the same orchestrator the
nightly cycle and the console's Retry use - locks, n8n precheck, audit run row, pass loop, stall
detection, provider circuit - with one difference that matters here:

  the day-scoped transcript stages (selection, canonical cues, speakers) run for THIS lecture
  only, and the day-wide Graph transcript sweep is refused.

A plain Retry would advance the whole day on those stages. Recovering one lecture from an upload
must not re-derive its siblings, so it does not use Retry.

WHEN IT RUNS
------------
Only when the lecture's current transcript includes a manual upload and EVERY transcript part the
selector chose for the occurrence holds content. A partial lecture waits for its other parts
rather than being evaluated on part of what was said. Then it resumes from the earliest
incomplete stage, exactly as a retry does: a COMPLETE stage is never re-run, so a lecture whose
QA is already done is not re-analysed and processing again is a no-op. QA runs only when the
resolver says its prerequisites are met (that is what reaching RUN_QA means).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, timezone

from app.db.repositories.lecture_processing_jobs import ACTIVE, LectureProcessingJobRepository
from app.db.repositories.manual_transcripts import ManualTranscriptRepository
from app.orchestration.runner import PROVIDER_ACTIONS
from app.orchestration.stages import (
    COMPLETE,
    IDLE_ACTIONS,
    MANUAL_REVIEW_REQUIRED,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    QA_EVALUATION,
    RECORDING_LINK,
    RUN_TYPE_MANUAL,
    STAGE_ORDER,
)


# Refusal codes for processing.
NO_MANUAL_UPLOAD = "NO_MANUAL_UPLOAD"
WAITING_FOR_TRANSCRIPT_PARTS = "WAITING_FOR_TRANSCRIPT_PARTS"
NOTHING_TO_PROCESS = "NOTHING_TO_PROCESS"
QA_RUNS_ON_THE_SERVER = "QA_RUNS_ON_THE_SERVER"
ALREADY_QUEUED = "ALREADY_QUEUED"
QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"

# The paid quality evaluation's own command (app/cli/main.py), run on the server.
SERVER_COMMAND = "python -m app.cli.main process-manual-transcript --lecture-id {lecture_id}"

PROCESSING_MESSAGES = {
    NO_MANUAL_UPLOAD: "This lecture has no manually uploaded transcript to process.",
    WAITING_FOR_TRANSCRIPT_PARTS: "Other transcript parts of this lecture are still missing. Upload them first.",
    NOTHING_TO_PROCESS: "Every stage this lecture can take is already complete or waiting on another source.",
    QA_RUNS_ON_THE_SERVER: ("The next step is the quality evaluation (a model call that can take several "
                            "minutes). It is not run from the console: it runs on the server with the "
                            "process-manual-transcript command, or in the nightly cycle for lectures of "
                            "the last three days."),
    ALREADY_QUEUED: "Processing is already queued or running for this lecture.",
    QUEUE_UNAVAILABLE: ("The background processing queue is not installed on this server yet "
                        "(migration 030). The transcript is stored; nothing was queued."),
}

# WHY THE CONSOLE NEVER BUYS THE GENERATION
# ------------------------------------------
# A model call can take up to max_tries x timeout (3 x 300 s, plus waits). The whole processing run is
# ONE database transaction, committed when the request returns. A web worker killed by its request
# timeout mid-call rolls back everything - including the answer that was already paid for - and
# pressing "resume" would pay again into the same timeout. So the console never processes at all: it
# QUEUES the lecture (queue_processing, public.lecture_processing_jobs) and the background worker in
# the positive-moments-backfill service runs process() with the provider allowed, where no HTTP
# timeout exists (app/orchestration/lecture_jobs.py). The server CLI remains a manual fallback.


class ProcessingRefused(RuntimeError):
    def __init__(self, code: str, plan: dict | None = None):
        super().__init__(PROCESSING_MESSAGES.get(code, code))
        self.code = code
        self.detail = PROCESSING_MESSAGES.get(code, code)
        self.plan = plan or {}


class ManualTranscriptRecovery:
    def __init__(self, *, uploads, resolver, orchestrator_factory, repository=None,
                 clock=None, allow_provider: bool = True, jobs=None):
        self.uploads = uploads                       # ManualTranscriptUploadService
        self.resolver = resolver
        self.orchestrator_factory = orchestrator_factory   # (lecture_id) -> PipelineOrchestrator
        # Whether THIS entry point may buy a QA generation: False for the console, True for the CLI.
        self.allow_provider = allow_provider
        self.repository = repository or ManualTranscriptRepository()
        # The durable per-lecture queue the background worker consumes (lecture_jobs.py).
        self.jobs = jobs or LectureProcessingJobRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = logging.getLogger(__name__)

    # -- reads -----------------------------------------------------------------------------------
    def status(self, connection, lecture_id) -> dict:
        """
        The upload target, the processing plan and the lecture's latest background job. Read-only.

        The plan is the one the background worker would follow (QA included): the console no
        longer runs processing itself, it queues it.
        """
        target = self.uploads.target(connection, lecture_id)
        plan = self.plan_processing(connection, lecture_id, target=target, provider_allowed=True)
        job = self._latest_job(connection, lecture_id)
        if plan["available"] and job and job["status"] in ACTIVE:
            plan = {**plan, "available": False, "reason": ALREADY_QUEUED,
                    "reason_message": PROCESSING_MESSAGES[ALREADY_QUEUED]}
        return {**target, "processing": plan, "job": job}

    def queue_processing(self, connection, lecture_id, *, requested_by=None) -> dict:
        """
        Queue this lecture for the background worker, if there is anything to process.

        Never runs anything. The caller commits - after a store, in the same transaction, so a
        stored transcript and its queued job exist together or not at all. Idempotent: a lecture
        already queued or running keeps its one job.
        """
        lecture_id = str(lecture_id)
        if not self.jobs.available(connection):
            return {"queued": False, "created": False, "reason": QUEUE_UNAVAILABLE,
                    "reason_message": PROCESSING_MESSAGES[QUEUE_UNAVAILABLE], "job": None}
        plan = self.plan_processing(connection, lecture_id, provider_allowed=True)
        if not plan["available"]:
            return {"queued": False, "created": False, "reason": plan["reason"],
                    "reason_message": plan["reason_message"], "plan": plan,
                    "job": self.jobs.latest_for_lecture(connection, lecture_id)}
        job, created = self.jobs.enqueue(connection, lecture_id, requested_by=requested_by)
        self.log.info("manual transcript processing queued", extra={"fields": {
            "service": "manual_transcript_recovery", "lecture_id": lecture_id,
            "job_id": job["job_id"] if job else None, "created": created,
            "planned_action": plan["next_action"]}})
        return {"queued": True, "created": created,
                "reason": None if created else ALREADY_QUEUED,
                "reason_message": None if created else PROCESSING_MESSAGES[ALREADY_QUEUED],
                "plan": plan, "job": job}

    def _latest_job(self, connection, lecture_id):
        return (self.jobs.latest_for_lecture(connection, lecture_id)
                if self.jobs.available(connection) else None)

    def plan_processing(self, connection, lecture_id, *, target=None,
                        provider_allowed: bool | None = None) -> dict:
        # `provider_allowed` overrides this entry point's own setting for PLANNING only: the
        # console plans what the background worker will do; it never runs it.
        provider_allowed = self.allow_provider if provider_allowed is None else provider_allowed
        target = target or self.uploads.target(connection, lecture_id)
        uploads = [item for item in target["uploads"] if item.get("is_current_content")]
        state = self.resolver.for_lecture(connection, str(lecture_id))
        action = state["next_executable_action"]
        stages = state["stages"]
        reason = None
        if not uploads:
            reason = NO_MANUAL_UPLOAD
        elif not target["all_parts_stored"]:
            reason = WAITING_FOR_TRANSCRIPT_PARTS
        elif action == MANUAL_REVIEW_REQUIRED:
            reason = (stages.get(state["blocking_stage"], {}).get("reason")
                      or MANUAL_REVIEW_REQUIRED)
        elif action in IDLE_ACTIONS or action == NOTHING_TO_DO:
            reason = NOTHING_TO_PROCESS
        elif action in PROVIDER_ACTIONS and not provider_allowed:
            reason = QA_RUNS_ON_THE_SERVER
        qa_settled = stages.get(QA_EVALUATION, {}).get("state") in (COMPLETE, NOT_APPLICABLE)
        recording = stages.get(RECORDING_LINK, {})
        return {
            "available": reason is None,
            "reason": reason,
            "reason_message": PROCESSING_MESSAGES.get(reason) if reason else None,
            "lecture_id": state["lecture_id"],
            "session_date": state["session_date"],
            "next_action": action,
            "blocking_stage": state["blocking_stage"],
            "qa_state": stages.get(QA_EVALUATION, {}).get("state"),
            # The paid step is still ahead and this entry point will not take it.
            "qa_runs_on_the_server": not qa_settled and not provider_allowed,
            "server_command": SERVER_COMMAND.format(lecture_id=state["lecture_id"]),
            "stages_preserved": [name for name in STAGE_ORDER
                                 if stages.get(name, {}).get("state") in (COMPLETE, NOT_APPLICABLE)],
            "cost": {
                "scope": "LECTURE",
                "affects_other_lectures_that_day": False,
                # Only the lecture-scoped recording link can call Graph here, and only when it is armed.
                "calls_microsoft_graph": recording.get("action") == "LINK_RECORDING",
                "buys_model_generation": not qa_settled and provider_allowed,
                # The guarded QA / Perfect sync, as the scheduler performs it, when still pending.
                "writes_legacy_production_row": any(
                    stages.get(name, {}).get("state") not in (COMPLETE, NOT_APPLICABLE)
                    for name in ("LEGACY_QA_SYNC", "PERFECT_SYNC")),
            },
            "last_processing": (uploads[-1].get("processing") if uploads else None),
        }

    # -- the one write path ------------------------------------------------------------------------
    def process(self, connection, lecture_id, *, entry_point: str | None = None) -> dict:
        """
        Resume this lecture's pipeline through the real orchestrator. The caller commits.
        Called by the background worker (entry point QUEUE) and the server CLI.
        """
        lecture_id = str(lecture_id)
        plan = self.plan_processing(connection, lecture_id)
        if not plan["available"]:
            raise ProcessingRefused(plan["reason"], plan)
        started_at = self.clock()
        self.log.info("manual transcript processing", extra={"fields": {
            "service": "manual_transcript_recovery", "lecture_id": lecture_id,
            "planned_action": plan["next_action"]}})
        orchestrator = self.orchestrator_factory(lecture_id)
        summary = orchestrator.run_window(
            connection, _date.fromisoformat(plan["session_date"]), run_type=RUN_TYPE_MANUAL,
            lecture_ids=[lecture_id])
        item = next((row for row in summary.get("lectures", [])
                     if row["lecture_id"] == lecture_id), {})
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
            "graph_calls": summary.get("graph_calls", 0),
            "entry_point": entry_point or ("CLI" if self.allow_provider else "CONSOLE"),
            # Deterministic stages are done; the quality evaluation waits for the server command.
            "qa_pending_on_server": (final["next_executable_action"] in PROVIDER_ACTIONS
                                     and not self.allow_provider),
        }
        self.repository.record_processing(connection, lecture_id, outcome)
        return {"lecture_id": lecture_id, "plan": plan, "processing": outcome, "run": summary}
