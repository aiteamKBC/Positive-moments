"""
Single-recording Microsoft Teams -> Bunny Stream pilot.

    SharePoint recording --(fresh Graph download URL)--> Bunny URL Fetch
    --> Bunny free encoding --> stored Teams WebVTT (speaker tags removed)
    --> Bunny Caption API --> read-back verification

The MP4 never passes through this process: Bunny pulls it from Microsoft.

STATE MACHINE (persisted before every external write, so a crash or rerun
resumes instead of repeating):

  video   PENDING -> SUBMITTING -> SUBMITTED|SUBMISSION_UNCERTAIN -> IDENTIFIED
          encoding NOT_STARTED -> IN_PROGRESS -> FINISHED -> VERIFIED
  caption PREPARED -> UPLOADING|UPLOAD_UNCERTAIN -> PUBLISHED -> VERIFIED (current)

Bunny's URL Fetch returns no video id, so a submitted fetch is identified by
its deterministic title token (rec-xxxxxxxx). A run that finds SUBMITTING,
SUBMITTED or SUBMISSION_UNCERTAIN only RECONCILES - it never submits a second
fetch. Anything ambiguous stops at REVIEW_REQUIRED for a human.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.bunny import captions as cap
from app.bunny.client import FAILED_STATUSES, FINISHED, STATUS_NAMES, BunnyError
from app.bunny.identity import (
    build_title,
    caption_publication_id,
    recording_identity,
    video_record_id,
)
from app.bunny.source import drive_item_facts
from app.bunny.verification import media_checks
from app.media.alignment import ALIGNED, ALIGNMENT_VERSION, TranscriptPart, align
from app.media.source import _recordings_from_metadata
from app.recordings.candidates import normalize_subject, parse_recording_name
from app.transcripts.webvtt import PARSED, parse_combined_webvtt

PASS, FAIL, UNVERIFIABLE, WARN = "PASS", "FAIL", "UNVERIFIABLE", "WARN"
READY, BLOCKED, REVIEW_REQUIRED, WAITING, COMPLETED = (
    "READY", "BLOCKED", "REVIEW_REQUIRED", "WAITING", "COMPLETED")
EXACT_LINK_STATUSES = ("WRITTEN",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PilotConfig:
    lecture_id: str
    library_id: int
    cdn_hostname: str | None = None
    language: str = "en"
    label: str = "English"
    duration_tolerance_seconds: float = 2.0
    poll_interval_seconds: float = 30.0
    identify_timeout_seconds: float = 900.0
    encoding_timeout_seconds: float = 3 * 3600.0
    max_videos_in_library: int = 1
    forbidden_library_ids: frozenset = frozenset({785080})
    expected_any_resolution: tuple = ("720p", "1080p")
    output_dir: str = "output/bunny_pilot"
    dry_run: bool = False


@dataclass
class Check:
    number: int
    name: str
    status: str
    critical: bool = True
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"check": self.number, "name": self.name, "status": self.status,
                "critical": self.critical, **({"detail": self.detail} if self.detail else {})}


@dataclass
class Preflight:
    checks: list
    lecture: object = None
    facts: object = None
    alignment: object = None
    caption_offset_ms: int | None = None
    library_settings: dict = field(default_factory=dict)
    identity: str | None = None
    title: str | None = None
    record_id: str | None = None
    existing_video: dict | None = None

    @property
    def ok(self) -> bool:
        return all(c.status in (PASS, WARN) for c in self.checks if c.critical)

    def blocking(self) -> list:
        return [c.as_dict() for c in self.checks if c.critical and c.status not in (PASS, WARN)]


class BunnyPilot:
    def __init__(self, *, config: PilotConfig, load_source, graph, media, bunny, core, manifest,
                 credentials: dict, sleep=time.sleep, clock=time.monotonic, open_url=None):
        self.config = config
        self._load_source = load_source
        self.graph = graph
        self.media = media
        self.bunny = bunny
        self.core = core
        self.manifest = manifest
        self.credentials = credentials
        self._sleep = sleep
        self._clock = clock
        self._open = open_url or (lambda request, timeout: urllib.request.urlopen(request, timeout=timeout))
        self.warnings: list[str] = []

    def _read_with_retry(self, fn, *args, attempts: int = 4):
        """Retry a READ-ONLY Graph call on network/429/5xx failures (DNS here is intermittent)."""
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args)
            except Exception as error:
                status = getattr(error, "status", None)
                transient = (getattr(error, "retryable", None) is True if hasattr(error, "retryable")
                             else status is None or status == 429 or (status or 0) >= 500)
                if not transient or attempt == attempts:
                    raise
                self.warnings.append(f"graph read retried ({type(error).__name__}, attempt {attempt})")
                self._sleep(5.0 * attempt)

    # =========================================================================== preflight
    def preflight(self) -> Preflight:
        cfg = self.config
        checks: list[Check] = []
        pf = Preflight(checks=checks)
        add = lambda *a, **k: checks.append(Check(*a, **k))

        # 1 credentials present (never printed)
        present = {name: bool(value) for name, value in self.credentials.items()}
        add(1, "bunny_credentials_available",
            PASS if present.get("BUNNY_STAGING_LIBRARY_ID") and present.get("BUNNY_STAGING_API_KEY") else FAIL,
            detail={"present": present})

        # 3 destination is the intended staging library (checked before any call)
        destination_ok = (cfg.library_id not in cfg.forbidden_library_ids
                          and int(self.bunny.library_id) == int(cfg.library_id))
        destination = Check(3, "destination_is_staging_library", PASS if destination_ok else FAIL,
                            detail={"library_id": cfg.library_id,
                                    "forbidden": sorted(cfg.forbidden_library_ids)})

        # 2 library accessible
        library_videos = None
        try:
            if not destination_ok:
                raise BunnyError("DESTINATION_REFUSED", "not the staging library")
            library_videos = self.bunny.list_videos(page=1, items_per_page=100)
            add(2, "bunny_library_accessible", PASS,
                detail={"total_videos": library_videos.get("totalItems")})
        except BunnyError as error:
            add(2, "bunny_library_accessible", FAIL, detail={"error": error.code})
        checks.append(destination)

        # 4-7 library settings (core API; the library key cannot read them)
        settings = None
        if self.core is None:
            reason = {"reason": "Get Video Library needs a Bunny ACCOUNT or subuser API key; "
                                "BUNNY_STAGING_ACCOUNT_API_KEY is not configured (the library key gets 401)"}
            for number, name in ((4, "free_encoding_enabled"), (5, "ai_transcribing_disabled"),
                                 (6, "h264_output_enabled"), (7, "resolutions_appropriate")):
                add(number, name, UNVERIFIABLE, detail=reason)
        else:
            try:
                settings = self.core.get_video_library(cfg.library_id)
            except BunnyError as error:
                for number, name in ((4, "free_encoding_enabled"), (5, "ai_transcribing_disabled"),
                                     (6, "h264_output_enabled"), (7, "resolutions_appropriate")):
                    add(number, name, UNVERIFIABLE, detail={"error": error.code})
        if settings is not None:
            pf.library_settings = {k: settings.get(k) for k in (
                "Id", "EncodingTier", "EnableTranscribing", "EnableTranscribingTitleGeneration",
                "EnableTranscribingDescriptionGeneration", "TranscribingCaptionLanguages",
                "OutputCodecs", "EnabledResolutions", "JitEncodingEnabled", "KeepOriginalFiles",
                "EnableMP4Fallback", "AllowEarlyPlay")}
            tier = settings.get("EncodingTier")
            add(4, "free_encoding_enabled", PASS if tier in (0, "0", "Free", "free") and
                not settings.get("JitEncodingEnabled") else FAIL,
                detail={"EncodingTier": tier, "JitEncodingEnabled": settings.get("JitEncodingEnabled")})
            transcribing = {k: settings.get(k) for k in (
                "EnableTranscribing", "EnableTranscribingTitleGeneration",
                "EnableTranscribingDescriptionGeneration")}
            add(5, "ai_transcribing_disabled",
                PASS if not any(transcribing.values()) and not settings.get("TranscribingCaptionLanguages")
                else FAIL, detail=transcribing)
            codecs = [c.strip().lower() for c in str(settings.get("OutputCodecs") or "").split(",") if c.strip()]
            add(6, "h264_output_enabled", PASS if "x264" in codecs else FAIL, detail={"OutputCodecs": codecs})
            resolutions = [r.strip() for r in str(settings.get("EnabledResolutions") or "").split(",") if r.strip()]
            add(7, "resolutions_appropriate",
                PASS if any(r in resolutions for r in cfg.expected_any_resolution) else FAIL,
                detail={"EnabledResolutions": resolutions})

        # 9-13 source lecture, recording, transcript
        try:
            lecture = self._load_source()
            pf.lecture = lecture
        except Exception as error:                   # SourceError, or the DB being unreachable
            code = getattr(error, "code", None) or type(error).__name__
            for number, name in ((8, "graph_drive_item_accessible"), (9, "source_is_correct_lecture"),
                                 (10, "selected_raw_webvtt_available"), (11, "transcript_aligned"),
                                 (13, "single_part_lecture")):
                add(number, name, FAIL, detail={"error": code})
            lecture = None
        if lecture is not None:
            self._source_checks(pf, add, lecture)

        # 12, 14 duplicates and existing captions (need identity)
        self._duplicate_checks(pf, add, library_videos)

        # 15 operations supported by the credentials
        add(15, "api_operations_supported",
            PASS if any(c.number == 2 and c.status == PASS for c in checks) else FAIL,
            detail={"proven": "library AccessKey authenticated on a library-scoped endpoint",
                    "same_scheme_required_by": ["POST /videos/fetch", "POST /videos/{id}/captions/{srclang}"],
                    "note": "write permission itself is only proven by the first write"})
        checks.sort(key=lambda c: c.number)
        return pf

    def _source_checks(self, pf: Preflight, add, lecture) -> None:
        cfg = self.config
        t = lecture.transcript
        drive, item = lecture.recording_drive_id, lecture.recording_item_id

        # 8 Graph DriveItem metadata
        facts = None
        if drive and item:
            try:
                facts = self._read_with_retry(drive_item_facts, self.graph, drive, item)
                same = facts.item_id == item and facts.drive_id == drive
                add(8, "graph_drive_item_accessible",
                    PASS if same and facts.mime_type == "video/mp4" and facts.duration_seconds else FAIL,
                    detail={"same_item": same, "mime_type": facts.mime_type,
                            "size_bytes": facts.size_bytes, "duration_seconds": facts.duration_seconds,
                            "parent_present": bool(facts.parent_id)})
            except Exception as error:              # GraphError carries no URL or token
                add(8, "graph_drive_item_accessible", FAIL,
                    detail={"error": type(error).__name__, "status": getattr(error, "status", None),
                            "code": getattr(error, "code", None)})
        else:
            add(8, "graph_drive_item_accessible", FAIL, detail={"error": "NO_RECORDING_IDENTITY"})
        pf.facts = facts

        # 9 correct lecture and date
        parsed = parse_recording_name(facts.name if facts else None)
        lecture_key, file_key = normalize_subject(lecture.subject), parsed[0] if parsed else ""
        nine = {
            "link_complete": lecture.link_stage_state == "COMPLETE" and lecture.link_status in EXACT_LINK_STATUSES,
            "legacy_row_agrees": (lecture.legacy_drive_id, lecture.legacy_item_id) == (drive, item),
            "file_name_matches_link": bool(facts) and facts.name == lecture.recording_filename,
            "file_date_matches_lecture": bool(parsed) and parsed[1] == lecture.session_date.isoformat(),
            # Teams may truncate the subject in the file name: equal, or one a prefix of the other.
            "file_subject_matches_lecture": len(file_key) >= 8 and (
                lecture_key == file_key or lecture_key.startswith(file_key) or file_key.startswith(lecture_key)),
            "not_cancelled": not lecture.is_cancelled,
        }
        add(9, "source_is_correct_lecture", PASS if all(nine.values()) else FAIL, detail=nine)

        # 10 selected raw WebVTT
        ten = {"selections": lecture.selections_seen}
        if t is not None:
            computed = cap.sha256_text(t.raw_content)
            parsed_vtt = parse_combined_webvtt(t.raw_content)
            ten.update(artifact_id=t.artifact_id, content_version=t.content_version,
                       sha256_matches_stored=computed == t.stored_content_sha256,
                       parse_status=parsed_vtt.status, cue_count=len(parsed_vtt.cues))
            ok10 = computed == t.stored_content_sha256 and parsed_vtt.status == PARSED and parsed_vtt.cues
        else:
            ok10 = False
        add(10, "selected_raw_webvtt_available", PASS if ok10 else FAIL, detail=ten)

        # 13 single part
        thirteen = {"selected_part_count": t.selected_part_count if t else None,
                    "part_index": t.part_index if t else None,
                    "part_offset_ms": t.part_offset_ms if t else None,
                    "lecture_recording_parts_rows": lecture.recording_part_rows,
                    "link_status": lecture.link_status}
        single = (t is not None and t.selected_part_count == 1 and t.part_index == 1
                  and t.part_offset_ms == 0 and lecture.recording_part_rows <= 1
                  and lecture.link_status != "MULTIPART_RECORDING")
        add(13, "single_part_lecture", PASS if single else FAIL, detail=thirteen)

        # 11 alignment (reuses app.media.alignment.align on measured duration)
        if t is not None and facts is not None and facts.duration_seconds:
            alignment = align(
                file_name=facts.name, file_duration_seconds=facts.duration_seconds,
                recordings=_recordings_from_metadata(lecture.link_metadata),
                parts=[TranscriptPart(t.part_index, t.part_offset_ms, t.provider_created_at,
                                      t.provider_end_at, t.content_correlation_id)])
            pf.alignment = alignment
            window_diff = facts.duration_seconds - t.window_seconds
            cues = parse_combined_webvtt(t.raw_content).cues
            eleven = {"status": alignment.status, "code": alignment.code, "method": alignment.method,
                      "media_offset_seconds": alignment.media_offset_seconds,
                      "confidence": alignment.confidence,
                      "transcript_window_seconds": round(t.window_seconds, 3),
                      "mp4_duration_seconds": facts.duration_seconds,
                      "window_difference_seconds": round(window_diff, 3),
                      "first_cue_start_ms": cues[0].start_ms if cues else None,
                      "last_cue_end_ms": max(c.end_ms for c in cues) if cues else None}
            ok11 = (alignment.status == ALIGNED
                    and abs(window_diff) <= cfg.duration_tolerance_seconds and bool(cues))
            if ok11:
                pf.caption_offset_ms = -int(round(alignment.media_offset_seconds * 1000))
                first = cues[0].start_ms + pf.caption_offset_ms
                last = max(c.end_ms for c in cues) + pf.caption_offset_ms
                eleven.update(caption_offset_ms=pf.caption_offset_ms,
                              first_cue_not_negative=first >= 0,
                              last_cue_within_mp4=last / 1000.0 <= facts.duration_seconds)
                ok11 = first >= 0 and last / 1000.0 <= facts.duration_seconds
            add(11, "transcript_aligned", PASS if ok11 else FAIL, detail=eleven)
        else:
            add(11, "transcript_aligned", FAIL, detail={"error": "MISSING_TRANSCRIPT_OR_DURATION"})

        if drive and item:
            pf.identity = recording_identity(drive, item)
            pf.record_id = str(video_record_id(cfg.library_id, drive, item))
            pf.title = build_title(lecture.subject, lecture.session_date, lecture.lecture_id, pf.identity)

    def _duplicate_checks(self, pf: Preflight, add, library_videos) -> None:
        cfg = self.config
        if not pf.identity:
            add(12, "no_duplicate_import", FAIL, detail={"error": "NO_IDENTITY"})
            add(14, "no_existing_caption_overwritten", FAIL, detail={"error": "NO_IDENTITY"})
            return
        record = self.manifest.video(pf.record_id)
        others = [v for v in self.manifest.videos()
                  if int(v["bunny_library_id"]) == cfg.library_id
                  and v["bunny_video_record_id"] != pf.record_id
                  and v.get("import_status") not in ("PENDING",)]
        twelve = {"identity": pf.identity, "manifest_record": record.get("import_status") if record else None,
                  "other_pilot_videos_in_library": len(others)}
        try:
            matches = self.bunny.find_videos_with_token(pf.identity) if library_videos is not None else None
        except BunnyError as error:
            matches, twelve["search_error"] = None, error.code
        status = FAIL
        if matches is not None:
            exact = [v for v in matches if v.get("title") == pf.title]
            twelve.update(bunny_videos_with_token=len(matches), exact_title_matches=len(exact))
            known = str(record.get("bunny_video_id") or "").lower() if record else ""
            if len(others) >= cfg.max_videos_in_library:
                twelve["refused"] = "pilot allows only one imported video in this library"
            elif len(matches) > 1 or len(matches) != len(exact):
                twelve["refused"] = "token is ambiguous in the library"
            elif not matches:
                status = PASS
                twelve["plan"] = "resume" if record else "new import"
            elif known and known == str(exact[0]["guid"]).lower():
                status, twelve["plan"] = PASS, "resume existing pilot video"
                pf.existing_video = exact[0]
            elif not known:
                status, twelve["plan"] = PASS, "adopt the one video already carrying this exact title"
                pf.existing_video = exact[0]
            else:
                twelve["refused"] = "Bunny video differs from the manifest record"
        add(12, "no_duplicate_import", status, detail=twelve)

        # 14 existing captions
        fourteen = {"video_known": bool(pf.existing_video)}
        status14 = PASS
        if pf.existing_video:
            try:
                video = self.bunny.get_video(pf.existing_video["guid"])
                languages = [c.get("srclang") for c in (video.get("captions") or [])]
                fourteen["caption_languages"] = languages
                current = self.manifest.current_caption(cfg.library_id, video["guid"], cfg.language)
                if cfg.language in languages and not current:
                    status14, fourteen["refused"] = FAIL, "an English caption exists that this pilot did not publish"
            except BunnyError as error:
                status14, fourteen["error"] = FAIL, error.code
        add(14, "no_existing_caption_overwritten", status14, detail=fourteen)

    # =========================================================================== run
    def run(self) -> dict:
        pf = self.preflight()
        report = {"started_at": _now(), "lecture_id": self.config.lecture_id,
                  "library_id": self.config.library_id, "dry_run": self.config.dry_run,
                  "preflight": [c.as_dict() for c in pf.checks],
                  "library_settings": pf.library_settings}
        if not pf.ok:
            report.update(outcome=BLOCKED, blocking=pf.blocking())
            return self._finish(report)
        if self.config.dry_run:
            report.update(outcome=READY, note="dry run: preflight passed, nothing written")
            return self._finish(report)

        record = self._ensure_record(pf)
        record, stop = self._import(record, pf)
        if stop:
            return self._finish({**report, **stop, "video": self._public_video(record)})
        record, stop = self._await_encoding(record)
        if stop:
            return self._finish({**report, **stop, "video": self._public_video(record)})
        record, stop = self._verify_media(record, pf)
        if stop:
            return self._finish({**report, **stop, "video": self._public_video(record)})
        publication, stop = self._publish_caption(record, pf)
        report.update(video=self._public_video(record), caption=publication)
        if stop:
            return self._finish({**report, **stop})
        report.update(outcome=COMPLETED)
        return self._finish(report)

    def _finish(self, report: dict) -> dict:
        report["finished_at"] = _now()
        report["warnings"] = list(self.warnings)
        self.manifest.record_run({k: report.get(k) for k in ("outcome", "dry_run", "blocking", "reason")}
                                 | {"lecture_id": report["lecture_id"]})
        return report

    @staticmethod
    def _public_video(record: dict | None) -> dict | None:
        return dict(record) if record else None

    # ---------------------------------------------------------------- video import
    def _ensure_record(self, pf: Preflight) -> dict:
        record = self.manifest.video(pf.record_id)
        if record:
            return record
        lecture, facts = pf.lecture, pf.facts
        return self.manifest.put_video({
            "bunny_video_record_id": pf.record_id, "lecture_id": lecture.lecture_id,
            "recording_part_index": 1, "recording_drive_id": lecture.recording_drive_id,
            "recording_item_id": lecture.recording_item_id, "recording_identity": pf.identity,
            "bunny_library_id": self.config.library_id, "bunny_video_id": None,
            "video_title": pf.title, "import_method": "URL_FETCH", "import_status": "PENDING",
            "encoding_status": "NOT_STARTED", "source_file_name": facts.name,
            "source_mime_type": facts.mime_type, "source_size_bytes": facts.size_bytes,
            "source_duration_seconds": facts.duration_seconds, "attempt_count": 0,
            "last_error_code": None, "last_error": None, "metadata": {}}, event="VIDEO_RECORD_CREATED")

    def _save(self, record: dict, event: str, **changes) -> dict:
        record = {**record, **changes}
        return self.manifest.put_video(record, event=event)

    def _import(self, record: dict, pf: Preflight):
        if record.get("bunny_video_id"):
            return record, None
        status = record["import_status"]
        if status in ("FAILED", "REVIEW_REQUIRED"):
            return record, {"outcome": REVIEW_REQUIRED, "reason": f"video record is {status}; needs a human"}
        if pf.existing_video and status == "PENDING":
            return self._identified(record, pf.existing_video, event="VIDEO_ADOPTED"), None
        if status == "PENDING":
            record = self._save(record, "FETCH_SUBMITTING", import_status="SUBMITTING",
                                attempt_count=int(record.get("attempt_count") or 0) + 1,
                                import_submitted_at=_now())
            try:
                source = self._read_with_retry(self.media.source_file, record["recording_drive_id"],
                                               record["recording_item_id"])
            except Exception as error:               # nothing was submitted: safe to retry later
                code = getattr(error, "code", type(error).__name__)
                record = self._save(record, "FETCH_SOURCE_UNAVAILABLE", import_status="PENDING",
                                    last_error_code=code, last_error="temporary download URL unavailable")
                return record, {"outcome": WAITING, "reason": f"source URL unavailable ({code})"}
            if (source.item_id != record["recording_item_id"]
                    or (source.size_bytes and record.get("source_size_bytes")
                        and int(source.size_bytes) != int(record["source_size_bytes"]))):
                del source
                record = self._save(record, "FETCH_SOURCE_MISMATCH", import_status="PENDING",
                                    last_error_code="SOURCE_CHANGED",
                                    last_error="DriveItem changed between preflight and import")
                return record, {"outcome": REVIEW_REQUIRED, "reason": "source changed since preflight"}
            try:
                self.bunny.fetch_video(source.download_url, title=record["video_title"])
            except BunnyError as error:
                if error.uncertain:
                    record = self._save(record, "FETCH_UNCERTAIN", import_status="SUBMISSION_UNCERTAIN",
                                        last_error_code=error.code, last_error=str(error))
                else:
                    record = self._save(record, "FETCH_REFUSED", import_status="FAILED",
                                        last_error_code=error.code, last_error=str(error))
                    return record, {"outcome": REVIEW_REQUIRED, "reason": f"Bunny refused the fetch ({error.code})"}
            else:
                record = self._save(record, "FETCH_SUBMITTED", import_status="SUBMITTED",
                                    last_error_code=None, last_error=None)
            finally:
                source = None                         # drop the temporary URL immediately
        return self._identify(record)

    def _identify(self, record: dict):
        deadline = self._clock() + self.config.identify_timeout_seconds
        while True:
            try:
                matches = self.bunny.find_videos_with_token(record["recording_identity"])
            except BunnyError as error:
                matches = None
                self.warnings.append(f"identify: {error.code}")
            if matches is not None:
                exact = [v for v in matches if v.get("title") == record["video_title"]]
                if len(matches) > 1 or len(exact) > 1:
                    record = self._save(record, "IDENTIFY_AMBIGUOUS", import_status="REVIEW_REQUIRED",
                                        last_error_code="AMBIGUOUS_FETCHED_VIDEO",
                                        last_error=f"{len(matches)} videos carry the identity token")
                    return record, {"outcome": REVIEW_REQUIRED, "reason": "more than one Bunny video matched"}
                if len(exact) == 1:
                    return self._identified(record, exact[0], event="VIDEO_IDENTIFIED"), None
            if self._clock() >= deadline:
                return record, {"outcome": WAITING,
                                "reason": "fetched video not visible yet; rerun reconciles (never refetches)"}
            self._sleep(self.config.poll_interval_seconds)

    def _identified(self, record: dict, video: dict, *, event: str) -> dict:
        record = self._save(record, event, bunny_video_id=str(video["guid"]).lower(),
                            import_status="IDENTIFIED", video_identified_at=_now(),
                            bunny_status_code=video.get("status"),
                            encoding_status="IN_PROGRESS" if video.get("status") != FINISHED else "FINISHED")
        try:
            self.bunny.set_meta_tags(record["bunny_video_id"], {
                "kbc_lecture_id": record["lecture_id"], "kbc_recording_identity": record["recording_identity"],
                "kbc_record_id": record["bunny_video_record_id"]})
            record = self._save(record, "VIDEO_TAGGED", metadata={**record.get("metadata", {}),
                                                                  "meta_tags_set": True})
        except BunnyError as error:
            self.warnings.append(f"metaTags not set ({error.code}); identity still held by title + manifest")
        return record

    # ---------------------------------------------------------------- encoding
    def _await_encoding(self, record: dict):
        if record.get("encoding_status") in ("FINISHED", "VERIFIED"):
            return record, None
        deadline = self._clock() + self.config.encoding_timeout_seconds
        last = None
        while True:
            try:
                video = self.bunny.get_video(record["bunny_video_id"])
            except BunnyError as error:
                self.warnings.append(f"encoding poll: {error.code}")
                video = None
            if video is not None:
                snapshot = (video.get("status"), video.get("encodeProgress"))
                fields = self._video_fields(video)
                if video.get("status") in FAILED_STATUSES:
                    record = self._save(record, "ENCODING_FAILED", encoding_status="FAILED", **fields,
                                        last_error_code=f"BUNNY_{STATUS_NAMES.get(video.get('status'))}",
                                        last_error="Bunny reported a failed import/encode")
                    return record, {"outcome": REVIEW_REQUIRED, "reason": "Bunny encoding failed"}
                if video.get("status") == FINISHED and int(video.get("encodeProgress") or 0) >= 100:
                    record = self._save(record, "ENCODING_FINISHED", encoding_status="FINISHED", **fields,
                                        encoding_completed_at=_now())
                    return record, None
                if snapshot != last:
                    record = self._save(record, "ENCODING_PROGRESS", encoding_status="IN_PROGRESS", **fields)
                    last = snapshot
            if self._clock() >= deadline:
                return record, {"outcome": WAITING, "reason": "encoding still in progress; rerun resumes"}
            self._sleep(self.config.poll_interval_seconds)

    @staticmethod
    def _video_fields(video: dict) -> dict:
        return {"bunny_status_code": video.get("status"), "encode_progress": video.get("encodeProgress"),
                "bunny_length_seconds": video.get("length"), "bunny_width": video.get("width"),
                "bunny_height": video.get("height"), "output_codecs": video.get("outputCodecs"),
                "available_resolutions": video.get("availableResolutions"),
                "bunny_storage_size_bytes": video.get("storageSize")}

    def _verify_media(self, record: dict, pf: Preflight):
        if record.get("encoding_status") == "VERIFIED":
            return record, None
        cfg = self.config
        video = self.bunny.get_video(record["bunny_video_id"])
        resolutions = self.bunny.get_resolutions(record["bunny_video_id"])
        try:
            storage = self.bunny.get_storage(record["bunny_video_id"])
        except BunnyError as error:
            storage = {}
            self.warnings.append(f"storage breakdown unavailable ({error.code})")
        source_height = (pf.facts.video or {}).get("height") if pf.facts else None
        tier = pf.library_settings.get("EncodingTier")
        checks, failed = media_checks(video, resolutions, storage,
                                      source_duration_seconds=float(record["source_duration_seconds"]),
                                      source_height=source_height, encoding_tier=tier,
                                      duration_tolerance_seconds=cfg.duration_tolerance_seconds)
        foreign_caption = (cfg.language in checks["captions_present_before_publication"]
                           and not self.manifest.current_caption(cfg.library_id, record["bunny_video_id"],
                                                                 cfg.language))
        if foreign_caption:
            failed.append("unexpected_caption_present")
        fields = self._video_fields(video) | {"encoding_tier": str(tier) if tier is not None else None,
                                              "media_verification": checks}
        if failed:
            record = self._save(record, "MEDIA_VERIFICATION_FAILED", encoding_status="REVIEW_REQUIRED",
                                **fields, last_error_code="MEDIA_VERIFICATION_FAILED",
                                last_error=", ".join(failed))
            return record, {"outcome": REVIEW_REQUIRED, "reason": "media verification failed",
                            "failed_checks": failed}
        record = self._save(record, "MEDIA_VERIFIED", encoding_status="VERIFIED", **fields,
                            media_verified_at=_now())
        return record, None

    # ---------------------------------------------------------------- captions
    def _publish_caption(self, record: dict, pf: Preflight):
        cfg = self.config
        t = pf.lecture.transcript
        video_id = record["bunny_video_id"]
        if cap.sha256_text(t.raw_content) != t.stored_content_sha256:
            return None, {"outcome": REVIEW_REQUIRED, "reason": "stored transcript hash changed"}
        build = cap.build_caption(t.raw_content, applied_offset_ms=pf.caption_offset_ms)
        validation = cap.validate_caption(build, t.raw_content,
                                          media_duration_seconds=float(record["source_duration_seconds"]))
        bunny_length = record.get("bunny_length_seconds")
        last_end = validation.checks.get("last_cue_end_ms") or 0
        validation.checks["last_cue_within_bunny_length"] = (
            bunny_length is not None and last_end / 1000.0 <= float(bunny_length) + 1.0)
        if not validation.checks["last_cue_within_bunny_length"]:
            validation.failures.append("last_cue_within_bunny_length")
        if validation.failures:
            return None, {"outcome": REVIEW_REQUIRED, "reason": "caption validation failed",
                          "failed_checks": validation.failures}

        out_dir = Path(cfg.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        caption_path = out_dir / f"{record['recording_identity']}.{cfg.language}.vtt"
        caption_path.write_text(build.text, encoding="utf-8", newline="\n")

        publication_id = str(caption_publication_id(cfg.library_id, video_id, cfg.language,
                                                     build.sha256, build.transform_version))
        current = self.manifest.current_caption(cfg.library_id, video_id, cfg.language)
        publication = self.manifest.caption(publication_id)
        if current and current["publication_id"] == publication_id and publication \
                and publication.get("publication_status") == "VERIFIED":
            return publication, None                  # idempotent rerun
        if current and current["publication_id"] != publication_id:
            return publication, {"outcome": REVIEW_REQUIRED,
                                 "reason": "a different caption is current; replacement needs explicit approval"}
        video = self.bunny.get_video(video_id)
        languages = [c.get("srclang") for c in (video.get("captions") or [])]
        uncertain_retry = publication and publication.get("publication_status") in ("UPLOADING", "UPLOAD_UNCERTAIN")
        if cfg.language in languages and not uncertain_retry:
            return publication, {"outcome": REVIEW_REQUIRED,
                                 "reason": "an English caption already exists; never overwritten"}

        if publication is None:
            publication = self.manifest.put_caption({
                "publication_id": publication_id, "bunny_library_id": cfg.library_id,
                "bunny_video_id": video_id, "lecture_id": record["lecture_id"],
                "language_code": cfg.language, "caption_label": cfg.label,
                "source_selection_id": t.selection_id, "source_artifact_id": t.artifact_id,
                "source_content_version": t.content_version,
                "source_content_sha256": t.stored_content_sha256,
                "caption_sha256": build.sha256, "caption_bytes": build.size_bytes,
                "transform_version": build.transform_version, "alignment_version": ALIGNMENT_VERSION,
                "alignment_method": pf.alignment.method, "applied_offset_ms": build.applied_offset_ms,
                "source_cue_count": build.source_cue_count, "published_cue_count": build.cue_count,
                "removed_voice_tag_count": build.removed_voice_tags, "publication_status": "PREPARED",
                "attempt_count": 0, "bunny_validation": {}, "readback": {},
                "local_validation": validation.checks, "local_caption_file": caption_path.as_posix()},
                event="CAPTION_PREPARED")

        if cfg.language in languages and uncertain_retry:
            publication = self.manifest.put_caption({**publication, "publication_status": "PUBLISHED",
                                                     "published_at": _now()}, event="CAPTION_RECONCILED")
        else:
            publication = self.manifest.put_caption({
                **publication, "publication_status": "UPLOADING", "upload_started_at": _now(),
                "attempt_count": int(publication.get("attempt_count") or 0) + 1}, event="CAPTION_UPLOADING")
            try:
                result = self.bunny.add_caption(video_id, cfg.language, label=cfg.label, vtt_text=build.text)
            except BunnyError as error:
                status = "UPLOAD_UNCERTAIN" if error.uncertain else "FAILED"
                publication = self.manifest.put_caption({**publication, "publication_status": status,
                                                         "last_error_code": error.code,
                                                         "last_error": str(error)}, event="CAPTION_" + status)
                return publication, {"outcome": REVIEW_REQUIRED if status == "FAILED" else WAITING,
                                     "reason": f"caption upload {status.lower()} ({error.code})"}
            accepted = (result.get("http_status", 200) < 400 and result.get("success") is not False
                        and result.get("valid") is not False and not result.get("errorList"))
            publication = self.manifest.put_caption({
                **publication, "bunny_validation": {k: result.get(k) for k in (
                    "http_status", "success", "statusCode", "message", "valid", "errorList",
                    "warningList", "warningMessage")},
                "publication_status": "PUBLISHED" if accepted else "REJECTED",
                "published_at": _now() if accepted else None,
                "last_error_code": None if accepted else "CAPTION_REJECTED"},
                event="CAPTION_PUBLISHED" if accepted else "CAPTION_REJECTED")
            if not accepted:
                return publication, {"outcome": REVIEW_REQUIRED, "reason": "Bunny rejected the caption"}

        readback = self._read_back(video_id, build)
        verified = readback["registered"] and readback["label_matches"] and readback.get("content_check") != "MISMATCH"
        publication = self.manifest.put_caption({
            **publication, "readback": readback, "bunny_caption_version": readback.get("caption_version"),
            "publication_status": "VERIFIED" if verified else "PUBLISHED",
            "verified_at": _now() if verified else None}, event="CAPTION_VERIFIED" if verified else "CAPTION_UNVERIFIED")
        if not verified:
            return publication, {"outcome": REVIEW_REQUIRED, "reason": "caption read-back did not verify"}
        self.manifest.set_current_caption(cfg.library_id, video_id, cfg.language, publication_id)
        return self.manifest.caption(publication_id), None

    def _read_back(self, video_id: str, build) -> dict:
        cfg = self.config
        video = self.bunny.get_video(video_id)
        entry = next((c for c in (video.get("captions") or []) if c.get("srclang") == cfg.language), None)
        readback = {"registered": entry is not None, "label_matches": bool(entry) and entry.get("label") == cfg.label,
                    "caption_version": entry.get("version") if entry else None,
                    "caption_languages": [c.get("srclang") for c in (video.get("captions") or [])]}
        if entry is None or not cfg.cdn_hostname:
            readback["content_check"] = "NOT_ATTEMPTED"
            return readback
        # Public caption path on the library's pull zone; a token-protected zone answers 403.
        url = f"https://{cfg.cdn_hostname}/{video_id}/captions/{cfg.language}.vtt?ver={entry.get('version') or 0}"
        try:
            with self._open(urllib.request.Request(url, headers={"Accept": "text/vtt"}), 60) as response:
                served = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            readback["content_check"] = "CDN_UNAVAILABLE"
            readback["cdn_status"] = getattr(error, "code", None)
            return readback
        readback["served_sha256"] = cap.sha256_text(served)
        readback["served_identical"] = readback["served_sha256"] == build.sha256
        ours, theirs = parse_combined_webvtt(build.text).cues, parse_combined_webvtt(served).cues
        same = len(ours) == len(theirs) and all(
            a.start_ms == b.start_ms and a.end_ms == b.end_ms and a.text == b.text for a, b in zip(ours, theirs))
        readback["served_cue_count"] = len(theirs)
        readback["content_check"] = "IDENTICAL" if readback["served_identical"] else ("EQUIVALENT" if same else "MISMATCH")
        return readback
