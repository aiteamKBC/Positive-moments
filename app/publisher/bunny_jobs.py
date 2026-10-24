"""
P0-D Bunny tracks, one recording part at a time, repository-backed:

    IMPORT_VIDEO     gate -> persist intent -> Bunny token check -> import slot ->
                     FRESH Graph download URL -> Bunny URL Fetch -> identify by token
    POLL_ENCODING    Bunny status -> media verification -> (confirmed failure) audited cleanup
    PUBLISH_CAPTION  the part's own raw WebVTT -> sanitized copy -> Caption API -> read-back

Every worker step does at most ONE external write, persists its intent first,
and returns instead of sleeping (WAITING / RETRY with a delay): encoding
monitoring never blocks transcript acquisition, recording discovery or other
queued work. No transaction is open during a Graph or Bunny call. The MP4
travels Microsoft -> Bunny directly; the temporary URL exists only in one stack
frame and is dropped right after the Fetch call.

UNCERTAIN OUTCOMES. A Fetch whose outcome is unknown (timeout, reset, 5xx) is
SUBMISSION_UNCERTAIN: it is only ever RECONCILED (found by its deterministic
title token), never resubmitted, deleted or re-imported automatically. When
reconciliation cannot settle it, it waits for an operator.

CONFIRMED FAILED IMPORT CLEANUP. Bunny reporting status Error / UploadFailed
for an identified video is a confirmed terminal failure. Such a video is
deleted automatically ONLY when every one of these holds: the exact video id
is known; it was never verified; it is HIDDEN; no current caption or LMS
mapping points to it; and the failure and the intended deletion are audited
(events committed) BEFORE the delete call. The row is then retired (never
deleted) and the next generation is imported with a fresh URL. A delete that
fails or cannot be confirmed preserves the video id and stops for review -
another copy is never created on top of it.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from app.bunny import captions as cap
from app.bunny import tokens
from app.bunny.client import FAILED_STATUSES, FINISHED, STATUS_NAMES, BunnyError
from app.bunny.identity import build_publisher_title, caption_publication_id, recording_identity, video_record_id
from app.bunny.verification import media_checks
from app.common.errors import PlatformError
from app.db.repositories.bunny_publisher import BunnyCaptionRepository, BunnyVideoRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher import recording_mapping as rm
from app.publisher.progress import EXCLUDE, INCLUDE, NEEDS_DECISION, load_facts, readiness

WORKER_VERSION = "publisher_bunny_v1"

DONE, WAITING, RETRY, DEFERRED, REVIEW, NOT_APPLICABLE, PERMANENT = (
    "DONE", "WAITING", "RETRY", "DEFERRED", "REVIEW", "NOT_APPLICABLE", "PERMANENT")
IMPORT_VIDEO, POLL_ENCODING, PUBLISH_CAPTION = "IMPORT_VIDEO", "POLL_ENCODING", "PUBLISH_CAPTION"
IN_SUBMISSION = ("SUBMITTING", "SUBMITTED", "SUBMISSION_UNCERTAIN")
DELETE_REASON = "CONFIRMED_IMPORT_FAILURE"

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobOutcome:
    lecture_id: str
    kind: str
    reason: str
    retry_after: timedelta | None = None
    next_refresh_at: datetime | None = None
    follow_ups: list = field(default_factory=list)        # [(job_kind, part_index)]
    detail: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        """Identity and codes only - never a URL, key, file content or caption text."""
        return {"kind": self.kind, "reason": self.reason, "worker_version": WORKER_VERSION, **self.detail}


@dataclass
class BunnyRuntime:
    """What every Bunny worker shares. `bunny` is bound to config.library_id."""
    config: object
    connect: object
    bunny: object
    media: object = None              # SharePointMedia-like: source_file(drive, item) -> fresh URL
    graph: object = None              # metadata reads (source height)
    encoding_tier: object = None      # verified at startup from the library settings
    clock: object = utcnow
    open_url: object = None           # caption CDN read-back; None = registration check only
    videos: BunnyVideoRepository = field(default_factory=BunnyVideoRepository)
    captions: BunnyCaptionRepository = field(default_factory=BunnyCaptionRepository)
    queue: PublisherQueueRepository = field(default_factory=PublisherQueueRepository)

    @property
    def library_id(self) -> int:
        return int(self.config.library_id)


class _Stop(Exception):
    def __init__(self, outcome: JobOutcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


def _cause(exc):
    return exc.__cause__ if isinstance(exc, PlatformError) and exc.__cause__ is not None else exc


class _Worker:
    job_kind = ""

    def __init__(self, runtime: BunnyRuntime):
        self.rt = runtime

    def run(self, lecture_id, part_index: int, *, attempt: int = 1, operator: bool = False) -> JobOutcome:
        lecture_id = str(lecture_id)
        if not self.rt.config.writes_bunny or self.rt.config.library_id is None:
            return JobOutcome(lecture_id, NOT_APPLICABLE, "SHADOW_MODE_NO_BUNNY_WRITES")
        try:
            connection = self.rt.connect()
            connection.autocommit = True
        except Exception as exc:  # noqa: BLE001 - any connect failure is a database outage
            return JobOutcome(lecture_id, RETRY, f"DATABASE_UNAVAILABLE:{type(exc).__name__}",
                              retry_after=_backoff(attempt))
        try:
            return self._run(connection, lecture_id, int(part_index), attempt, operator)
        except _Stop as stop:
            return stop.outcome
        except (psycopg.Error, PlatformError) as exc:
            cause = _cause(exc)
            reason = ("LOCK_TIMEOUT" if isinstance(cause, psycopg.errors.LockNotAvailable)
                      else f"DATABASE_ERROR:{type(cause).__name__}")
            return JobOutcome(lecture_id, RETRY, reason, retry_after=_backoff(attempt))
        except BunnyError as error:
            # A READ that failed (writes are handled where they happen).
            if error.retryable or error.status is None or (error.status or 0) >= 500 or error.status == 429:
                return JobOutcome(lecture_id, RETRY, f"BUNNY_{error.code}", retry_after=_backoff(attempt))
            return JobOutcome(lecture_id, REVIEW, f"BUNNY_{error.code}")
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def _run(self, connection, lecture_id, part_index, attempt, operator) -> JobOutcome:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------------------
    def _event(self, connection, name, lecture_id, **detail):
        self.rt.queue.event(connection, name, lecture_id=lecture_id, detail={"worker_version": WORKER_VERSION,
                                                                             **detail})

    def _video(self, connection, lecture_id, part_index):
        with connection.transaction():
            return self.rt.videos.live_for_part(connection, self.rt.library_id, lecture_id, part_index)

    def _set(self, connection, row, *, expect, event=None, **changes) -> dict:
        """One committed compare-and-set (+ its audit event). A lost race stops the step."""
        with connection.transaction():
            new = self.rt.videos.transition(connection, row["bunny_video_record_id"], expect=expect, **changes)
            if new is None:
                raise _Stop(JobOutcome(str(row["lecture_id"]), DEFERRED, "CONCURRENT_CHANGE",
                                       retry_after=timedelta(minutes=1)))
            if event:
                self._event(connection, event, row["lecture_id"], part=row["recording_part_index"],
                            record_id=str(row["bunny_video_record_id"]),
                            bunny_video_id=str(new["bunny_video_id"]) if new["bunny_video_id"] else None,
                            import_status=new["import_status"], encoding_status=new["encoding_status"])
        return new

    def _review_row(self, connection, row, code, *, column="import_status"):
        return self._set(connection, row, expect={column: row[column]}, event=f"VIDEO_{column.split('_')[0].upper()}_REVIEW",
                         last_error_code=code, **{column: "REVIEW_REQUIRED"})

    def _matches(self, connection, row) -> tuple[list, list]:
        """Bunny videos carrying this row's identity token (retired generations excluded), and exact titles."""
        with connection.transaction():
            retired = self.rt.videos.retired_video_ids(connection, self.rt.library_id, row["recording_identity"])
        found = [v for v in self.rt.bunny.find_videos_with_token(row["recording_identity"])
                 if str(v.get("guid") or "").lower() not in retired]
        return found, [v for v in found if v.get("title") == row["video_title"]]


def _backoff(attempt: int) -> timedelta:
    steps = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
    return steps[attempt - 1] if 1 <= attempt <= len(steps) else timedelta(hours=3)


def _cdn_hostname(cfg) -> str:
    """The CDN host of the library this process writes to ("" = no content read-back)."""
    host = getattr(cfg, "cdn_hostname", None)
    if isinstance(host, str):
        return host
    return getattr(cfg, "staging_cdn_hostname", "") if getattr(cfg, "mode", "") == "STAGING_WRITE" else ""


def _status_name(code) -> str:
    return STATUS_NAMES.get(code, str(code)).upper()


# =================================================================================== IMPORT
class ImportWorker(_Worker):
    job_kind = IMPORT_VIDEO

    def _run(self, connection, lecture_id, part_index, attempt, operator):
        with connection.transaction():
            facts = load_facts(connection, lecture_id)
        row = self._video(connection, lecture_id, part_index)
        gate = self._gate(facts, part_index)
        if row is None:
            if gate is not None:
                return gate
            row = self._create(connection, facts, part_index)
        status = row["import_status"]
        if status == "IDENTIFIED":
            return self._identified_outcome(row, "ALREADY_IDENTIFIED")
        if status in ("FAILED", "REVIEW_REQUIRED"):
            if not operator:
                return JobOutcome(lecture_id, REVIEW, row["last_error_code"] or f"VIDEO_{status}",
                                  detail={"part": part_index, "retained": True})
            return self._operator(connection, row, facts, attempt)
        if status in IN_SUBMISSION:
            return self._reconcile(connection, row)
        if gate is not None:                       # re-checked before ANY Bunny write
            return gate
        return self._submit(connection, row, attempt)

    def _gate(self, facts, part_index) -> JobOutcome | None:
        lid = facts.lecture_id
        if not facts.exists:
            return JobOutcome(lid, PERMANENT, "LECTURE_NOT_FOUND")
        if facts.cancelled or facts.suppressed:
            return JobOutcome(lid, NOT_APPLICABLE, "LECTURE_CANCELLED" if facts.cancelled else "LI_DUPLICATE")
        if facts.duplicate_unresolved:
            # A duplicate loser or an ambiguous group: never imported, left for review.
            return JobOutcome(lid, REVIEW, facts.duplicate_block,
                              detail={"duplicate_case": facts.duplicate_case, "duplicate_role": facts.duplicate_role})
        if facts.lms_status != "VERIFIED" or not facts.lms_occurrence_id:
            return JobOutcome(lid, WAITING, f"LMS_NOT_VERIFIED:{facts.lms_status}", retry_after=timedelta(minutes=30))
        if facts.recording_status != "LOCATED":
            return JobOutcome(lid, WAITING, f"RECORDING_NOT_LOCATED:{facts.recording_status}",
                              retry_after=timedelta(minutes=30))
        part = facts.part(part_index)
        if part is None:
            return JobOutcome(lid, PERMANENT, "NO_SUCH_RECORDING_PART")
        policy = facts.policy(part)
        if policy == EXCLUDE:
            return JobOutcome(lid, NOT_APPLICABLE, "PART_EXCLUDED_BY_OPERATOR")
        if policy == NEEDS_DECISION:
            return JobOutcome(lid, REVIEW, "FRAGMENT_NEEDS_DECISION", detail={"part": part_index})
        if policy != INCLUDE:
            return JobOutcome(lid, WAITING, "PART_NOT_LOCATED", retry_after=timedelta(minutes=30))
        return None

    def _create(self, connection, facts, part_index) -> dict:
        lib, part = self.rt.library_id, facts.part(part_index)
        drive, item = part["drive_id"], part["item_id"]
        with connection.transaction():
            generation = self.rt.videos.generations(connection, lib, drive, item) + 1
            if generation > self.rt.config.max_import_generations:
                raise _Stop(JobOutcome(facts.lecture_id, REVIEW, "IMPORT_GENERATIONS_EXHAUSTED",
                                       detail={"part": part_index}))
            identity = recording_identity(drive, item)
            title = build_publisher_title(facts.subject, facts.session_date, facts.lms_occurrence_id, identity,
                                          part_index=part_index, part_count=max(len(facts.parts), part_index))
            row = self.rt.videos.create(
                connection, record_id=video_record_id(lib, drive, item, generation), lecture_id=facts.lecture_id,
                part=part_index, drive=drive, item=item, identity=identity, library=lib, title=title,
                file_name=part["file_name"], size=part["size_bytes"], duration=part["duration_seconds"],
                metadata={"generation": generation, "lms_occurrence_id": str(facts.lms_occurrence_id),
                          "part_count": len(facts.parts), "classification": part["classification"]})
            if row is None:
                other = self.rt.videos.live_for_source(connection, lib, drive, item)
                mine = self.rt.videos.live_for_part(connection, lib, facts.lecture_id, part_index)
                if mine is not None:
                    return mine                              # a concurrent worker created it
                raise _Stop(JobOutcome(facts.lecture_id, REVIEW, "SOURCE_ALREADY_IMPORTED", detail={
                    "part": part_index, "owner_lecture_id": str(other["lecture_id"]) if other else None}))
            self._event(connection, "VIDEO_INTENT_RECORDED", facts.lecture_id, part=part_index,
                        record_id=str(row["bunny_video_record_id"]), generation=generation, identity=identity)
        return row

    def _submit(self, connection, row, attempt) -> JobOutcome:
        lid, part = str(row["lecture_id"]), row["recording_part_index"]
        found, exact = self._matches(connection, row)
        if found:
            if len(found) == 1 and len(exact) == 1:
                return self._identify(connection, row, exact[0], event="VIDEO_ADOPTED")
            self._review_row(connection, row, "BUNNY_TOKEN_AMBIGUOUS")
            return JobOutcome(lid, REVIEW, "BUNNY_TOKEN_AMBIGUOUS", detail={"part": part, "matches": len(found)})
        # Claim an import slot and record the submission intent in ONE transaction.
        with connection.transaction():
            self.rt.videos.lock_slots(connection, self.rt.library_id)
            in_flight = self.rt.videos.in_flight(connection, self.rt.library_id)
            if in_flight >= self.rt.config.max_imports_in_flight:
                return JobOutcome(lid, DEFERRED, "IMPORT_SLOTS_FULL", retry_after=timedelta(minutes=2),
                                  detail={"part": part, "in_flight": in_flight})
            claimed = self.rt.videos.transition(connection, row["bunny_video_record_id"],
                                                expect={"import_status": "PENDING"}, import_status="SUBMITTING",
                                                attempt_count=int(row["attempt_count"]) + 1,
                                                import_submitted_at=self.rt.clock())
            if claimed is None:
                return JobOutcome(lid, DEFERRED, "CONCURRENT_CHANGE", retry_after=timedelta(minutes=1))
            self._event(connection, "FETCH_SUBMITTING", lid, part=part, record_id=str(row["bunny_video_record_id"]),
                        attempt=claimed["attempt_count"])
        row = claimed
        # A FRESH temporary URL, immediately before the submit, held in this frame only.
        try:
            source = self.rt.media.source_file(row["recording_drive_id"], row["recording_item_id"])
        except Exception as error:  # noqa: BLE001 - nothing has been submitted: safe to go back
            code = str(getattr(error, "code", type(error).__name__))[:80]
            retryable = bool(getattr(error, "retryable", True))
            self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_SOURCE_UNAVAILABLE",
                      import_status="PENDING", last_error_code=code)
            return JobOutcome(lid, RETRY if retryable else REVIEW, f"SOURCE_URL_UNAVAILABLE:{code}",
                              retry_after=_backoff(attempt) if retryable else None, detail={"part": part})
        if str(source.item_id) != str(row["recording_item_id"]) or (
                source.size_bytes and row["source_size_bytes"]
                and int(source.size_bytes) != int(row["source_size_bytes"])):
            source = None
            self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_SOURCE_CHANGED",
                      import_status="REVIEW_REQUIRED", last_error_code="SOURCE_CHANGED")
            return JobOutcome(lid, REVIEW, "SOURCE_CHANGED", detail={"part": part})
        try:
            self.rt.bunny.fetch_video(source.download_url, title=row["video_title"])
        except BunnyError as error:
            if error.uncertain:
                self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_UNCERTAIN",
                          import_status="SUBMISSION_UNCERTAIN", last_error_code=error.code)
                return JobOutcome(lid, WAITING, "FETCH_UNCERTAIN_RECONCILING", retry_after=timedelta(minutes=2),
                                  detail={"part": part})
            if error.status in (401, 403):
                self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_FORBIDDEN",
                          import_status="REVIEW_REQUIRED", last_error_code=error.code)
                return JobOutcome(lid, REVIEW, f"FETCH_FORBIDDEN:{error.status}", detail={"part": part})
            # A definite refusal: no video was created, so a bounded retry is safe.
            exhausted = int(row["attempt_count"]) >= 3
            self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_REFUSED",
                      import_status="REVIEW_REQUIRED" if exhausted else "PENDING", last_error_code=error.code)
            return JobOutcome(lid, REVIEW if exhausted else RETRY, f"FETCH_REFUSED:{error.code}",
                              retry_after=None if exhausted else _backoff(attempt), detail={"part": part})
        finally:
            source = None                          # the temporary URL is dropped immediately
        self._set(connection, row, expect={"import_status": "SUBMITTING"}, event="FETCH_SUBMITTED",
                  import_status="SUBMITTED", last_error_code=None)
        return JobOutcome(lid, WAITING, "FETCH_SUBMITTED_IDENTIFYING", retry_after=timedelta(minutes=1),
                          detail={"part": part})

    def _reconcile(self, connection, row) -> JobOutcome:
        """SUBMITTING / SUBMITTED / SUBMISSION_UNCERTAIN: find the fetched video by token; never resubmit."""
        lid, part = str(row["lecture_id"]), row["recording_part_index"]
        found, exact = self._matches(connection, row)
        if len(found) == 1 and len(exact) == 1:
            return self._identify(connection, row, exact[0], event="VIDEO_IDENTIFIED")
        if found:
            self._review_row(connection, row, "AMBIGUOUS_FETCHED_VIDEO")
            return JobOutcome(lid, REVIEW, "AMBIGUOUS_FETCHED_VIDEO", detail={"part": part, "matches": len(found)})
        window = (self.rt.config.identify_timeout if row["import_status"] == "SUBMITTED"
                  else self.rt.config.uncertain_reconcile_window)
        since = row["import_submitted_at"] or self.rt.clock()
        if self.rt.clock() - since > window:
            code = ("IDENTIFY_TIMEOUT" if row["import_status"] == "SUBMITTED"
                    else "UNCERTAIN_SUBMISSION_UNRESOLVED")
            self._review_row(connection, row, code)
            return JobOutcome(lid, REVIEW, code, detail={"part": part})
        return JobOutcome(lid, WAITING, f"RECONCILING_{row['import_status']}", retry_after=timedelta(minutes=2),
                          detail={"part": part})

    def _identify(self, connection, row, video, *, event) -> JobOutcome:
        status = video.get("status")
        try:
            row = self._set(connection, row, expect={"import_status": row["import_status"]}, event=event,
                            import_status="IDENTIFIED", bunny_video_id=str(video["guid"]).lower(),
                            video_identified_at=self.rt.clock(), bunny_status_code=status,
                            encoding_status="FINISHED" if status == FINISHED else "IN_PROGRESS",
                            last_error_code=None,
                            history={"at": self.rt.clock().isoformat(), "status": status})
        except PlatformError as exc:
            if isinstance(_cause(exc), psycopg.errors.UniqueViolation):
                return JobOutcome(str(row["lecture_id"]), REVIEW, "BUNNY_VIDEO_ALREADY_MAPPED")
            raise
        except psycopg.errors.UniqueViolation:
            return JobOutcome(str(row["lecture_id"]), REVIEW, "BUNNY_VIDEO_ALREADY_MAPPED")
        try:
            self.rt.bunny.set_meta_tags(str(row["bunny_video_id"]), {
                "kbc_lecture_id": str(row["lecture_id"]), "kbc_recording_identity": row["recording_identity"],
                "kbc_record_id": str(row["bunny_video_record_id"]), "kbc_part": str(row["recording_part_index"]),
                "kbc_lms_occurrence_id": str((row["metadata"] or {}).get("lms_occurrence_id") or "")})
        except BunnyError as error:
            log.warning("metaTags not set (%s); the PostgreSQL mapping is authoritative", error.code)
        return self._identified_outcome(row, "VIDEO_IDENTIFIED")

    def _identified_outcome(self, row, reason) -> JobOutcome:
        return JobOutcome(str(row["lecture_id"]), DONE, reason, follow_ups=[(POLL_ENCODING, row["recording_part_index"])],
                          detail={"part": row["recording_part_index"],
                                  "bunny_video_id": str(row["bunny_video_id"])})

    def _operator(self, connection, row, facts, attempt) -> JobOutcome:
        """An authenticated operator asked to retry a reviewed import."""
        lid, part = str(row["lecture_id"]), row["recording_part_index"]
        if row["bunny_video_id"]:
            return JobOutcome(lid, REVIEW, "VIDEO_ALREADY_IDENTIFIED_USE_ENCODING_RETRY", detail={"part": part})
        found, exact = self._matches(connection, row)
        if len(found) == 1 and len(exact) == 1:
            return self._identify(connection, row, exact[0], event="VIDEO_ADOPTED_BY_OPERATOR")
        if found:
            return JobOutcome(lid, REVIEW, "AMBIGUOUS_FETCHED_VIDEO", detail={"part": part, "matches": len(found)})
        gate = self._gate(facts, part)
        if gate is not None:
            return gate
        # Bunny holds no video for this token: the operator's retry is the approval to submit again.
        row = self._set(connection, row, expect={"import_status": row["import_status"]},
                        event="OPERATOR_REIMPORT_APPROVED", import_status="PENDING", attempt_count=0,
                        last_error_code=None)
        return self._submit(connection, row, attempt)


# =================================================================================== ENCODING
class EncodingWorker(_Worker):
    job_kind = POLL_ENCODING

    def _run(self, connection, lecture_id, part_index, attempt, operator):
        row = self._video(connection, lecture_id, part_index)
        if row is None or row["import_status"] != "IDENTIFIED":
            return JobOutcome(lecture_id, NOT_APPLICABLE, "NO_IDENTIFIED_VIDEO")
        if row["encoding_status"] == "VERIFIED":
            return self._verified(row, "ALREADY_VERIFIED")
        cleanup = (row["metadata"] or {}).get("cleanup")
        if row["encoding_status"] in ("FAILED", "REVIEW_REQUIRED") and not operator:
            return JobOutcome(lecture_id, REVIEW, row["last_error_code"] or f"ENCODING_{row['encoding_status']}",
                              detail={"part": part_index, "retained": True})
        try:
            video = self.rt.bunny.get_video(str(row["bunny_video_id"]))
        except BunnyError as error:
            if error.status == 404:
                if cleanup in ("DELETE_PENDING", "DELETE_UNCERTAIN", "DELETE_FAILED"):
                    return self._retire_and_retry(connection, row)
                self._set(connection, row, expect={"encoding_status": row["encoding_status"]},
                          event="VIDEO_MISSING_IN_BUNNY", encoding_status="REVIEW_REQUIRED",
                          last_error_code="VIDEO_MISSING_IN_BUNNY")
                return JobOutcome(lecture_id, REVIEW, "VIDEO_MISSING_IN_BUNNY", detail={"part": part_index})
            raise
        status = video.get("status")
        if status in FAILED_STATUSES:
            return self._confirmed_failure(connection, row, video)
        if status == FINISHED and int(video.get("encodeProgress") or 0) >= 100:
            return self._verify(connection, row, video)
        progress = int(video.get("encodeProgress") or 0)
        if (status, progress) != (row["bunny_status_code"], row["encode_progress"]):
            row = self._set(connection, row, expect={"encoding_status": row["encoding_status"]},
                            encoding_status="IN_PROGRESS", bunny_status_code=status, encode_progress=progress,
                            history={"at": self.rt.clock().isoformat(), "status": status, "progress": progress})
        identified = row["video_identified_at"] or self.rt.clock()
        if self.rt.clock() - identified > self.rt.config.encoding_stall_after:
            return JobOutcome(lecture_id, REVIEW, "ENCODING_STALLED",
                              detail={"part": part_index, "status": _status_name(status), "progress": progress})
        return JobOutcome(lecture_id, WAITING, f"ENCODING_{_status_name(status)}",
                          retry_after=timedelta(minutes=5), detail={"part": part_index, "progress": progress})

    def _verify(self, connection, row, video) -> JobOutcome:
        lid, part, video_id = str(row["lecture_id"]), row["recording_part_index"], str(row["bunny_video_id"])
        resolutions = self.rt.bunny.get_resolutions(video_id)
        try:
            storage = self.rt.bunny.get_storage(video_id)
        except BunnyError:
            storage = {}
        checks, failed = media_checks(video, resolutions, storage,
                                      source_duration_seconds=float(row["source_duration_seconds"]),
                                      source_height=self._source_height(row), encoding_tier=self.rt.encoding_tier,
                                      duration_tolerance_seconds=self.rt.config.duration_tolerance_seconds)
        with connection.transaction():
            ours = self.rt.captions.current(connection, self.rt.library_id, video_id, self.rt.config.caption_language)
        if self.rt.config.caption_language in checks["captions_present_before_publication"] and ours is None:
            failed.append("unexpected_caption_present")
        fields = {"bunny_status_code": video.get("status"), "encode_progress": video.get("encodeProgress"),
                  "bunny_length_seconds": video.get("length"), "bunny_width": video.get("width"),
                  "bunny_height": video.get("height"), "output_codecs": video.get("outputCodecs"),
                  "available_resolutions": video.get("availableResolutions"),
                  "bunny_storage_size_bytes": video.get("storageSize"),
                  "encoding_tier": str(self.rt.encoding_tier) if self.rt.encoding_tier is not None else None,
                  "media_verification": checks}
        if failed:
            self._set(connection, row, expect={"encoding_status": row["encoding_status"]},
                      event="MEDIA_VERIFICATION_FAILED", encoding_status="REVIEW_REQUIRED",
                      last_error_code="MEDIA_VERIFICATION_FAILED", **fields)
            return JobOutcome(lid, REVIEW, "MEDIA_VERIFICATION_FAILED", detail={"part": part, "failed": failed})
        row = self._set(connection, row, expect={"encoding_status": row["encoding_status"]}, event="MEDIA_VERIFIED",
                        encoding_status="VERIFIED", encoding_completed_at=self.rt.clock(),
                        media_verified_at=self.rt.clock(), last_error_code=None,
                        history={"at": self.rt.clock().isoformat(), "status": video.get("status"), "verified": True},
                        **fields)
        return self._verified(row, "MEDIA_VERIFIED")

    def _verified(self, row, reason) -> JobOutcome:
        return JobOutcome(str(row["lecture_id"]), DONE, reason,
                          follow_ups=[(PUBLISH_CAPTION, row["recording_part_index"])],
                          detail={"part": row["recording_part_index"]})

    def _source_height(self, row):
        if self.rt.graph is None:
            return None
        try:
            from app.bunny.source import drive_item_facts
            facts = drive_item_facts(self.rt.graph, row["recording_drive_id"], row["recording_item_id"])
            return (facts.video or {}).get("height")
        except Exception:  # noqa: BLE001 - unknown height means every configured resolution is expected
            return None

    # -- confirmed failed import --------------------------------------------------------
    def _confirmed_failure(self, connection, row, video) -> JobOutcome:
        lid, part, video_id = str(row["lecture_id"]), row["recording_part_index"], str(row["bunny_video_id"])
        status = video.get("status")
        with connection.transaction():
            caption = self.rt.captions.current(connection, self.rt.library_id, video_id,
                                               self.rt.config.caption_language)
        deletable = (row["encoding_status"] != "VERIFIED" and row["visibility_state"] == "HIDDEN"
                     and caption is None and not (row["metadata"] or {}).get("lms_published"))
        code = f"BUNNY_{_status_name(status)}"
        if not deletable:
            self._set(connection, row, expect={"encoding_status": row["encoding_status"]},
                      event="VIDEO_IMPORT_FAILED_CONFIRMED", encoding_status="FAILED", bunny_status_code=status,
                      last_error_code=code, metadata={"cleanup": "NOT_ELIGIBLE"})
            return JobOutcome(lid, REVIEW, "CONFIRMED_FAILURE_NOT_DELETABLE", detail={"part": part, "status": code})
        # Audit the failure AND the intended deletion before the delete call.
        with connection.transaction():
            updated = self.rt.videos.transition(
                connection, row["bunny_video_record_id"], expect={"encoding_status": row["encoding_status"]},
                encoding_status="FAILED", bunny_status_code=status, last_error_code=code,
                metadata={"cleanup": "DELETE_PENDING", "failed_status": code},
                history={"at": self.rt.clock().isoformat(), "status": status, "failed": True})
            if updated is None:
                return JobOutcome(lid, DEFERRED, "CONCURRENT_CHANGE", retry_after=timedelta(minutes=1))
            self._event(connection, "VIDEO_IMPORT_FAILED_CONFIRMED", lid, part=part, bunny_video_id=video_id,
                        status=code, record_id=str(row["bunny_video_record_id"]))
            self._event(connection, "VIDEO_DELETE_INTENDED", lid, part=part, bunny_video_id=video_id,
                        reason=DELETE_REASON, record_id=str(row["bunny_video_record_id"]))
        return self._delete(connection, updated)

    def _delete(self, connection, row) -> JobOutcome:
        lid, part, video_id = str(row["lecture_id"]), row["recording_part_index"], str(row["bunny_video_id"])
        try:
            self.rt.bunny.delete_video(video_id)
        except BunnyError as error:
            if not error.uncertain:
                self._set(connection, row, expect={"encoding_status": "FAILED"}, event="VIDEO_DELETE_FAILED",
                          metadata={"cleanup": "DELETE_FAILED"}, last_error_code=f"DELETE_{error.code}")
                return JobOutcome(lid, REVIEW, "CLEANUP_FAILED", detail={"part": part, "bunny_video_id": video_id})
        # Confirm by reading back: only a 404 proves the video is gone.
        try:
            still = self.rt.bunny.get_video(video_id)
        except BunnyError as error:
            if error.status == 404:
                return self._retire_and_retry(connection, row)
            still = None
        self._set(connection, row, expect={"encoding_status": "FAILED"}, event="VIDEO_DELETE_UNCONFIRMED",
                  metadata={"cleanup": "DELETE_UNCERTAIN"}, last_error_code="DELETE_NOT_CONFIRMED")
        return JobOutcome(lid, REVIEW, "CLEANUP_NOT_CONFIRMED",
                          detail={"part": part, "bunny_video_id": video_id, "still_listed": still is not None})

    def _retire_and_retry(self, connection, row) -> JobOutcome:
        lid, part = str(row["lecture_id"]), row["recording_part_index"]
        with connection.transaction():
            retired = self.rt.videos.transition(
                connection, row["bunny_video_record_id"], expect={"retired_at": None},
                retired_at=self.rt.clock(), retire_reason=DELETE_REASON, metadata={"cleanup": "DELETED"})
            if retired is None:
                return JobOutcome(lid, DEFERRED, "CONCURRENT_CHANGE", retry_after=timedelta(minutes=1))
            self._event(connection, "VIDEO_DELETED", lid, part=part, bunny_video_id=str(row["bunny_video_id"]),
                        record_id=str(row["bunny_video_record_id"]))
            generations = self.rt.videos.generations(connection, self.rt.library_id, row["recording_drive_id"],
                                                     row["recording_item_id"])
        if generations >= self.rt.config.max_import_generations:
            return JobOutcome(lid, REVIEW, "IMPORT_RETRIES_EXHAUSTED", detail={"part": part})
        return JobOutcome(lid, DONE, "CONFIRMED_FAILURE_CLEANED_REIMPORTING", follow_ups=[(IMPORT_VIDEO, part)],
                          detail={"part": part, "deleted_bunny_video_id": str(row["bunny_video_id"])})


# =================================================================================== CAPTIONS
TRANSCRIPT_PARTS = """
SELECT a.artifact_id, p.part_index, a.provider_call_id, a.content_correlation_id, a.provider_created_at,
       a.content_version, a.content_sha256, s.selection_id
  FROM public.lecture_transcript_selections s
  JOIN public.lecture_transcript_selection_parts p ON p.selection_id = s.selection_id
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = p.artifact_id
 WHERE s.lecture_id = %s AND s.selection_status = 'SELECTED' AND s.selection_version = %s
 ORDER BY p.part_index
"""
CONTENT = """
SELECT raw_content, content_sha256, content_format FROM public.lecture_transcript_artifact_contents
 WHERE artifact_id = %s AND version_number = %s
"""
MAX_CAPTION_OFFSET_MS = 120_000


class CaptionWorker(_Worker):
    job_kind = PUBLISH_CAPTION

    def _run(self, connection, lecture_id, part_index, attempt, operator):
        cfg, lib = self.rt.config, self.rt.library_id
        language, label = cfg.caption_language, cfg.caption_label
        row = self._video(connection, lecture_id, part_index)
        if row is None or row["import_status"] != "IDENTIFIED":
            return JobOutcome(lecture_id, NOT_APPLICABLE, "NO_IDENTIFIED_VIDEO")
        if row["encoding_status"] != "VERIFIED":
            return JobOutcome(lecture_id, WAITING, "VIDEO_NOT_VERIFIED", retry_after=timedelta(minutes=10))
        video_id = str(row["bunny_video_id"])
        source = self._source(connection, lecture_id, part_index)
        try:
            build = cap.build_caption(source["raw"], applied_offset_ms=source["offset_ms"])
        except cap.CaptionBuildError as error:
            return JobOutcome(lecture_id, REVIEW, f"CAPTION_BUILD_FAILED:{error.code}", detail={"part": part_index})
        # The tail is judged against the MEASURED MP4 duration (milliseconds), not Bunny's whole-second
        # length, which is rounded down; encoding verification already proved the two agree.
        validation = cap.validate_caption(build, source["raw"],
                                          media_duration_seconds=float(row["source_duration_seconds"]),
                                          tail_tolerance_seconds=cap.CAPTION_TAIL_TOLERANCE_SECONDS)
        if row["bunny_length_seconds"] is None:
            validation.failures.append("bunny_length_unknown")
        if validation.failures:
            return JobOutcome(lecture_id, REVIEW, "CAPTION_VALIDATION_FAILED",
                              detail={"part": part_index, "failed": validation.failures})
        publication_id = str(caption_publication_id(lib, video_id, language, build.sha256, build.transform_version))
        with connection.transaction():
            current = self.rt.captions.current(connection, lib, video_id, language)
            publication = self.rt.captions.publication(connection, publication_id)
        if current and str(current["publication_id"]) == publication_id and current["publication_status"] == "VERIFIED":
            return self._done(connection, lecture_id, part_index, "CAPTION_ALREADY_CURRENT")
        revision = False
        if current and str(current["publication_id"]) != publication_id:
            same_source = str(current["source_artifact_id"]) == source["artifact_id"]
            newer = int(current["source_content_version"]) < int(source["content_version"])
            if not (same_source and (newer or current["source_content_version"] == source["content_version"])):
                return JobOutcome(lecture_id, REVIEW, "CAPTION_SOURCE_CHANGED", detail={"part": part_index})
            revision = True
        if publication and publication["publication_status"] in ("REJECTED", "FAILED", "REVIEW_REQUIRED") \
                and not operator:
            return JobOutcome(lecture_id, REVIEW, f"CAPTION_{publication['publication_status']}",
                              detail={"part": part_index, "retained": True})
        if publication is None:
            with connection.transaction():
                publication = self.rt.captions.prepare(
                    connection, publication_id=publication_id, library_id=lib, video_id=video_id,
                    lecture_id=lecture_id, language=language, label=label, selection_id=source["selection_id"],
                    artifact_id=source["artifact_id"], content_version=source["content_version"],
                    content_sha256=source["content_sha256"], caption_sha256=build.sha256,
                    caption_bytes=build.size_bytes, transform_version=build.transform_version,
                    alignment_version=rm.MAPPING_VERSION, alignment_method=source["media_origin"],
                    offset_ms=build.applied_offset_ms, source_cues=build.source_cue_count,
                    published_cues=build.cue_count, removed_voice_tags=build.removed_voice_tags)
                self._event(connection, "CAPTION_PREPARED", lecture_id, part=part_index,
                            publication_id=publication_id, revision=revision,
                            source_content_version=source["content_version"])
        status = publication["publication_status"]
        if status == "VERIFIED":
            return self._make_current(connection, publication, part_index)
        if status == "REVIEW_REQUIRED" and operator and publication["last_error_code"] == "READBACK_FAILED" \
                and publication["published_at"] is not None:
            return self._reverify(connection, publication, video_id, build, part_index)
        if status != "PUBLISHED":
            video = self.rt.bunny.get_video(video_id)
            present = language in [c.get("srclang") for c in (video.get("captions") or [])]
            attempted = status in ("UPLOADING", "UPLOAD_UNCERTAIN") or operator
            if present and current is None and not attempted:
                return JobOutcome(lecture_id, REVIEW, "FOREIGN_CAPTION_PRESENT", detail={"part": part_index})
            publication = self._upload(connection, publication, video_id, build, part_index)
            if publication["publication_status"] != "PUBLISHED":
                kind = WAITING if publication["publication_status"] == "UPLOAD_UNCERTAIN" else REVIEW
                return JobOutcome(lecture_id, kind, f"CAPTION_{publication['publication_status']}",
                                  retry_after=timedelta(minutes=2) if kind == WAITING else None,
                                  detail={"part": part_index})
        readback = self._read_back(video_id, build)
        verified = readback["registered"] and readback["label_matches"] and readback["content_check"] != "MISMATCH"
        if not verified:
            with connection.transaction():
                self.rt.captions.transition(connection, publication_id, expect="PUBLISHED", readback=readback,
                                            publication_status="REVIEW_REQUIRED", last_error_code="READBACK_FAILED")
            return JobOutcome(lecture_id, REVIEW, "CAPTION_READBACK_FAILED", detail={"part": part_index})
        with connection.transaction():
            publication = self.rt.captions.transition(
                connection, publication_id, expect="PUBLISHED", publication_status="VERIFIED", verified_at="now()",
                readback=readback, bunny_caption_version=readback.get("caption_version"))
        if publication is None:
            return JobOutcome(lecture_id, DEFERRED, "CONCURRENT_CHANGE", retry_after=timedelta(minutes=1))
        return self._make_current(connection, publication, part_index)

    def _source(self, connection, lecture_id, part_index) -> dict:
        """This part's own raw WebVTT and its PROVEN offset onto this recording's media timeline."""
        from app.transcripts.selection import SELECTION_VERSION
        with connection.transaction():
            facts = load_facts(connection, lecture_id)
            rows = connection.execute(TRANSCRIPT_PARTS, (lecture_id, SELECTION_VERSION)).fetchall()
        part = facts.part(part_index)
        if part is None or part["locate_status"] != "LOCATED":
            raise _Stop(JobOutcome(lecture_id, REVIEW, "RECORDING_PART_NOT_LOCATED", detail={"part": part_index}))
        located = [p for p in facts.parts if p["locate_status"] == "LOCATED"]
        mappings = [rm.PartMapping(part_index=p["part_index"], locate_status=rm.LOCATED, reason="STORED",
                                   recording=rm.RecordingPart(p["graph_recording_id"], p["call_id"] or "",
                                                              p["recording_created_at"], p["recording_end_at"],
                                                              p["content_correlation_id"])) for p in located]
        transcripts = [rm.TranscriptPartRef(str(r[0]), r[1], (r[2] or "").lower() or None, r[3], r[4]) for r in rows]
        paired, _, conflict = rm.pair_transcripts(mappings, (), transcripts)
        if conflict:
            raise _Stop(JobOutcome(lecture_id, REVIEW, conflict, detail={"part": part_index}))
        mine = next(m for m in paired if m.part_index == part_index)
        if not mine.transcript_artifact_id:
            deadline = facts.scheduled_end + timedelta(hours=72) if facts.scheduled_end else None
            if deadline is not None and self.rt.clock() > deadline:
                raise _Stop(JobOutcome(lecture_id, REVIEW, "NO_TRANSCRIPT_FOR_PART", detail={"part": part_index}))
            raise _Stop(JobOutcome(lecture_id, WAITING, "TRANSCRIPT_PART_NOT_AVAILABLE",
                                   retry_after=timedelta(minutes=45), detail={"part": part_index}))
        artifact = next(r for r in rows if str(r[0]) == mine.transcript_artifact_id)
        artifact_id, _, _, _, created, version, sha, selection_id = artifact
        with connection.transaction():
            content = connection.execute(CONTENT, (artifact_id, version)).fetchone()
        if content is None or content[2] != "text/vtt" or content[1] != sha or cap.sha256_text(content[0]) != sha:
            raise _Stop(JobOutcome(lecture_id, REVIEW, "TRANSCRIPT_CONTENT_POINTER_MISMATCH", detail={"part": part_index}))
        media_zero = part["recording_created_at"]
        if part["media_origin"] == "file_name_origin":
            media_zero = media_zero - timedelta(seconds=float(part["lead_seconds"] or 0))
        offset_ms = int(round((created - media_zero).total_seconds() * 1000))
        if abs(offset_ms) > MAX_CAPTION_OFFSET_MS:
            raise _Stop(JobOutcome(lecture_id, REVIEW, "CAPTION_OFFSET_IMPLAUSIBLE",
                                   detail={"part": part_index, "offset_ms": offset_ms}))
        return {"raw": content[0], "artifact_id": str(artifact_id), "content_version": int(version),
                "content_sha256": sha, "selection_id": str(selection_id), "offset_ms": offset_ms,
                "media_origin": part["media_origin"]}

    def _upload(self, connection, publication, video_id, build, part_index) -> dict:
        pid = str(publication["publication_id"])
        with connection.transaction():
            publication = self.rt.captions.transition(
                connection, pid, expect=("PREPARED", "UPLOADING", "UPLOAD_UNCERTAIN", "REJECTED", "FAILED",
                                         "REVIEW_REQUIRED"),
                publication_status="UPLOADING", upload_started_at="now()", attempt_count="+1")
            self._event(connection, "CAPTION_UPLOADING", publication["lecture_id"], part=part_index, publication_id=pid)
        try:
            result = self.rt.bunny.add_caption(video_id, self.rt.config.caption_language,
                                               label=self.rt.config.caption_label, vtt_text=build.text)
        except BunnyError as error:
            status = "UPLOAD_UNCERTAIN" if error.uncertain else "FAILED"
            with connection.transaction():
                return self.rt.captions.transition(connection, pid, expect="UPLOADING", publication_status=status,
                                                   last_error_code=error.code)
        accepted = (result.get("http_status", 200) < 400 and result.get("success") is not False
                    and result.get("valid") is not False and not result.get("errorList"))
        validation = {k: result.get(k) for k in ("http_status", "success", "statusCode", "message", "valid",
                                                  "errorList", "warningList", "warningMessage")}
        with connection.transaction():
            publication = self.rt.captions.transition(
                connection, pid, expect="UPLOADING", bunny_validation=validation,
                publication_status="PUBLISHED" if accepted else "REJECTED",
                **({"published_at": "now()", "last_error_code": None} if accepted
                   else {"last_error_code": "CAPTION_REJECTED"}))
            self._event(connection, "CAPTION_PUBLISHED" if accepted else "CAPTION_REJECTED",
                        publication["lecture_id"], part=part_index, publication_id=pid)
        return publication

    def _reverify(self, connection, publication, video_id, build, part_index) -> JobOutcome:
        """Operator retry of a caption Bunny ACCEPTED whose read-back failed: read it back again, never
        re-upload. Only a served copy IDENTICAL or EQUIVALENT to this publication's build verifies it; a
        missing caption, an unreachable CDN or any mismatch leaves it REVIEW_REQUIRED."""
        lecture_id, pid = str(publication["lecture_id"]), str(publication["publication_id"])
        readback = self._read_back(video_id, build)
        verified = (readback["registered"] and readback["label_matches"]
                    and readback["content_check"] in (cap.IDENTICAL, cap.EQUIVALENT))
        with connection.transaction():
            if verified:
                publication = self.rt.captions.transition(
                    connection, pid, expect="REVIEW_REQUIRED", publication_status="VERIFIED", verified_at="now()",
                    last_error_code=None, readback=readback, bunny_caption_version=readback.get("caption_version"))
            else:
                self.rt.captions.transition(connection, pid, expect="REVIEW_REQUIRED", readback=readback)
            self._event(connection, "CAPTION_REVERIFIED" if verified else "CAPTION_REVERIFY_FAILED", lecture_id,
                        part=part_index, publication_id=pid, content_check=readback["content_check"],
                        caption_version=readback.get("caption_version"))
        if not verified:
            return JobOutcome(lecture_id, REVIEW, "CAPTION_REVERIFY_FAILED", detail={"part": part_index})
        if publication is None:
            return JobOutcome(lecture_id, DEFERRED, "CONCURRENT_CHANGE", retry_after=timedelta(minutes=1))
        return self._make_current(connection, publication, part_index)

    def _read_back(self, video_id, build) -> dict:
        cfg = self.rt.config
        video = self.rt.bunny.get_video(video_id)
        entry = next((c for c in (video.get("captions") or []) if c.get("srclang") == cfg.caption_language), None)
        readback = {"registered": entry is not None,
                    "label_matches": bool(entry) and entry.get("label") == cfg.caption_label,
                    "caption_version": entry.get("version") if entry else None, "content_check": "NOT_ATTEMPTED"}
        host = _cdn_hostname(cfg)
        if entry is None or not host or self.rt.open_url is None:
            return readback
        path, version = f"/{video_id}/captions/{cfg.caption_language}.vtt", entry.get("version") or 0
        key = getattr(cfg, "cdn_token_key", "")
        # With CDN token authentication on, the caption file needs a token too; the publisher signs its own
        # read-back with the pull zone key. A wrong or missing token is a 403 -> CDN_UNAVAILABLE, never a pass.
        url = tokens.signed_cdn_url(host, path, key, params={"ver": version}) if key \
            else f"https://{host}{path}?ver={version}"
        try:
            with self.rt.open_url(urllib.request.Request(url, headers={"Accept": "text/vtt"}), 60) as response:
                served = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            readback.update(content_check="CDN_UNAVAILABLE", cdn_status=getattr(error, "code", None))
            return readback
        # Bunny re-serialises a stored caption (BOM, numbered cues, trailing newline), so the check is
        # cue-by-cue and exact, not byte-for-byte; the diagnostics carry no caption text.
        comparison = cap.compare_published_caption(build.text, served)
        readback.update(content_check=comparison.pop("result"), content_comparison=comparison)
        return readback

    def _make_current(self, connection, publication, part_index) -> JobOutcome:
        lecture_id = str(publication["lecture_id"])
        with connection.transaction():
            superseded = self.rt.captions.make_current(connection, publication)
            self._event(connection, "CAPTION_VERIFIED", lecture_id, part=part_index,
                        publication_id=str(publication["publication_id"]), superseded=superseded)
        return self._done(connection, lecture_id, part_index, "CAPTION_VERIFIED")

    def _done(self, connection, lecture_id, part_index, reason) -> JobOutcome:
        mark_lecture_ready(connection, lecture_id, self.rt.library_id, self.rt.config.caption_language, self.rt.queue)
        return JobOutcome(lecture_id, DONE, reason, detail={"part": part_index})


def mark_lecture_ready(connection, lecture_id, library_id, language, queue) -> bool:
    """Record LECTURE_FULLY_READY once per lecture and library, when readiness first holds."""
    with connection.transaction():
        facts = load_facts(connection, lecture_id)
        state = readiness(connection, facts, library_id, language)
        if not state.fully_ready:
            return False
        exists = connection.execute(
            "SELECT 1 FROM public.bunny_publisher_events WHERE lecture_id = %s AND event = 'LECTURE_FULLY_READY' "
            "AND detail ->> 'library_id' = %s", (lecture_id, str(library_id))).fetchone()
        if not exists:
            queue.event(connection, "LECTURE_FULLY_READY", lecture_id=lecture_id,
                        detail={"library_id": str(library_id), "parts": len(state.parts)})
        return True
