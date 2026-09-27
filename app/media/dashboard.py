"""
The Positive Moments Media read model: what the website shows.

Read-only and cheap: persisted lecture snapshots plus LIVE job/asset counts,
so a clip that completed a second ago is counted now, not at the next preview.
No Graph call, no model call, no provider call happens here - these run
inside the Operations API's read-only transactions.

Every counter carries a `filter` key and every row lists the keys it matches,
so a number on the dashboard is always a click away from its lectures.
"""
from __future__ import annotations

from app.media.render.base import RenderPolicy, estimate_credits
from app.positive_moments.policy import ANALYSIS_POLICY_VERSION

COUNTERS = (
    ("all", "Lectures in range"),
    ("transcript_ready", "Transcript ready"),
    ("recording_ready", "Recording ready"),
    ("waiting_for_recording", "Waiting for recording"),
    ("not_analyzed", "Not analyzed"),
    ("analyzing", "Analyzing"),
    ("no_positive_moments", "No positive moments"),
    ("moments_found", "Positive moments found"),
    ("ready_to_render", "Ready to render"),
    ("rendering", "Rendering"),
    ("uploading", "Uploading"),
    ("clips_completed", "Clips completed"),
    ("needs_review", "Needs review"),
    ("failed", "Failed"),
    ("not_previewed", "Not yet previewed"),
)


def _count(counts: dict, *names) -> int:
    return sum(int(counts.get(name, {}).get("count", 0)) for name in names)


def row_filters(row: dict, jobs: dict, *, analyzing: bool) -> list[str]:
    keys = ["all"]
    if row.get("transcript_state") is None:
        keys.append("not_previewed")
    if row.get("transcript_state") == "READY":
        keys.append("transcript_ready")
    if row.get("recording_state") == "READY":
        keys.append("recording_ready")
    if row.get("recording_state") == "WAITING_FOR_RECORDING":
        keys.append("waiting_for_recording")
    state = row.get("analysis_state")
    if analyzing:
        keys.append("analyzing")
    elif state in (None, "NOT_ANALYZED", "STALE") and row.get("transcript_state") == "READY":
        keys.append("not_analyzed")
    if state == "NO_POSITIVE_MOMENTS":
        keys.append("no_positive_moments")
    if state == "MOMENTS_FOUND":
        keys.append("moments_found")
    if _count(jobs, "READY_TO_RENDER"):
        keys.append("ready_to_render")
    if _count(jobs, "QUEUED", "RENDERING"):
        keys.append("rendering")
    if _count(jobs, "RENDER_SUCCEEDED", "UPLOAD_PENDING", "UPLOADING"):
        keys.append("uploading")
    if _count(jobs, "COMPLETED"):
        keys.append("clips_completed")
    if row.get("needs_review") or state == "REVIEW_REQUIRED" or _count(
            jobs, "REVIEW_REQUIRED_ALIGNMENT", "FAILED_FINAL"):
        keys.append("needs_review")
    if state == "FAILED" or _count(jobs, "FAILED_RETRYABLE", "FAILED_FINAL"):
        keys.append("failed")
    return keys


def _plain(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is not None and type(value).__name__ == "Decimal":
        return float(value)
    if value is not None and type(value).__name__ == "UUID":
        return str(value)
    return value


def _clean(row: dict) -> dict:
    return {key: _plain(value) for key, value in row.items()}


def dashboard(connection, *, media_repository, date_from, date_to,
              render_policy: RenderPolicy | None = None) -> dict:
    policy = render_policy or RenderPolicy()
    rows = media_repository.states(connection, date_from, date_to)
    ids = [str(row["lecture_id"]) for row in rows]
    jobs = media_repository.job_counts_by_lecture(connection, ids)
    active = media_repository.active_runs(connection)
    analyzing_ids = set()
    for run in active:
        if run["mode"] in ("ANALYZE", "RETRY_FAILED"):
            done = media_repository.run_items_done(connection, run["run_id"])
            scope = ([str(run["lecture_id"])] if run["lecture_id"] else
                     media_repository.lectures_in_range(connection, run["requested_from"],
                                                        run["requested_to"]))
            analyzing_ids.update(lecture for lecture in scope if lecture not in done)
    counters = {key: 0 for key, _ in COUNTERS}
    lectures = []
    ready_count = ready_seconds = ready_credits = 0.0
    for row in rows:
        lecture_jobs = jobs.get(str(row["lecture_id"]), {})
        keys = row_filters(row, lecture_jobs, analyzing=str(row["lecture_id"]) in analyzing_ids)
        for key in keys:
            counters[key] += 1
        ready = lecture_jobs.get("READY_TO_RENDER")
        if ready:
            ready_count += ready["count"]
            ready_seconds += ready["seconds"]
            ready_credits += ready["credits"]
        elif not lecture_jobs and row.get("planned_clip_count"):
            # Previewed only: the plan exists in the snapshot, not as jobs yet.
            ready_count += row["planned_clip_count"]
            ready_seconds += float(row.get("planned_output_seconds") or 0)
            ready_credits += float(row.get("estimated_credits") or 0)
        lectures.append({**_clean(row), "filters": keys,
                         "job_counts": {k: v["count"] for k, v in lecture_jobs.items()},
                         "completed_clips": _count(lecture_jobs, "COMPLETED"),
                         "review_items": _count(lecture_jobs, "REVIEW_REQUIRED_ALIGNMENT",
                                                "FAILED_FINAL")})
    return {
        "date_from": str(date_from), "date_to": str(date_to),
        "analysis_policy_version": ANALYSIS_POLICY_VERSION,
        "counters": [{"filter": key, "label": label, "count": counters[key]}
                     for key, label in COUNTERS
                     if key != "not_previewed" or counters[key]],
        "cost_preview": {
            "is_estimate": True,
            "safe_clips_planned": int(ready_count),
            "planned_output_minutes": round(ready_seconds / 60.0, 1),
            "estimated_credits": round(ready_credits, 1),
            "formula": "max(1, width x height x fps x seconds / 100,000,000) per clip",
            "render_policy": policy.as_dict(),
            "credits_per_output_minute": estimate_credits(60, policy),
        },
        "active_runs": [_clean(run) for run in active],
        "recent_runs": [_clean(run) for run in media_repository.recent_runs(connection, 10)],
        "lectures": lectures,
    }


TIMELINE_STAGES = (
    ("EVIDENCE_SELECTED", "Evidence selected"),
    ("VERIFIED", "Verified"),
    ("RECORDING_RESOLVED", "Recording resolved"),
    ("ALIGNMENT_CALCULATED", "Alignment calculated"),
    ("RENDER_SUBMITTED", "Render submitted"),
    ("RENDER_COMPLETED", "Render completed"),
    ("UPLOADED", "Uploaded to SharePoint"),
    ("COMPLETE", "Complete"),
)


def timeline(moment: dict, job: dict | None, events: list) -> list:
    seen = {}
    for event in events:
        seen.setdefault(event["stage"], event)
    steps = []
    for stage, label in TIMELINE_STAGES:
        if stage == "VERIFIED":
            steps.append({"stage": stage, "label": label, "done": True,
                          "at": _plain(moment.get("created_at")),
                          "detail": moment.get("verifier_verdict")})
            continue
        event = seen.get(stage)
        steps.append({"stage": stage, "label": label, "done": event is not None,
                      "at": _plain(event["created_at"]) if event else None,
                      "detail": (event.get("status") if event else None)})
    if job and job["status"] in ("FAILED_RETRYABLE", "FAILED_FINAL",
                                 "REVIEW_REQUIRED_ALIGNMENT", "WAITING_FOR_RECORDING",
                                 "WAITING_FOR_ALIGNMENT"):
        steps.append({"stage": "BLOCKED", "label": "Stopped at " + (job["error_stage"] or "—"),
                      "done": False, "at": _plain(job["updated_at"]),
                      "detail": job["error_code"],
                      "next_retry_at": _plain(job["next_retry_at"])})
    return steps


JOB_PUBLIC_FIELDS = (
    "job_id", "status", "provider", "source_file_name", "source_duration_seconds",
    "evidence_start_ms", "evidence_end_ms", "media_offset_seconds", "alignment_method",
    "alignment_confidence", "alignment_status", "requested_media_start_seconds",
    "requested_media_end_seconds", "actual_media_start_seconds", "actual_media_end_seconds",
    "padding_before_requested_seconds", "padding_after_requested_seconds",
    "padding_before_applied_seconds", "padding_after_applied_seconds", "estimated_credits",
    "attempt_count", "max_attempts", "next_retry_at", "provider_status", "submitted_at",
    "render_completed_at", "output_duration_seconds", "output_size_bytes", "error_stage",
    "error_code", "error_message", "updated_at")


def lecture_media(connection, *, lecture_id, media_repository, moments_repository) -> dict:
    """One lecture's analysis, recording and moments. No URL of any kind."""
    state = media_repository.state(connection, lecture_id)
    latest = moments_repository.latest_analysis(connection, lecture_id)
    analysis_id = (state or {}).get("analysis_id")
    if analysis_id is None and latest and latest["status"] != "FAILED":
        # No dashboard snapshot yet: the newest settled analysis is the answer.
        analysis_id = latest["analysis_id"]
    current = None
    if analysis_id:
        current = next((a for a in [latest] if a and str(a["analysis_id"]) == str(analysis_id)),
                       None) or {"analysis_id": analysis_id}
    moments = moments_repository.moments(connection, analysis_id) if analysis_id else []
    jobs = media_repository.jobs_for_lecture(connection, lecture_id)
    by_moment = {}
    for job in jobs:
        by_moment[str(job["moment_id"])] = job
    assets = {str(asset["job_id"]): asset
              for asset in media_repository.assets_for_lecture(connection, lecture_id)}
    events = media_repository.events(connection, [job["job_id"] for job in jobs])
    events_by_job = {}
    for event in events:
        events_by_job.setdefault(str(event["job_id"]), []).append(event)

    shown = []
    for moment in moments:
        job = by_moment.get(str(moment["moment_id"]))
        asset = assets.get(str(job["job_id"])) if job else None
        shown.append({
            "moment_id": str(moment["moment_id"]), "moment_index": moment["moment_index"],
            "category": moment["category"], "positive_speakers": moment["positive_speakers"],
            "all_speakers": moment["all_speakers"],
            "conversation_type": moment["conversation_type"],
            "trainer_included": moment["trainer_included"],
            "positive_quote": moment["positive_quote"], "dialogue": moment["dialogue"],
            "start_cue": moment["start_cue"], "end_cue": moment["end_cue"],
            "evidence_start_ms": moment["evidence_start_ms"],
            "evidence_end_ms": moment["evidence_end_ms"],
            "reason": moment["reason"], "source": moment["source"],
            "selector_confidence": _plain(moment["selector_confidence"]),
            "verifier_verdict": moment["verifier_verdict"],
            "verifier_confidence": _plain(moment["verifier_confidence"]),
            "job": ({k: _plain(job[k]) for k in JOB_PUBLIC_FIELDS} if job else None),
            "asset": ({"asset_id": str(asset["asset_id"]),
                       "output_filename": asset["output_filename"],
                       "duration_seconds": _plain(asset["duration_seconds"]),
                       "size_bytes": asset["size_bytes"],
                       "verification_status": asset["verification_status"],
                       "completed_at": _plain(asset["completed_at"])} if asset else None),
            "timeline": timeline(moment, job, events_by_job.get(str(job["job_id"]), [])
                                 if job else []),
        })
    analysis = None
    if latest:
        analysis = {
            "analysis_id": str(latest["analysis_id"]), "status": latest["status"],
            "source": latest["source"], "policy_version": latest["analysis_policy_version"],
            "current_policy_version": ANALYSIS_POLICY_VERSION,
            "document_id": str(latest["document_id"]),
            "transcript_fingerprint": latest["transcript_fingerprint"][:12],
            "candidate_count": latest["candidate_count"],
            "structurally_valid_count": latest["structurally_valid_count"],
            "accepted_count": latest["accepted_count"],
            "rejection_summary": latest["rejection_summary"],
            "error_code": latest["error_code"],
            "analyzed_at": _plain(latest["completed_at"]),
            "is_current": bool(current and str(current["analysis_id"]) == str(latest["analysis_id"])),
            "models": {stage: {"model": (latest["model_metadata"].get(stage) or {}).get("model"),
                               "prompt_version": (latest["model_metadata"].get(stage) or {}).get(
                                   "prompt_version")}
                       for stage in ("selector", "verifier")
                       if (latest["model_metadata"] or {}).get(stage)},
        }
    return {"lecture_id": str(lecture_id),
            "state": _clean(state) if state else None,
            "analysis": analysis,
            "recording": ({key: _plain((state or {}).get(key)) for key in (
                "recording_state", "recording_status", "recording_reason", "recording_rule",
                "recording_file_name", "recording_duration_seconds", "recording_checked_at")}
                if state else None),
            "moments": shown,
            "totals": {"moments": len(shown),
                       "completed_clips": sum(1 for m in shown if m["asset"]),
                       "ready_to_render": sum(1 for m in shown if m["job"] and
                                              m["job"]["status"] == "READY_TO_RENDER")}}
