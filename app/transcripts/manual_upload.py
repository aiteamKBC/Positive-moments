"""
Manual transcript upload: the permanent fallback for a Teams transcript Microsoft Graph will not
serve (November 2026: `400 Invalid transcript id` on channel-meeting transcripts Teams itself
shows and lets a person download).

AN ADDITIONAL INPUT, NOT A SECOND PIPELINE
------------------------------------------
Graph lists the transcript; only its CONTENT cannot be fetched. So the upload is attached to the
transcript artifact Graph already registered for the lecture - same artifact row, same append-only
content table, same `store_content` - and from there every existing step runs unchanged:

    upload (validated, confirmed)                       <- this module
      -> lecture_transcript_artifact_contents          (TranscriptArtifactRepository.store_content)
      -> selection + combination for THIS lecture      (TranscriptSelectionService.select_lecture,
                                                        the body the day run and the Publisher use)
      -> canonical cues, speakers, attendance, engagement, QA, render, legacy sync
                                                        (app/orchestration/transcript_recovery.py:
                                                         the real orchestrator, narrowed to the lecture)

IDENTITY IS NEVER TAKEN FROM THE CALLER
---------------------------------------
The caller names a lecture (the page it is on) and, for a multi-part lecture, which of THAT
lecture's parts. The target transcript is whatever the existing selector chooses for the
lecture's own scheduled occurrence from the transcripts Graph listed for the lecture's own
meeting. No meeting id, transcript id or date is accepted from the request. A part outside that
selection is refused.

WHAT IS REFUSED
---------------
Anything that is not a UTF-8 WebVTT file with spoken cues; a file larger than MAX_UPLOAD_BYTES;
a transcript that runs longer than the Teams transcript it claims to be; bytes already stored for
a different transcript (another lecture's file); a part that already holds content (no silent
replacement: there is no replacement path at all); and a lecture with no Teams transcript for its
occurrence (nothing to attach to).

Identical bytes uploaded again to the same part are a no-op success, not a second version.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from app.common.errors import PlatformError
from app.common.hashing import content_sha256 as sha256_of
from app.db.repositories.manual_transcripts import ManualTranscriptRepository
from app.db.repositories.transcript_artifacts import MANUAL_UPLOAD, TranscriptArtifactRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository,
    TranscriptSelectionRunRepository,
)
from app.orchestration.locks import LectureBusy, LectureLockManager
from app.transcripts.combine import combine_transcript_parts
from app.transcripts.models import VTT, ReadyLecture
from app.transcripts.selection import SELECTED, SELECTION_VERSION, select_transcript_parts
from app.transcripts.selection_service import TranscriptSelectionService
from app.transcripts.webvtt import (
    EMPTY_TRANSCRIPT,
    INVALID_WEBVTT,
    UNSUPPORTED_STRUCTURE,
    parse_combined_webvtt,
)


VALIDATOR_VERSION = "manual_vtt_upload_v1"

# A three-hour Teams transcript is a few hundred kilobytes; 5 MB is generous and still small
# enough that a wrong file (a recording, an export) is refused before it is read in full.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSION = ".vtt"

# The downloaded transcript is timed from the start of its call, exactly like Graph's. Its last
# cue cannot end after the call did; two minutes absorbs end-of-call rounding.
OVERRUN_TOLERANCE_MS = 120_000
# Speech covering under half of the transcript's span is unusual enough to say so before confirming.
LOW_COVERAGE_RATIO = 0.5
# A handful of unreadable blocks is reported; more than this share means a damaged file.
MALFORMED_SHARE_LIMIT = 0.02
# The recovery list's ceiling: each entry is confirmed with a few small reads.
RECOVERY_QUEUE_LIMIT = 100

# --- refusal codes (each one says what to do) -------------------------------------------------
NOT_A_VTT_FILE = "NOT_A_VTT_FILE"
EMPTY_FILE = "EMPTY_FILE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
NOT_A_TEXT_FILE = "NOT_A_TEXT_FILE"
NOT_UTF8 = "NOT_UTF8"
INVALID_WEBVTT_FILE = "INVALID_WEBVTT"
NO_CUES = "NO_CUES"
NO_SPOKEN_TEXT = "NO_SPOKEN_TEXT"
MALFORMED_CUES = "MALFORMED_CUES"
NOT_COMBINABLE = "NOT_COMBINABLE"
LECTURE_NOT_FOUND = "LECTURE_NOT_FOUND"
LECTURE_CANCELLED = "LECTURE_CANCELLED"
LECTURE_IS_SUPPRESSED_DUPLICATE = "LECTURE_IS_SUPPRESSED_DUPLICATE"
MEETING_NOT_RESOLVED = "MEETING_NOT_RESOLVED"
NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE = "NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE"
PART_NOT_IN_THIS_LECTURE = "PART_NOT_IN_THIS_LECTURE"
PART_REQUIRED = "PART_REQUIRED"
ALL_PARTS_HAVE_TRANSCRIPTS = "ALL_PARTS_HAVE_TRANSCRIPTS"
TRANSCRIPT_ALREADY_STORED = "TRANSCRIPT_ALREADY_STORED"
CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT = "CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT"
LONGER_THAN_TEAMS_TRANSCRIPT = "LONGER_THAN_TEAMS_TRANSCRIPT"
FILE_CHANGED_SINCE_PREVIEW = "FILE_CHANGED_SINCE_PREVIEW"
TRANSCRIPT_ROW_LOCKED = "TRANSCRIPT_ROW_LOCKED"

# --- warnings (shown before confirming; they do not block) -------------------------------------
NO_SPEAKER_LABELS = "NO_SPEAKER_LABELS"
LOW_COVERAGE = "LOW_COVERAGE"
SOME_MALFORMED_BLOCKS = "SOME_MALFORMED_BLOCKS"
NON_MONOTONIC_CUES = "NON_MONOTONIC_CUES"
PART_DURATION_UNKNOWN = "PART_DURATION_UNKNOWN"
OTHER_PARTS_MISSING = "OTHER_PARTS_MISSING"

MESSAGES = {
    NOT_A_VTT_FILE: "Only a .vtt transcript file downloaded from Microsoft Teams can be uploaded.",
    EMPTY_FILE: "The file is empty.",
    FILE_TOO_LARGE: f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB; a Teams transcript is far smaller.",
    NOT_A_TEXT_FILE: "The file is not a text transcript.",
    NOT_UTF8: "The file is not UTF-8 text. Download the transcript from Teams again as .vtt.",
    INVALID_WEBVTT_FILE: "The file does not start with WEBVTT, so it is not a WebVTT transcript.",
    NO_CUES: "The file contains no timed transcript cues.",
    NO_SPOKEN_TEXT: "Every cue in the file is empty; there is no spoken text to evaluate.",
    MALFORMED_CUES: "Too many cues in the file are unreadable; the file looks damaged.",
    NOT_COMBINABLE: "The transcript is too short or has no readable timing, so the pipeline could not use it.",
    LECTURE_NOT_FOUND: "This lecture does not exist.",
    LECTURE_CANCELLED: "This lecture is cancelled; it takes no transcript.",
    LECTURE_IS_SUPPRESSED_DUPLICATE: "This is a suppressed duplicate calendar event; upload to the lecture that was kept.",
    MEETING_NOT_RESOLVED: "This lecture's Teams meeting has not been resolved, so there is no transcript to attach a file to.",
    NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE: "Teams has not listed a transcript for this lecture's occurrence, so there is nothing to attach the file to.",
    PART_NOT_IN_THIS_LECTURE: "That transcript part does not belong to this lecture.",
    PART_REQUIRED: "This lecture has more than one transcript part still missing; choose which part the file is.",
    ALL_PARTS_HAVE_TRANSCRIPTS: "Every transcript part of this lecture is already stored; nothing is missing.",
    TRANSCRIPT_ALREADY_STORED: "This transcript part already holds a different stored transcript. It is never replaced by an upload.",
    CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT: "These exact bytes are already stored for a different transcript, so this file belongs to another lecture.",
    LONGER_THAN_TEAMS_TRANSCRIPT: "The file runs longer than the Teams transcript of this lecture, so it is not this lecture's transcript.",
    FILE_CHANGED_SINCE_PREVIEW: "The file is not the one that was previewed. Preview it again before confirming.",
    TRANSCRIPT_ROW_LOCKED: "The pipeline is writing this lecture's transcript right now. Try again in a few minutes.",
    NO_SPEAKER_LABELS: "No cue names a speaker. Engagement and speaker attribution will be limited.",
    LOW_COVERAGE: "The cues cover less than half of the Teams transcript's duration.",
    SOME_MALFORMED_BLOCKS: "A few blocks in the file are unreadable and will be skipped.",
    NON_MONOTONIC_CUES: "Some cues start before the previous one.",
    PART_DURATION_UNKNOWN: "Teams did not report this transcript's end time, so its duration could not be compared.",
    OTHER_PARTS_MISSING: "Other parts of this lecture are still missing; processing waits until every part is stored.",
}

# Refusals that are about the lecture or its state rather than the file: HTTP 409, not 400.
CONFLICT_CODES = frozenset({
    LECTURE_CANCELLED, LECTURE_IS_SUPPRESSED_DUPLICATE, MEETING_NOT_RESOLVED,
    NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE, ALL_PARTS_HAVE_TRANSCRIPTS, TRANSCRIPT_ALREADY_STORED,
    CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT, FILE_CHANGED_SINCE_PREVIEW, TRANSCRIPT_ROW_LOCKED,
})


def problem(code: str, **detail) -> dict:
    return {"code": code, "message": MESSAGES.get(code, code), **detail}


class UploadRefused(Exception):
    """Nothing was stored. `problems` names every reason, each with its message."""

    def __init__(self, problems: list[dict], *, status: str = "REFUSED"):
        super().__init__(problems[0]["message"] if problems else "upload refused")
        self.problems = problems
        self.status = status

    @property
    def code(self) -> str:
        return self.problems[0]["code"] if self.problems else "REFUSED"

    @property
    def is_conflict(self) -> bool:
        return self.code in CONFLICT_CODES

    @property
    def is_not_found(self) -> bool:
        return self.code == LECTURE_NOT_FOUND


# --- the file on its own ------------------------------------------------------------------------

@dataclass
class VttFile:
    filename: str
    content_bytes: int
    content_sha256: str
    text: str = field(repr=False)
    cue_count: int = 0
    spoken_cue_count: int = 0
    first_cue_start_ms: int | None = None
    last_cue_end_ms: int | None = None
    speaker_label_count: int = 0
    speaker_attribution: bool = False
    parse_status: str | None = None
    malformed_block_count: int = 0

    def facts(self) -> dict:
        """What the console shows and the provenance keeps. Never transcript text or speaker names."""
        return {
            "filename": self.filename, "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256, "cue_count": self.cue_count,
            "spoken_cue_count": self.spoken_cue_count,
            "first_cue_start_ms": self.first_cue_start_ms,
            "last_cue_end_ms": self.last_cue_end_ms,
            "speaker_label_count": self.speaker_label_count,
            "speaker_attribution": self.speaker_attribution,
            "parse_status": self.parse_status,
            "malformed_block_count": self.malformed_block_count,
        }


def clean_filename(name: str | None) -> str:
    """The base name only, bounded; it is shown and recorded, never used as a path."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    return base[:200] or "transcript.vtt"


def inspect_vtt(filename: str | None, raw: bytes) -> tuple[VttFile | None, list[dict], list[dict]]:
    """
    Validate an uploaded file on its own. Returns (file, errors, warnings); file is None when the
    bytes could not even be read as a transcript.

    The parser is the platform's canonical WebVTT parser and the combination check is the real
    Phase 2B combiner, so a file accepted here is a file the pipeline will accept.
    """
    name = clean_filename(filename)
    errors: list[dict] = []
    warnings: list[dict] = []
    if not name.lower().endswith(ALLOWED_EXTENSION):
        errors.append(problem(NOT_A_VTT_FILE))
    if not raw:
        return None, errors + [problem(EMPTY_FILE)], warnings
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, errors + [problem(FILE_TOO_LARGE, limit_bytes=MAX_UPLOAD_BYTES)], warnings
    if b"\x00" in raw:
        return None, errors + [problem(NOT_A_TEXT_FILE)], warnings
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, errors + [problem(NOT_UTF8)], warnings

    parsed = parse_combined_webvtt(text)
    metrics = parsed.metrics
    spoken = [cue for cue in parsed.cues if cue.text]
    labels = {cue.speaker_label_raw for cue in parsed.cues if cue.speaker_label_raw}
    vtt = VttFile(
        filename=name, content_bytes=len(raw), content_sha256=sha256_of(raw), text=text,
        cue_count=metrics.get("cue_count", 0), spoken_cue_count=len(spoken),
        first_cue_start_ms=metrics.get("first_cue_start_ms"),
        last_cue_end_ms=metrics.get("last_cue_end_ms"),
        speaker_label_count=len(labels), speaker_attribution=bool(labels),
        parse_status=parsed.status, malformed_block_count=metrics.get("malformed_block_count", 0))

    if parsed.status == INVALID_WEBVTT:
        errors.append(problem(INVALID_WEBVTT_FILE))
        return vtt, errors, warnings
    if parsed.status in (EMPTY_TRANSCRIPT, UNSUPPORTED_STRUCTURE) or not parsed.cues:
        errors.append(problem(NO_CUES))
        return vtt, errors, warnings
    if not spoken:
        errors.append(problem(NO_SPOKEN_TEXT))
    malformed = vtt.malformed_block_count
    if malformed > max(1, int(MALFORMED_SHARE_LIMIT * vtt.cue_count)):
        errors.append(problem(MALFORMED_CUES, malformed_block_count=malformed))
    elif malformed:
        warnings.append(problem(SOME_MALFORMED_BLOCKS, malformed_block_count=malformed))
    if metrics.get("non_monotonic_cue_count"):
        warnings.append(problem(NON_MONOTONIC_CUES, count=metrics["non_monotonic_cue_count"]))
    if not vtt.speaker_attribution:
        warnings.append(problem(NO_SPEAKER_LABELS))
    try:
        # The one-part case of the real combiner: the pipeline's own acceptance test.
        combine_transcript_parts([(datetime(2000, 1, 1, tzinfo=timezone.utc), text)])
    except ValueError as exc:
        errors.append(problem(NOT_COMBINABLE, detail=str(exc)))
    return vtt, errors, warnings


# --- the file against the lecture ---------------------------------------------------------------

def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


class ManualTranscriptUploadService:
    def __init__(self, *, repository=None, artifact_repository=None, selection_repository=None,
                 lock_manager=None, clock=None, lock_timeout_ms: int = 2000):
        self.repository = repository or ManualTranscriptRepository()
        self.artifacts = artifact_repository or TranscriptArtifactRepository()
        self.selections = selection_repository or TranscriptSelectionRepository()
        self.selection_service = TranscriptSelectionService(
            lecture_repository=None, selection_repository=self.selections,
            run_repository=TranscriptSelectionRunRepository())
        self.locks = lock_manager or LectureLockManager()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock_timeout_ms = lock_timeout_ms
        self.log = logging.getLogger(__name__)

    # -- what the console shows before a file is chosen ---------------------------------------
    def target(self, connection, lecture_id) -> dict:
        """
        The lecture, the transcript part(s) Teams listed for its occurrence, which of them hold
        content (and from where), and whether an upload is possible. Read-only.
        """
        lecture = self.repository.load_lecture(connection, lecture_id)
        if lecture is None:
            raise UploadRefused([problem(LECTURE_NOT_FOUND)])
        header = self._lecture_header(lecture)
        refusal = self._lecture_refusal(lecture)
        parts: list[dict] = []
        if refusal is None:
            plan = self._plan(connection, lecture)
            if plan.status != SELECTED:
                refusal = problem(NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE, selection_status=plan.status)
            else:
                parts = self._parts(connection, lecture, plan)
        missing = [part for part in parts if not part["has_content"]]
        if refusal is None and parts and not missing:
            refusal = problem(ALL_PARTS_HAVE_TRANSCRIPTS)
        uploads = self.repository.uploads_for_lecture(connection, lecture["lecture_id"])
        return {
            "lecture": header, "parts": parts,
            "upload_allowed": refusal is None,
            "refusal": refusal,
            "missing_part_count": len(missing),
            "all_parts_stored": bool(parts) and not missing,
            "uploads": uploads,
            "limits": {"max_bytes": MAX_UPLOAD_BYTES, "extension": ALLOWED_EXTENSION},
            "validator_version": VALIDATOR_VERSION,
        }

    # -- the Operations dashboard's recovery list -------------------------------------------------
    def recovery_queue(self, connection, *, date_from=None, date_to=None,
                       limit: int = RECOVERY_QUEUE_LIMIT) -> dict:
        """
        Every lecture (newest first, any date unless bounded) that an upload could complete now.

        A cheap SQL prefilter finds lectures with a listed Teams transcript but no stored content;
        each is then confirmed by target() - the per-lecture console's own rule - so this list can
        never offer an upload the lecture page would refuse. Read-only.
        """
        ids = self.repository.lectures_missing_content(
            connection, date_from=date_from, date_to=date_to, limit=limit + 1)
        truncated = len(ids) > limit
        lectures = []
        for lecture_id in ids[:limit]:
            target = self.target(connection, lecture_id)
            if not target["upload_allowed"]:
                continue
            missing = [part for part in target["parts"] if not part["has_content"]]
            lectures.append({
                **target["lecture"],
                "part_count": len(target["parts"]),
                "missing_part_count": target["missing_part_count"],
                "missing_parts": [{key: part[key] for key in (
                    "part_index", "artifact_id", "teams_started_at", "duration_ms",
                    "artifact_status", "graph_failure_reason")} for part in missing],
                "previous_upload_count": len(target["uploads"]),
            })
        return {"lectures": lectures, "count": len(lectures), "truncated": truncated,
                "date_from": _iso(date_from), "date_to": _iso(date_to), "limit": limit}

    # -- validate without writing ----------------------------------------------------------------
    def preview(self, connection, lecture_id, *, filename, raw: bytes, artifact_id=None) -> dict:
        """Everything the commit would check, and nothing written. Errors come back as data."""
        return self._evaluate(connection, lecture_id, filename=filename, raw=raw,
                              artifact_id=artifact_id)

    # -- store -------------------------------------------------------------------------------------
    def commit(self, connection, lecture_id, *, filename, raw: bytes, artifact_id,
               expected_sha256: str, uploaded_by: str | None) -> dict:
        """
        Store a confirmed upload and re-select the lecture. One transaction, the caller commits.

        Re-validates from scratch under the lecture lock and the artifact row lock, so a stale
        preview, a concurrent upload or a Graph write in between can never be overwritten.
        """
        lecture_id = str(lecture_id)
        if not self.locks.try_acquire(connection, lecture_id):
            raise LectureBusy(lecture_id)
        try:
            return self._commit_locked(connection, lecture_id, filename=filename, raw=raw,
                                       artifact_id=artifact_id, expected_sha256=expected_sha256,
                                       uploaded_by=uploaded_by)
        finally:
            try:
                self.locks.release(connection, lecture_id)
            except PlatformError:
                self.log.warning("lecture lock release failed for %s; released when the session ends",
                                 lecture_id)

    def _commit_locked(self, connection, lecture_id, *, filename, raw, artifact_id,
                       expected_sha256, uploaded_by) -> dict:
        connection.execute(f"SET LOCAL lock_timeout = {int(self.lock_timeout_ms)}")
        if artifact_id:
            try:
                self.repository.lock_artifact(connection, artifact_id)
            except psycopg.errors.LockNotAvailable as exc:
                raise UploadRefused([problem(TRANSCRIPT_ROW_LOCKED)]) from exc
        result = self._evaluate(connection, lecture_id, filename=filename, raw=raw,
                                artifact_id=artifact_id)
        if result["errors"]:
            raise UploadRefused(result["errors"])
        vtt_facts = result["file"]
        if not expected_sha256 or expected_sha256.strip().lower() != vtt_facts["content_sha256"]:
            raise UploadRefused([problem(FILE_CHANGED_SINCE_PREVIEW)])
        part = result["part"]
        if not artifact_id or str(artifact_id) != part["artifact_id"]:
            # The confirmation must name the part the preview showed; never inferred at commit.
            raise UploadRefused([problem(PART_REQUIRED)])
        if result["duplicate"]:
            return {**result, "status": "ALREADY_STORED", "stored": False,
                    "selection": self._selection_summary(connection, lecture_id)}

        lecture = self.repository.load_lecture(connection, lecture_id)
        text = raw.decode("utf-8")
        stored = self.artifacts.store_content(
            connection, part["artifact_id"], raw_text=text,
            content_sha256=vtt_facts["content_sha256"], content_bytes=vtt_facts["content_bytes"],
            content_format=VTT, speaker_attribution=vtt_facts["speaker_attribution"],
            source=MANUAL_UPLOAD)
        uploaded_at = self.clock()
        provenance = {
            "source": MANUAL_UPLOAD,
            "upload_id": str(uuid.uuid4()),
            "lecture_id": lecture_id,
            "artifact_id": part["artifact_id"],
            "provider_transcript_id": part["provider_transcript_id"],
            "content_sha256": vtt_facts["content_sha256"],
            "content_bytes": vtt_facts["content_bytes"],
            "content_version": stored["version_number"],
            "uploaded_at": uploaded_at.isoformat(),
            "uploaded_by": (uploaded_by or "")[:150] or None,
            "original_filename": vtt_facts["filename"],
            "validator_version": VALIDATOR_VERSION,
            "file": {key: vtt_facts[key] for key in (
                "cue_count", "spoken_cue_count", "first_cue_start_ms", "last_cue_end_ms",
                "speaker_label_count", "speaker_attribution", "parse_status")},
            "part_duration_ms": part["duration_ms"],
            "warnings": [item["code"] for item in result["warnings"]],
            "processing": {"status": "NOT_STARTED"},
        }
        self.repository.record_upload(connection, lecture_id, part["artifact_id"], provenance)

        # The lecture's own selection, exactly as the day run and the Publisher compute it.
        try:
            self.repository.lock_selection(connection, lecture_id, SELECTION_VERSION)
        except psycopg.errors.LockNotAvailable as exc:
            raise UploadRefused([problem(TRANSCRIPT_ROW_LOCKED)]) from exc
        counters = {"selections_created": 0, "selections_updated": 0,
                    "combined_transcripts_created": 0, "combined_transcripts_reused": 0,
                    "multi_part_selections": 0, "error_count": 0}
        selection = self.selection_service.select_lecture(
            connection, self._ready(lecture), counters, persist=True)
        self.log.info("manual transcript stored", extra={"fields": {
            "service": "manual_transcript_upload", "lecture_id": lecture_id,
            "artifact_id": part["artifact_id"], "content_version": stored["version_number"],
            "content_sha256_prefix": vtt_facts["content_sha256"][:12],
            "selection_status": selection.get("selection_status")}})
        return {**result, "status": "STORED", "stored": True,
                "content_version": stored["version_number"],
                "upload": provenance,
                "selection": {"selection_status": selection.get("selection_status"),
                              "selected_parts_count": selection.get("selected_parts_count"),
                              "combined_duration_minutes": selection.get("combined_duration_minutes"),
                              "combine_error": selection.get("combine_error")},
                "target": self.target(connection, lecture_id)}

    # -- the shared checks -----------------------------------------------------------------------
    def _evaluate(self, connection, lecture_id, *, filename, raw, artifact_id) -> dict:
        vtt, errors, warnings = inspect_vtt(filename, raw)
        lecture = self.repository.load_lecture(connection, lecture_id)
        if lecture is None:
            raise UploadRefused([problem(LECTURE_NOT_FOUND)])
        refusal = self._lecture_refusal(lecture)
        part = None
        parts: list[dict] = []
        duplicate = False
        if refusal is not None:
            errors.insert(0, refusal)
        else:
            plan = self._plan(connection, lecture)
            if plan.status != SELECTED:
                errors.insert(0, problem(NO_TEAMS_TRANSCRIPT_FOR_OCCURRENCE,
                                         selection_status=plan.status))
            else:
                parts = self._parts(connection, lecture, plan)
                part, part_problem = self._choose_part(parts, artifact_id, vtt)
                if part_problem:
                    errors.insert(0, part_problem)
        if part is not None and vtt is not None:
            duplicate = self._content_checks(connection, part, vtt, errors, warnings)
            self._duration_checks(part, vtt, errors, warnings)
            others = [item for item in parts
                      if item["artifact_id"] != part["artifact_id"] and not item["has_content"]]
            if others:
                warnings.append(problem(OTHER_PARTS_MISSING, missing_part_count=len(others)))
        return {
            "ok": not errors,
            "errors": errors, "warnings": warnings,
            "lecture": self._lecture_header(lecture),
            "part": part, "parts": parts,
            "file": vtt.facts() if vtt is not None else None,
            "duplicate": duplicate,
            "validator_version": VALIDATOR_VERSION,
        }

    def _choose_part(self, parts, artifact_id, vtt):
        if artifact_id:
            for item in parts:
                if item["artifact_id"] == str(artifact_id):
                    return item, None
            return None, problem(PART_NOT_IN_THIS_LECTURE)
        missing = [item for item in parts if not item["has_content"]]
        if len(missing) == 1:
            return missing[0], None
        if not missing:
            # Every part is stored. Identical bytes are still a recognisable re-upload.
            if vtt is not None:
                for item in parts:
                    if item["content_sha256"] == vtt.content_sha256:
                        return item, None
            return None, problem(ALL_PARTS_HAVE_TRANSCRIPTS)
        return None, problem(PART_REQUIRED, missing_part_count=len(missing))

    def _content_checks(self, connection, part, vtt, errors, warnings) -> bool:
        """True when these exact bytes are already this part's content (an idempotent re-upload)."""
        if part["has_content"]:
            if part["content_sha256"] == vtt.content_sha256:
                return True
            errors.append(problem(TRANSCRIPT_ALREADY_STORED, content_source=part["content_source"]))
            return False
        owners = [owner for owner in self.repository.content_owners(connection, vtt.content_sha256)
                  if owner["artifact_id"] != part["artifact_id"]]
        if owners:
            errors.append(problem(CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT, lectures=[
                {"lecture_id": owner["lecture_id"], "subject": owner["subject"],
                 "session_date": _iso(owner["session_date"])}
                for owner in owners if owner["lecture_id"]][:5]))
        return False

    @staticmethod
    def _duration_checks(part, vtt, errors, warnings) -> None:
        span = part["duration_ms"]
        last = vtt.last_cue_end_ms
        if last is None:
            return
        if not span:
            warnings.append(problem(PART_DURATION_UNKNOWN))
            return
        if last > span + OVERRUN_TOLERANCE_MS:
            errors.append(problem(LONGER_THAN_TEAMS_TRANSCRIPT, file_end_ms=last,
                                  teams_duration_ms=span))
        elif last < span * LOW_COVERAGE_RATIO:
            warnings.append(problem(LOW_COVERAGE, file_end_ms=last, teams_duration_ms=span))

    # -- lecture and selection plumbing -----------------------------------------------------------
    @staticmethod
    def _lecture_refusal(lecture) -> dict | None:
        if lecture["is_cancelled"]:
            return problem(LECTURE_CANCELLED)
        if lecture["is_suppressed_duplicate"]:
            return problem(LECTURE_IS_SUPPRESSED_DUPLICATE)
        if not (lecture["downstream_ready"] and lecture["meeting_id"]
                and lecture["meeting_lookup_user_id"]):
            return problem(MEETING_NOT_RESOLVED)
        return None

    @staticmethod
    def _lecture_header(lecture) -> dict:
        return {"lecture_id": str(lecture["lecture_id"]), "subject": lecture["subject"],
                "module": lecture["module"], "session_date": _iso(lecture["session_date"]),
                "scheduled_start": _iso(lecture["scheduled_start"]),
                "scheduled_end": _iso(lecture["scheduled_end"])}

    @staticmethod
    def _ready(lecture) -> ReadyLecture:
        return ReadyLecture(
            lecture_id=lecture["lecture_id"], meeting_id=lecture["meeting_id"],
            meeting_lookup_user_id=lecture["meeting_lookup_user_id"],
            scheduled_start=lecture["scheduled_start"], scheduled_end=lecture["scheduled_end"],
            session_date=lecture["session_date"], subject=lecture["subject"],
            module=lecture["module"],
            meeting_lookup_context_source=lecture["meeting_lookup_context_source"],
            join_url_oid_hint_status=lecture["join_url_oid_hint_status"])

    def _plan(self, connection, lecture):
        """The existing selector on the lecture's own candidates and schedule. No Graph."""
        candidates = self.selections.load_candidates(connection, lecture["lecture_id"])
        return select_transcript_parts(
            candidates, scheduled_start=lecture["scheduled_start"],
            scheduled_end=lecture["scheduled_end"], target_date=lecture["session_date"],
            meeting_id=lecture["meeting_id"])

    def _parts(self, connection, lecture, plan) -> list[dict]:
        states = self.repository.artifact_states(
            connection, lecture["lecture_id"], [part.candidate.artifact_id for part in plan.parts])
        parts = []
        for index, ranked in enumerate(plan.parts, start=1):
            artifact_id = str(ranked.candidate.artifact_id)
            state = states.get(artifact_id, {})
            has_content = bool(state.get("content_version"))
            duration_ms = (int(ranked.duration_seconds * 1000)
                           if ranked.candidate.provider_end_at is not None else None)
            manual = state.get("manual_upload") or None
            parts.append({
                "part_index": index,
                "artifact_id": artifact_id,
                "provider_transcript_id": ranked.candidate.provider_transcript_id,
                "is_primary": ranked.candidate.provider_transcript_id
                == plan.primary.candidate.provider_transcript_id,
                "teams_started_at": _iso(ranked.start),
                "teams_ended_at": _iso(ranked.candidate.provider_end_at),
                "duration_ms": duration_ms,
                "has_content": has_content,
                "content_source": state.get("content_source"),
                "content_sha256": state.get("content_sha256"),
                "content_bytes": state.get("content_bytes"),
                "artifact_status": state.get("artifact_status"),
                "graph_failure_reason": state.get("content_fetch_reason") if not has_content else None,
                "uploaded_at": manual.get("uploaded_at") if manual else None,
                "uploaded_by": manual.get("uploaded_by") if manual else None,
            })
        return parts

    def _selection_summary(self, connection, lecture_id) -> dict:
        row = connection.execute(
            "SELECT selection_status, selected_part_count FROM public.lecture_transcript_selections "
            " WHERE lecture_id = %s AND selection_version = %s",
            (str(lecture_id), SELECTION_VERSION)).fetchone()
        return {"selection_status": row[0], "selected_parts_count": row[1]} if row else {}
