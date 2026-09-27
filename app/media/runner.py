"""
The Positive Moments media runner: `python -m app.cli.media media-runner`.

One process, concurrency 1, two kinds of work, both claimed atomically:

  JOBS  QUEUED            -> fresh Graph download URL -> provider submit -> RENDERING
        RENDERING (due)   -> provider status (webhook backup) -> UPLOAD_PENDING
        UPLOAD_PENDING    -> stream MP4 to one temp file -> Graph upload session
                             -> positive_moment_media_assets -> COMPLETED
        FAILED_RETRYABLE  -> back to its resume stage, with backoff, bounded
  RUNS  PREVIEW / ANALYZE / RENDER / RETRY_FAILED, one lecture per step,
        checkpointed in positive_moment_run_items so a restart resumes

Money is spent only on jobs an operator moved to QUEUED. A restart never
queues anything.

Double-spend guard: `submitted_at` is committed BEFORE the provider call. A
QUEUED job found with `submitted_at` set and no render id means a previous
process died mid-submit; its outcome is unknown, so it is parked as
FAILED_FINAL / SUBMIT_OUTCOME_UNKNOWN for an explicit operator retry instead
of being silently submitted twice.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from app.db.repositories.media_platform import retry_delay
from app.media.delivery import DeliveryError, output_filename, remove_quietly
from app.media.render.base import (
    FAILED,
    SUCCEEDED,
    RenderPolicy,
    RenderProviderError,
    RenderRequest,
)

RUNNER_VERSION = "positive_moments_media_runner_v1"
POLL_SECONDS = 60
MAX_POLL_SECONDS = 300
RENDER_TIMEOUT = timedelta(hours=3)
SOURCE_DURATION_TOLERANCE_SECONDS = 2.0

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


class MediaJobWorker:
    """Advances ONE claimed job by one step. Every write is compare-and-set."""

    def __init__(self, *, media_repository, moments_repository, provider, sharepoint,
                 destination_drive_id, destination_folder_item_id, webhook_url_for=None,
                 temp_dir=None):
        self.media = media_repository
        self.moments = moments_repository
        self.provider = provider
        self.sharepoint = sharepoint
        self.destination_drive_id = destination_drive_id
        self.destination_folder_item_id = destination_folder_item_id
        self.webhook_url_for = webhook_url_for
        self.temp_dir = temp_dir

    def step(self, connection, job: dict) -> str:
        status = job["status"]
        try:
            if status == "QUEUED":
                return self.submit(connection, job)
            if status == "RENDERING":
                return self.poll(connection, job)
            if status in ("RENDER_SUCCEEDED", "UPLOAD_PENDING", "UPLOADING"):
                return self.upload(connection, job)
            if status == "FAILED_RETRYABLE":
                return self.rearm(connection, job)
            return "NOOP"
        finally:
            self.media.release(connection, job["job_id"])

    # -- failure handling -----------------------------------------------------------

    def _fail(self, connection, job, *, stage, code, message, retryable, resume):
        attempts = job["attempt_count"] + 1
        exhausted = attempts >= job["max_attempts"]
        final = not retryable or exhausted
        self.media.transition(
            connection, job["job_id"], expected=job["status"],
            status="FAILED_FINAL" if final else "FAILED_RETRYABLE",
            event_stage=stage, event_detail={"code": code, "attempt": attempts},
            error_stage=stage, error_code=("ATTEMPTS_EXHAUSTED" if exhausted and retryable
                                           else code),
            error_message=str(message)[:500], attempt_count=attempts, resume_stage=resume,
            next_retry_at=None if final else _now() + retry_delay(attempts))
        return "FAILED_FINAL" if final else "FAILED_RETRYABLE"

    def rearm(self, connection, job) -> str:
        target = {"SUBMIT": "QUEUED", "POLL": "RENDERING",
                  "UPLOAD": "UPLOAD_PENDING"}.get(job["resume_stage"] or "SUBMIT", "QUEUED")
        extra = {"submitted_at": None, "provider_render_id": None} if target == "QUEUED" else {}
        if target == "RENDERING":
            extra["next_poll_at"] = _now()
        self.media.transition(connection, job["job_id"], expected="FAILED_RETRYABLE",
                              status=target, event_stage="RETRY", next_retry_at=None, **extra)
        return target

    # -- submit ---------------------------------------------------------------------------

    def submit(self, connection, job) -> str:
        if job["provider_render_id"]:
            # Already submitted (a webhook or poll will move it on).
            self.media.transition(connection, job["job_id"], expected="QUEUED",
                                  status="RENDERING", next_poll_at=_now())
            return "RENDERING"
        if job["submitted_at"] is not None:
            self.media.transition(
                connection, job["job_id"], expected="QUEUED", status="FAILED_FINAL",
                event_stage="RENDER_SUBMIT", error_stage="RENDER_SUBMIT",
                error_code="SUBMIT_OUTCOME_UNKNOWN",
                error_message="a previous submit did not record its outcome; check the "
                              "provider dashboard, then Retry",
                resume_stage="SUBMIT")
            return "FAILED_FINAL"
        if self.media.asset_by_plan(connection, job["plan_fingerprint"]):
            self.media.transition(connection, job["job_id"], expected="QUEUED",
                                  status="COMPLETED", event_stage="COMPLETE",
                                  event_detail={"reused_existing_asset": True})
            return "COMPLETED"
        try:
            source = self.sharepoint.source_file(job["source_drive_id"], job["source_item_id"])
        except DeliveryError as error:
            return self._fail(connection, job, stage="RENDER_SUBMIT", code=error.code,
                              message=str(error), retryable=error.retryable, resume="SUBMIT")
        expected = float(job["source_duration_seconds"] or 0)
        if source.duration_seconds and expected and abs(
                source.duration_seconds - expected) > SOURCE_DURATION_TOLERANCE_SECONDS:
            # The file changed since the offset was proven. Never cut it blind.
            self.media.transition(connection, job["job_id"], expected="QUEUED",
                                  status="REVIEW_REQUIRED_ALIGNMENT",
                                  event_stage="RENDER_SUBMIT", error_stage="ALIGNMENT",
                                  error_code="SOURCE_CHANGED_SINCE_ALIGNMENT")
            return "REVIEW_REQUIRED_ALIGNMENT"
        start = float(job["actual_media_start_seconds"])
        duration = float(job["actual_media_end_seconds"]) - start
        policy = RenderPolicy(**{k: v for k, v in (job["render_policy"] or {}).items()
                                 if k in RenderPolicy.__dataclass_fields__})
        request = RenderRequest(
            job_id=str(job["job_id"]), source_url=source.download_url,
            trim_start_seconds=start, trim_duration_seconds=duration, policy=policy,
            webhook_url=self.webhook_url_for(job["job_id"]) if self.webhook_url_for else None)
        # Commit the intent first: see the module docstring.
        marked = self.media.transition(connection, job["job_id"], expected="QUEUED",
                                       status="QUEUED", event_stage="RENDER_SUBMIT",
                                       submitted_at=_now())
        if not marked:
            return "NOOP"
        connection.commit()
        try:
            rendered = self.provider.submit(request)
        except RenderProviderError as error:
            self.media.transition(connection, job["job_id"], expected="QUEUED",
                                  status="QUEUED", submitted_at=None, event_stage="RENDER_SUBMIT",
                                  event_detail={"code": error.code})
            job = {**job, "submitted_at": None}
            return self._fail(connection, job, stage="RENDER_SUBMIT", code=error.code,
                              message=str(error), retryable=error.retryable, resume="SUBMIT")
        self.media.transition(connection, job["job_id"], expected="QUEUED",
                              status="RENDERING", event_stage="RENDER_SUBMITTED",
                              event_detail={"render_id": rendered.render_id},
                              provider_render_id=rendered.render_id,
                              provider_status=rendered.provider_status,
                              next_poll_at=_now() + timedelta(seconds=POLL_SECONDS),
                              error_code=None, error_message=None, error_stage=None)
        return "RENDERING"

    # -- poll (webhook backup) -----------------------------------------------------------

    def poll(self, connection, job) -> str:
        try:
            status = self.provider.get_status(job["provider_render_id"])
        except RenderProviderError as error:
            if not error.retryable:
                return self._fail(connection, job, stage="RENDER_POLL", code=error.code,
                                  message=str(error), retryable=False, resume="POLL")
            self.media.transition(connection, job["job_id"], expected="RENDERING",
                                  status="RENDERING",
                                  next_poll_at=_now() + timedelta(seconds=MAX_POLL_SECONDS))
            return "RENDERING"
        return apply_render_status(self.media, connection, job, status)

    # -- upload --------------------------------------------------------------------------

    def upload(self, connection, job) -> str:
        existing = self.media.asset_by_plan(connection, job["plan_fingerprint"])
        if existing:
            self.media.transition(connection, job["job_id"], expected=job["status"],
                                  status="COMPLETED", event_stage="COMPLETE",
                                  event_detail={"reused_existing_asset": True})
            return "COMPLETED"
        if not self.destination_drive_id or not self.destination_folder_item_id:
            return self._fail(connection, job, stage="UPLOAD",
                              code="SHAREPOINT_DESTINATION_NOT_CONFIGURED",
                              message="MEDIA_SHAREPOINT_DRIVE_ID / folder item id missing",
                              retryable=False, resume="UPLOAD")
        if not self.media.transition(connection, job["job_id"], expected=job["status"],
                                     status="UPLOADING", event_stage="UPLOAD"):
            return "NOOP"
        connection.commit()
        job = {**job, "status": "UPLOADING"}
        path = None
        try:
            status = self.provider.get_status(job["provider_render_id"])
            if status.status != SUCCEEDED or not status.output_url:
                return self._fail(connection, job, stage="UPLOAD", code="RENDER_NOT_AVAILABLE",
                                  message="the provider no longer reports a finished render",
                                  retryable=True, resume="POLL")
            path = self.sharepoint.download_to_temp(status.output_url,
                                                    expected_size=status.size_bytes,
                                                    directory=self.temp_dir)
            moment = self.moments.moment(connection, job["moment_id"]) or {}
            lecture = self.media.lecture(connection, job["lecture_id"]) or {}
            name = output_filename(session_date=str(lecture.get("session_date") or ""),
                                   subject=lecture.get("subject"),
                                   lecture_id=str(job["lecture_id"]),
                                   moment_index=int(moment.get("moment_index") or 0),
                                   plan_fingerprint=job["plan_fingerprint"])
            delivered = self.sharepoint.upload(path, drive_id=self.destination_drive_id,
                                               folder_item_id=self.destination_folder_item_id,
                                               filename=name)
        except RenderProviderError as error:
            return self._fail(connection, job, stage="UPLOAD", code=error.code,
                              message=str(error), retryable=error.retryable, resume="UPLOAD")
        except DeliveryError as error:
            return self._fail(connection, job, stage="UPLOAD", code=error.code,
                              message=str(error), retryable=error.retryable, resume="UPLOAD")
        finally:
            remove_quietly(path)
        asset = self.media.insert_asset(connection, {
            "job_id": job["job_id"], "moment_id": job["moment_id"],
            "lecture_id": job["lecture_id"], "provider": job["provider"],
            "plan_fingerprint": job["plan_fingerprint"],
            "source_drive_id": job["source_drive_id"], "source_item_id": job["source_item_id"],
            "output_filename": delivered.name, "destination_drive_id": delivered.drive_id,
            "destination_item_id": delivered.item_id, "web_url": delivered.web_url,
            "duration_seconds": job["output_duration_seconds"],
            "size_bytes": delivered.size_bytes,
            "verification_status": ("ADOPTED_EXISTING_SAME_SIZE" if delivered.adopted_existing
                                    else "UPLOAD_CONFIRMED_SIZE_MATCH")})
        if asset is None or str(asset["job_id"]) != str(job["job_id"]):
            # A DIFFERENT job already owns this plan's asset or this DriveItem.
            return self._fail(connection, job, stage="UPLOAD", code="ASSET_IDENTITY_CONFLICT",
                              message="an asset for this plan or item already exists",
                              retryable=False, resume="UPLOAD")
        self.media.event(connection, job["job_id"], "UPLOADED", "OK",
                         {"file": delivered.name, "adopted": delivered.adopted_existing})
        self.media.transition(connection, job["job_id"], expected="UPLOADING",
                              status="COMPLETED", event_stage="COMPLETE",
                              error_code=None, error_message=None, error_stage=None)
        return "COMPLETED"


def apply_render_status(media, connection, job: dict, status) -> str:
    """
    Apply a VERIFIED provider status (from our own API call, never a payload).
    Shared by the poll and the webhook; compare-and-set makes them race-safe.
    """
    if status.render_id != str(job["provider_render_id"]):
        return "IGNORED_RENDER_MISMATCH"
    if status.status == SUCCEEDED:
        from app.media.render.base import validate_output
        policy = RenderPolicy(**{k: v for k, v in (job["render_policy"] or {}).items()
                                 if k in RenderPolicy.__dataclass_fields__})
        expected = float(job["actual_media_end_seconds"]) - float(job["actual_media_start_seconds"])
        problems = validate_output(status, expected_duration_seconds=expected, policy=policy)
        if problems:
            media.transition(connection, job["job_id"], expected="RENDERING",
                             status="FAILED_FINAL", event_stage="RENDER_COMPLETED",
                             event_detail={"problems": problems}, error_stage="RENDER_VALIDATION",
                             error_code=problems[0], provider_status=status.provider_status)
            return "FAILED_FINAL"
        moved = media.transition(connection, job["job_id"], expected="RENDERING",
                                 status="UPLOAD_PENDING", event_stage="RENDER_COMPLETED",
                                 event_detail={"duration": status.duration_seconds,
                                               "size": status.size_bytes},
                                 provider_status=status.provider_status,
                                 render_completed_at=_now(),
                                 output_duration_seconds=status.duration_seconds,
                                 output_size_bytes=status.size_bytes)
        return "UPLOAD_PENDING" if moved else "NOOP"
    if status.status == FAILED:
        attempts = job["attempt_count"] + 1
        final = attempts >= job["max_attempts"]
        media.transition(connection, job["job_id"], expected="RENDERING",
                         status="FAILED_FINAL" if final else "FAILED_RETRYABLE",
                         event_stage="RENDER_FAILED",
                         event_detail={"provider_status": status.provider_status},
                         error_stage="RENDER", error_code="RENDER_FAILED_AT_PROVIDER",
                         error_message=(status.error_message or "")[:300] or None,
                         provider_status=status.provider_status, attempt_count=attempts,
                         resume_stage="SUBMIT",
                         next_retry_at=None if final else _now() + retry_delay(attempts))
        return "FAILED_FINAL" if final else "FAILED_RETRYABLE"
    submitted = job.get("submitted_at")
    if submitted and _now() - submitted > RENDER_TIMEOUT:
        media.transition(connection, job["job_id"], expected="RENDERING", status="FAILED_FINAL",
                         event_stage="RENDER", error_stage="RENDER", error_code="RENDER_TIMEOUT",
                         provider_status=status.provider_status, resume_stage="SUBMIT")
        return "FAILED_FINAL"
    media.transition(connection, job["job_id"], expected="RENDERING", status="RENDERING",
                     provider_status=status.provider_status,
                     next_poll_at=_now() + timedelta(seconds=POLL_SECONDS))
    return "RENDERING"


class MediaRunProcessor:
    """Processes operator batch runs, one lecture per step."""

    def __init__(self, *, media_repository, media_service):
        self.media = media_repository
        self.service = media_service

    def _process(self, connection, run, lecture_id, mode):
        outcome = self.service.process(
            connection, lecture_id, analyze=mode in ("ANALYZE", "RETRY_FAILED"),
            allow_model=mode in ("ANALYZE", "RETRY_FAILED"),
            persist_jobs=mode != "PREVIEW", run_id=run["run_id"])
        detail = {"analysis": outcome.analysis_outcome, "jobs_planned": outcome.jobs_planned}
        if mode == "RENDER":
            detail["queued"] = self.service.queue_render(connection, lecture_id=lecture_id)
        if mode == "RETRY_FAILED":
            detail["rearmed"] = self.service.retry_failed(connection, lecture_id=lecture_id)
        if mode in ("RENDER", "RETRY_FAILED"):
            outcome = self.service.process(connection, lecture_id, analyze=False,
                                           allow_model=False, persist_jobs=True,
                                           run_id=run["run_id"])
        self.media.save_state(connection, outcome.state)
        return outcome, detail

    def lectures(self, connection, run) -> list[str]:
        if run["lecture_id"]:
            return [str(run["lecture_id"])]
        return self.media.lectures_in_range(connection, run["requested_from"],
                                            run["requested_to"])

    def step(self, connection, run, *, runner: str) -> bool:
        """Process the next unfinished lecture. False when the run is finished."""
        lectures = self.lectures(connection, run)
        done = self.media.run_items_done(connection, run["run_id"])
        remaining = [lecture for lecture in lectures if lecture not in done]
        counts = dict(run.get("counts") or {})
        if not remaining:
            self.media.heartbeat_run(connection, run["run_id"], runner=runner,
                                     total=len(lectures), processed=len(done), counts=counts)
            self.media.finish_run(connection, run["run_id"], status="COMPLETED")
            return False
        lecture_id = remaining[0]
        mode = run["mode"]
        try:
            # A savepoint: one lecture's failure undoes that lecture only.
            with connection.transaction():
                outcome, detail = self._process(connection, run, lecture_id, mode)
            status, result = "DONE", outcome.state.get("analysis_state")
        except Exception as exc:              # noqa: BLE001 - one lecture never stops the run
            log.warning("media run lecture failed", extra={
                "run_id": str(run["run_id"]), "lecture_id": lecture_id,
                "error_type": type(exc).__name__})
            status, result = "DONE", "ERROR"
            detail = {"error_type": type(exc).__name__,
                      "error_code": getattr(exc, "code", None)}
        self.media.record_run_item(connection, run["run_id"], lecture_id, status=status,
                                   outcome=result, detail=detail)
        counts[result or "UNKNOWN"] = counts.get(result or "UNKNOWN", 0) + 1
        self.media.heartbeat_run(connection, run["run_id"], runner=runner, total=len(lectures),
                                 processed=len(done) + 1, counts=counts)
        run["counts"] = counts
        return True


class MediaRunner:
    """The long-running loop. Jobs first (they are already paid for), then runs."""

    def __init__(self, *, connection_factory, worker_factory, run_processor_factory,
                 media_repository, runner_id: str | None = None):
        self.connection_factory = connection_factory
        self.worker_factory = worker_factory
        self.run_processor_factory = run_processor_factory
        self.media = media_repository
        self.runner_id = runner_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = False

    def request_stop(self, *_):
        self._stop = True

    def once(self) -> str:
        """One unit of work. Returns what was done."""
        with self.connection_factory() as connection:
            connection.prepare_threshold = None
            job = self.media.claim_job(connection, self.runner_id)
            connection.commit()
            if job is not None:
                result = self.worker_factory().step(connection, job)
                connection.commit()
                return f"JOB:{result}"
            run = self.media.claim_run(connection, self.runner_id, RUNNER_VERSION)
            connection.commit()
            if run is not None:
                self.run_processor_factory().step(connection, run, runner=self.runner_id)
                connection.commit()
                return "RUN_STEP"
        return "IDLE"

    def run_forever(self, *, poll_seconds: int = 10, max_iterations=None, sleep=time.sleep):
        iterations, work = 0, 0
        while not self._stop:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            try:
                result = self.once()
            except Exception as exc:          # noqa: BLE001 - the loop must survive
                log.error("media runner iteration failed",
                          extra={"error_type": type(exc).__name__})
                sleep(poll_seconds)
                continue
            if result == "IDLE":
                sleep(poll_seconds)
            else:
                work += 1
        return {"iterations": iterations, "work_units": work, "runner_id": self.runner_id}
