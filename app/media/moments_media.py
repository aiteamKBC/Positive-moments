"""
One lecture, end to end, up to (never including) spending money.

    pipeline resolver  -> canonical document + RECORDING_LINK answer
    analysis           -> reuse / legacy import / selector+verifier (optional)
    recording source   -> the linked file's durable identity (no matcher here)
    alignment          -> offset PROVEN from measured duration, or refused
    clip plan          -> +/- padding, clamped to the file
    jobs               -> one pre-render job per moment (READY_TO_RENDER,
                          WAITING_FOR_RECORDING, WAITING_FOR_ALIGNMENT,
                          REVIEW_REQUIRED_ALIGNMENT)
    snapshot           -> positive_moment_lecture_states, for the dashboard

Rendering starts only with `queue_render`, which an operator reaches through
an explicit "Render" action. Analysis never submits a render; a service
restart never submits a render.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.media import source as src
from app.media.alignment import (
    ALIGNED,
    REVIEW_REQUIRED_ALIGNMENT,
    WAITING_FOR_ALIGNMENT,
    Alignment,
    RecordingRef,
    TranscriptPart,
    align,
)
from app.media.clip_plan import PlanRefused, plan_clip, plan_fingerprint
from app.media.delivery import DeliveryError
from app.media.render.base import RenderPolicy, estimate_credits
from app.positive_moments import service as analysis
from app.positive_moments.policy import ANALYSIS_POLICY_VERSION

TRANSCRIPT_READY = "READY"
TRANSCRIPT_NOT_READY = "NOT_READY"

MEDIA_NONE = "NO_MEDIA"
FAILED_STATES = ("FAILED_RETRYABLE", "FAILED_FINAL")


@dataclass
class MediaSettings:
    provider: str = "creatomate"
    padding_before: float = 60.0
    padding_after: float = 60.0
    render_policy: RenderPolicy = field(default_factory=RenderPolicy)


@dataclass
class LectureOutcome:
    lecture_id: str
    state: dict
    analysis_outcome: str | None = None
    jobs_planned: int = 0
    provider_calls: int = 0
    detail: dict = field(default_factory=dict)


def _sha(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def media_state(counts: dict) -> str:
    """One word for a lecture's clips, most urgent first."""
    if not counts:
        return MEDIA_NONE
    has = lambda *names: any(counts.get(n, {}).get("count") for n in names)  # noqa: E731
    if has(*FAILED_STATES):
        return "FAILED"
    if has("REVIEW_REQUIRED_ALIGNMENT"):
        return "NEEDS_REVIEW"
    if has("UPLOAD_PENDING", "UPLOADING", "RENDER_SUCCEEDED"):
        return "UPLOADING"
    if has("QUEUED", "RENDERING"):
        return "RENDERING"
    if has("READY_TO_RENDER"):
        return "READY_TO_RENDER"
    if has("WAITING_FOR_ALIGNMENT"):
        return "WAITING_FOR_ALIGNMENT"
    if has("WAITING_FOR_RECORDING"):
        return "WAITING_FOR_RECORDING"
    if has("COMPLETED"):
        return "COMPLETED"
    return MEDIA_NONE


class PositiveMomentMediaService:

    def __init__(self, *, resolver, moments_repository, media_repository, analyzer=None,
                 sharepoint=None, recordings_lookup=None, settings: MediaSettings | None = None):
        self.resolver = resolver
        self.moments = moments_repository
        self.media = media_repository
        self.analyzer = analyzer
        self.sharepoint = sharepoint
        # callable(connection, lecture_id, legacy_session_id) -> tuple[RecordingRef]
        self.recordings_lookup = recordings_lookup
        self.settings = settings or MediaSettings()

    # ------------------------------------------------------------------------
    # the lecture
    # ------------------------------------------------------------------------

    def process(self, connection, lecture_id, *, analyze: bool, allow_model: bool,
                persist_jobs: bool, run_id=None, probe_alignment: bool = False) -> LectureOutcome:
        lecture = self.media.lecture(connection, lecture_id)
        state = self.resolver.for_lecture(connection, str(lecture_id))
        stages = state.get("stages") or {}
        cues_stage = stages.get("CANONICAL_CUES") or {}
        recording_stage = stages.get("RECORDING_LINK") or {}
        legacy_session_id = recording_stage.get("legacy_session_id")
        document_id = cues_stage.get("document_id") if cues_stage.get("state") == "COMPLETE" else None
        outcome = LectureOutcome(lecture_id=str(lecture_id), state={})

        # -- analysis --------------------------------------------------------
        current = latest = None
        transcript = None
        if document_id:
            transcript = self.moments.transcript(connection, document_id)
            latest = self.moments.latest_analysis(connection, lecture_id)
            if transcript is not None:
                current = self.moments.current_analysis(
                    connection, lecture_id, document_id=document_id,
                    transcript_fingerprint=transcript.fingerprint)
            if analyze and self.analyzer is not None and (
                    current is None or current["status"] == analysis.FAILED):
                result = self.analyzer.analyze(connection, lecture_id=lecture_id,
                                               document_id=document_id,
                                               legacy_session_id=legacy_session_id,
                                               allow_model=allow_model)
                outcome.analysis_outcome = result.status
                outcome.provider_calls += result.provider_calls
                outcome.detail["analysis_detail"] = result.detail
                if result.analysis and result.analysis["status"] != analysis.FAILED:
                    current = result.analysis
                latest = self.moments.latest_analysis(connection, lecture_id)
        analysis_state = (analysis.analysis_state(latest, current) if document_id
                          else analysis.NOT_ANALYZED)
        moments = self.moments.moments(connection, current["analysis_id"]) if current else []

        # -- recording -------------------------------------------------------
        legacy = self.media.legacy_recording(connection, legacy_session_id)
        coded = self.media.coded_recording_link(connection, lecture_id)
        recording = src.resolve_recording(state=state, legacy=legacy, coded_link=coded,
                                          is_cancelled=bool((lecture or {}).get("is_cancelled")))
        duration = recording.duration_seconds
        file_name = recording.file_name
        if recording.ready and duration is None and self.sharepoint is not None:
            try:
                facts = self.sharepoint.item_facts(recording.drive_id, recording.item_id)
                duration = facts.get("duration_seconds")
                file_name = file_name or facts.get("name")
                outcome.detail["recording_facts"] = "READ_FROM_GRAPH"
            except DeliveryError as error:
                outcome.detail["recording_facts"] = error.code

        # -- alignment + plans ------------------------------------------------
        alignment = None
        if recording.ready and (moments or probe_alignment):
            recordings = recording.recordings
            if not recordings and self.recordings_lookup is not None:
                recordings = self.recordings_lookup(connection, lecture_id, legacy_session_id)
            parts = [TranscriptPart(part_index=row["part_index"],
                                    part_offset_ms=int(row["part_offset_ms"] or 0),
                                    provider_created_at=row["provider_created_at"],
                                    provider_end_at=row["provider_end_at"],
                                    content_correlation_id=row["content_correlation_id"])
                     for row in self.media.transcript_parts(connection, lecture_id)]
            alignment = align(file_name=file_name, file_duration_seconds=duration,
                              recordings=list(recordings or ()), parts=parts)
        plans = [self._plan(moment, recording, alignment) for moment in moments]
        if persist_jobs:
            for moment, planned in zip(moments, plans):
                if self._persist(connection, moment, planned):
                    outcome.jobs_planned += 1

        # -- snapshot ---------------------------------------------------------
        counts = self.media.job_counts_by_lecture(connection, [lecture_id]).get(str(lecture_id), {})
        ready = [p for p in plans if p["status"] == "READY_TO_RENDER"]
        if persist_jobs or counts:
            planned_jobs = counts.get("READY_TO_RENDER", {})
            planned_count = planned_jobs.get("count", 0)
            planned_seconds = planned_jobs.get("seconds", 0.0)
            planned_credits = planned_jobs.get("credits", 0.0)
        else:
            planned_count = len(ready)
            planned_seconds = sum(p["duration"] for p in ready)
            planned_credits = sum(p["credits"] for p in ready)
        needs_review = (analysis_state == analysis.REVIEW_REQUIRED
                        or recording_stage.get("state") == "REVIEW_REQUIRED"
                        or any(p["status"] == REVIEW_REQUIRED_ALIGNMENT for p in plans)
                        or bool(counts.get("FAILED_FINAL", {}).get("count"))
                        or bool(counts.get("REVIEW_REQUIRED_ALIGNMENT", {}).get("count")))
        snapshot = {
            "lecture_id": str(lecture_id),
            "session_date": (lecture or {}).get("session_date") or state.get("session_date"),
            "subject": (lecture or {}).get("subject") or state.get("subject"),
            "trainer": (legacy or {}).get("trainer") or (
                self.media.trainer_for(connection, lecture) if lecture else None),
            "legacy_session_id": legacy_session_id,
            "transcript_state": (TRANSCRIPT_READY if document_id else
                                 "NOT_APPLICABLE" if (lecture or {}).get("is_cancelled")
                                 else TRANSCRIPT_NOT_READY),
            "transcript_document_id": document_id,
            "recording_state": recording.state, "recording_status": recording.status,
            "recording_reason": (str(recording.reason)[:500] if recording.reason else None),
            "recording_rule": recording.rule, "recording_file_name": file_name,
            "recording_duration_seconds": duration,
            "recording_checked_at": recording.checked_at,
            "analysis_state": analysis_state,
            "analysis_id": current["analysis_id"] if current else None,
            "moment_count": len(moments),
            "media_state": (media_state(counts) if counts else
                            (media_state({p["status"]: {"count": 1} for p in plans})
                             if plans else MEDIA_NONE)),
            "media_counts": {k: v["count"] for k, v in counts.items()},
            "completed_clip_count": counts.get("COMPLETED", {}).get("count", 0),
            "planned_clip_count": planned_count,
            "planned_output_seconds": round(planned_seconds, 3),
            "estimated_credits": round(planned_credits, 3),
            "needs_review": needs_review, "last_run_id": run_id,
        }
        outcome.state = snapshot
        outcome.detail["alignment"] = alignment.as_dict() if alignment else None
        return outcome

    # ------------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------------

    def _plan(self, moment: dict, recording, alignment: Alignment | None) -> dict:
        fp_moment = moment["moment_fingerprint"]
        base = {"moment": moment, "duration": 0.0, "credits": 0.0,
                "evidence_start_ms": int(moment["evidence_start_ms"]),
                "evidence_end_ms": int(moment["evidence_end_ms"])}
        if not recording.ready:
            return {**base, "status": "WAITING_FOR_RECORDING",
                    "fingerprint": _sha("waiting-recording", fp_moment),
                    "error_code": recording.status, "error_message": recording.reason,
                    "alignment": None, "plan": None}
        source = {"source_drive_id": recording.drive_id, "source_item_id": recording.item_id,
                  "source_file_name": recording.file_name}
        if alignment is None or alignment.status == WAITING_FOR_ALIGNMENT:
            code = alignment.code if alignment else "ALIGNMENT_NOT_ATTEMPTED"
            return {**base, **source, "status": "WAITING_FOR_ALIGNMENT",
                    "fingerprint": _sha("waiting-alignment", fp_moment, recording.item_id),
                    "error_code": code, "error_message": None,
                    "alignment": alignment, "plan": None}
        if alignment.status != ALIGNED:
            return {**base, **source, "status": REVIEW_REQUIRED_ALIGNMENT,
                    "fingerprint": _sha("review-alignment", fp_moment, recording.item_id,
                                        alignment.code),
                    "error_code": alignment.code, "error_message": None,
                    "alignment": alignment, "plan": None}
        try:
            plan = plan_clip(evidence_start_ms=base["evidence_start_ms"],
                             evidence_end_ms=base["evidence_end_ms"], alignment=alignment,
                             padding_before=self.settings.padding_before,
                             padding_after=self.settings.padding_after)
        except PlanRefused as refused:
            return {**base, **source, "status": REVIEW_REQUIRED_ALIGNMENT,
                    "fingerprint": _sha("review-plan", fp_moment, recording.item_id,
                                        refused.code),
                    "error_code": refused.code, "error_message": None,
                    "alignment": alignment, "plan": None, "refusal": refused.detail}
        policy = self.settings.render_policy
        fingerprint = plan_fingerprint(
            moment_fingerprint=fp_moment, source_drive_id=recording.drive_id,
            source_item_id=recording.item_id, alignment=alignment, plan=plan,
            render_policy=policy.as_dict(), provider=self.settings.provider)
        return {**base, **source, "status": "READY_TO_RENDER", "fingerprint": fingerprint,
                "error_code": None, "error_message": None, "alignment": alignment,
                "plan": plan, "duration": plan.duration_seconds,
                "credits": estimate_credits(plan.duration_seconds, policy)}

    def _job_values(self, moment: dict, planned: dict) -> dict:
        alignment, plan = planned.get("alignment"), planned.get("plan")
        values = {
            "moment_id": moment["moment_id"], "lecture_id": moment["lecture_id"],
            "provider": self.settings.provider, "plan_fingerprint": planned["fingerprint"],
            "source_drive_id": planned.get("source_drive_id"),
            "source_item_id": planned.get("source_item_id"),
            "source_file_name": planned.get("source_file_name"),
            "source_duration_seconds": alignment.file_duration_seconds if alignment else None,
            "evidence_start_ms": planned["evidence_start_ms"],
            "evidence_end_ms": planned["evidence_end_ms"],
            "media_offset_seconds": alignment.media_offset_seconds if alignment else None,
            "alignment_method": alignment.method if alignment else None,
            "alignment_confidence": alignment.confidence if alignment else None,
            "alignment_status": alignment.status if alignment else "NOT_ATTEMPTED",
            "alignment_detail": {**(alignment.as_dict() if alignment else {}),
                                 **({"refusal": planned["refusal"]}
                                    if planned.get("refusal") else {})},
            "render_policy": self.settings.render_policy.as_dict(),
            "status": planned["status"], "error_code": planned.get("error_code"),
            "error_message": (str(planned.get("error_message"))[:500]
                              if planned.get("error_message") else None),
            "error_stage": ("RECORDING" if planned["status"] == "WAITING_FOR_RECORDING" else
                            "ALIGNMENT" if planned["status"] != "READY_TO_RENDER" else None),
        }
        if plan is not None:
            values.update({
                "requested_media_start_seconds": plan.requested_start_seconds,
                "requested_media_end_seconds": plan.requested_end_seconds,
                "actual_media_start_seconds": plan.actual_start_seconds,
                "actual_media_end_seconds": plan.actual_end_seconds,
                "padding_before_requested_seconds": plan.padding_before_requested,
                "padding_after_requested_seconds": plan.padding_after_requested,
                "padding_before_applied_seconds": plan.padding_before_applied,
                "padding_after_applied_seconds": plan.padding_after_applied,
                "estimated_credits": planned["credits"]})
        return values

    def _persist(self, connection, moment: dict, planned: dict) -> bool:
        """
        Make the moment's ACTIVE job the one this plan describes. True if changed.

        Same plan -> the existing job stands (idempotent; a COMPLETED job is
        never re-rendered). A different plan supersedes a job that has not
        started spending (or has finished/failed); a job in flight is left
        alone until it settles.
        """
        active = self.media.active_job_for_moment(connection, moment["moment_id"])
        if active and active["plan_fingerprint"] == planned["fingerprint"]:
            if active["status"] in ("WAITING_FOR_RECORDING", "WAITING_FOR_ALIGNMENT",
                                    "REVIEW_REQUIRED_ALIGNMENT") and (
                    active["error_code"] != planned.get("error_code")):
                self.media.transition(connection, active["job_id"], expected=active["status"],
                                      status=active["status"], event_stage="PLANNING",
                                      error_code=planned.get("error_code"))
            return False
        if active and active["status"] in ("QUEUED", "RENDERING", "RENDER_SUCCEEDED",
                                           "UPLOAD_PENDING", "UPLOADING"):
            return False
        if active:
            self.media.transition(connection, active["job_id"], expected=active["status"],
                                  status="SUPERSEDED", event_stage="PLANNING",
                                  event_detail={"superseded_by_plan": planned["fingerprint"][:12]})
        existing = self.media.job_by_plan(connection, planned["fingerprint"])
        if existing:
            revived = ("COMPLETED" if self.media.asset_by_plan(connection, planned["fingerprint"])
                       else planned["status"])
            self.media.transition(connection, existing["job_id"], expected="SUPERSEDED",
                                  status=revived, event_stage="PLANNING")
            return True
        job = self.media.insert_job(connection, self._job_values(moment, planned))
        self.media.event(connection, job["job_id"], "EVIDENCE_SELECTED", "PLANNED",
                         {"moment_index": moment["moment_index"],
                          "analysis_policy_version": ANALYSIS_POLICY_VERSION})
        if planned["status"] != "WAITING_FOR_RECORDING":
            self.media.event(connection, job["job_id"], "RECORDING_RESOLVED", "OK", {})
        if planned.get("alignment") is not None:
            self.media.event(connection, job["job_id"], "ALIGNMENT_CALCULATED",
                             planned["alignment"].status,
                             {"code": planned["alignment"].code,
                              "method": planned["alignment"].method})
        return True

    # ------------------------------------------------------------------------
    # operator actions (never automatic)
    # ------------------------------------------------------------------------

    def queue_render(self, connection, *, lecture_id=None, moment_id=None) -> int:
        """READY_TO_RENDER -> QUEUED. The only way money starts being spent."""
        jobs = (self.media.jobs_for_lecture(connection, lecture_id) if lecture_id else
                [job for job in [self.media.active_job_for_moment(connection, moment_id)] if job])
        queued = 0
        for job in jobs:
            if moment_id and str(job["moment_id"]) != str(moment_id):
                continue
            if self.media.asset_by_plan(connection, job["plan_fingerprint"]):
                continue            # already delivered for this exact plan
            if self.media.transition(connection, job["job_id"], expected="READY_TO_RENDER",
                                     status="QUEUED", event_stage="RENDER_REQUESTED",
                                     submitted_at=None, provider_render_id=None):
                queued += 1
        return queued

    def retry_failed(self, connection, *, lecture_id=None, moment_id=None) -> int:
        """Re-arm failed jobs. FAILED_FINAL is re-armed only for recoverable causes."""
        from datetime import datetime, timezone
        jobs = (self.media.jobs_for_lecture(connection, lecture_id) if lecture_id else
                [job for job in [self.media.active_job_for_moment(connection, moment_id)] if job])
        armed = 0
        for job in jobs:
            if job["status"] == "FAILED_RETRYABLE" or (
                    job["status"] == "FAILED_FINAL" and job["error_code"] in RECOVERABLE_FINAL):
                resume = job["resume_stage"] or "SUBMIT"
                if self.media.transition(
                        connection, job["job_id"], expected=job["status"],
                        status="FAILED_RETRYABLE", event_stage="RETRY_REQUESTED",
                        resume_stage=resume, next_retry_at=datetime.now(timezone.utc),
                        attempt_count=min(job["attempt_count"], job["max_attempts"] - 1)):
                    armed += 1
        return armed


# Final failures an operator may explicitly retry: the cause is outside the
# plan (a transient provider or network condition, or an unknown submit
# outcome that a human has now checked).
RECOVERABLE_FINAL = frozenset({
    "SUBMIT_OUTCOME_UNKNOWN", "ATTEMPTS_EXHAUSTED", "RENDER_TIMEOUT",
    "CREATOMATE_UNREACHABLE", "RENDER_FAILED_AT_PROVIDER",
    "SOURCE_GRAPH_PERMISSION_DENIED",
})


def lookup_graph_recordings(metadata_gateway, links_repository):
    """
    For a lecture linked by the legacy branch (no coded metadata): read the
    Graph callRecordings for its call, read-only, through the SAME organizer
    route Recording Link uses.
    """
    from app.transcripts.identity import teams_call_id

    def lookup(connection, lecture_id, legacy_session_id):
        target = links_repository.target(connection, lecture_id, legacy_session_id)
        if target is None or not target.meeting_id or not target.meeting_lookup_user_id:
            return ()
        call_id = teams_call_id(target.legacy_session_id or "")
        if not call_id:
            return ()
        result = metadata_gateway.lookup(meeting_lookup_user_id=target.meeting_lookup_user_id,
                                         meeting_id=target.meeting_id, call_id=call_id)
        return tuple(RecordingRef(r.recording_id, r.created_at, r.end_at,
                                  r.content_correlation_id)
                     for r in (result.logical_recordings if result.resolvable else ()))
    return lookup
