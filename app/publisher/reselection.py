"""
Operator re-selection of a lecture's recording under the longest-recording rule (2026-10-28).

A lecture located before recording_mapping.select_recording existed keeps the parts it was located
with: several Teams recordings of one occurrence (a 3-minute false start next to the 2-hour lecture)
may all have been included, and imported. These two actions apply the rule to such a lecture on
request, through the recording worker itself (same identity rules, same lock, same persistence):

    preview   RecordingWorker.run(rediscover=True, dry_run=True) on a READ ONLY connection, plus the
              lecture's persisted Bunny state -> what re-selection would include, keep, import and
              stop delivering. Graph is read; nothing is written anywhere.
    apply     the same evaluation, persisted (bunny_recording_discoveries / _sources only) and audited
              (OPERATOR_RECORDING_RESELECTED). The worker keeps the stored verified mapping unless the
              fresh one is LOCATED. Nothing here calls Bunny: the selected part is imported by the
              daemon's normal IMPORT_VIDEO track (an existing verified video is reused, never re-uploaded),
              and a de-selected part's video is never deleted - it stops being delivered, and once an
              operator deletes it in Bunny the FULL reconciliation retires its row (audited).

Responses carry identities, statuses, part indexes and durations only - never a URL or a file name.
"""
from __future__ import annotations

from app.db.repositories.bunny_publisher import BunnyVideoRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher.handlers import part_subject_key
from app.publisher.operations import OperatorActionRefused
from app.publisher.progress import INCLUDE, load_facts
from app.publisher.recordings import LOCATED

INCONCLUSIVE = "RESELECTION_INCONCLUSIVE_STORED_KEPT"
BUNNY_JOBS = ("IMPORT_VIDEO", "POLL_ENCODING", "PUBLISH_CAPTION")
LECTURES_OF_DAY = """
SELECT lecture_id::text FROM public.lecture_sessions
 WHERE session_date = %s AND NOT is_cancelled AND metadata -> 'duplicate_suppression' IS NULL
 ORDER BY scheduled_start, lecture_id
"""

# Plan actions.
NO_CHANGE = "NO_CHANGE"                        # the selected part is already the only one delivered, video present
RESELECT_KEEP_VIDEO = "RESELECT_KEEP_VIDEO"    # inclusion changes; the selected part's video already exists
RESELECT_AND_IMPORT = "RESELECT_AND_IMPORT"    # inclusion changes; the selected part still has to be imported
IMPORT_PENDING = "IMPORT_PENDING"              # inclusion unchanged; the selected part is not imported yet
BLOCKED = "BLOCKED"


def _video(row) -> dict | None:
    if row is None:
        return None
    return {"bunny_video_id": str(row["bunny_video_id"]) if row["bunny_video_id"] else None,
            "import_status": row["import_status"], "encoding_status": row["encoding_status"],
            "visibility": row["visibility_state"]}


def _in_bunny(bunny, video_id):
    """Bunny's own answer for one video id (Get Video, read-only): True, False on 404, None when unknown."""
    if bunny is None or not video_id:
        return None
    from app.bunny.client import BunnyError
    try:
        bunny.get_video(video_id)
        return True
    except BunnyError as error:
        return False if error.status == 404 else None


def plan_lecture(connect, worker, lecture_id, *, library_id, bunny=None) -> dict:
    """
    What re-selecting ONE lecture would do. Reads only: Graph, a READ ONLY database connection and, when a
    Bunny client is given, Get Video for each video involved (`bunny_present`).
    """
    lecture_id = str(lecture_id)
    outcome = worker.run(lecture_id, rediscover=True, dry_run=True)
    with connect() as connection:
        connection.read_only = True
        facts = load_facts(connection, lecture_id)
        videos = {v["recording_part_index"]: v for v in BunnyVideoRepository().for_lecture(
            connection, lecture_id, library_id) if v["retired_at"] is None}
        selected = (outcome.detail.get("recording_selection") or {}).get("part_index")
        jobs = {kind: PublisherQueueRepository().latest(connection, kind, part_subject_key(lecture_id, selected))
                for kind in BUNNY_JOBS} if selected is not None else {}
    plan = {"lecture_id": lecture_id, "subject": facts.subject, "session_date": facts.session_date,
            "scheduled_start": facts.scheduled_start, "scheduled_end": facts.scheduled_end,
            "lms_status": facts.lms_status, "recording_status": facts.recording_status,
            "evaluation": {"kind": outcome.kind, "reason": outcome.reason, "review_reason": outcome.review_reason},
            "current": [{"part_index": p["part_index"], "locate_status": p["locate_status"],
                         "classification": p["classification"],
                         "duration_seconds": round(float(p["duration_seconds"])) if p["duration_seconds"] else None,
                         "policy": facts.policy(p), "operator_decision": facts.decisions.get(p["part_index"]),
                         "video": _video(videos.get(p["part_index"]))} for p in facts.parts],
            "selection": None, "action": BLOCKED, "blocking": [], "stop_delivering": []}
    if not facts.exists:
        plan["blocking"].append("LECTURE_NOT_FOUND")
        return plan
    selection = outcome.detail.get("recording_selection")
    if outcome.kind != LOCATED or outcome.reason == INCONCLUSIVE or not selection:
        plan["blocking"].append(outcome.detail.get("reselection") or outcome.reason)
        return plan
    selected = selection["part_index"]
    plan["selection"] = selection
    included = sorted(p["part_index"] for p in facts.parts if facts.policy(p) == INCLUDE)
    decided = {i: d for i, d in facts.decisions.items() if i != selected and d == INCLUDE}
    if facts.decisions.get(selected) == "EXCLUDE" or decided:
        plan["blocking"].append("OPERATOR_PART_DECISION_OVERRIDES_SELECTION")
    if facts.duplicate_block:
        plan["blocking"].append(facts.duplicate_block)
    video = videos.get(selected)
    stored = facts.part(selected)
    if video is not None and stored is not None and (video["recording_drive_id"], video["recording_item_id"]) != (
            stored["drive_id"], stored["item_id"]):
        plan["blocking"].append("SELECTED_PART_VIDEO_HAS_ANOTHER_SOURCE")
    plan["stop_delivering"] = [{"part_index": i, **(_video(v) or {}),
                                "bunny_present": _in_bunny(bunny, v["bunny_video_id"] and str(v["bunny_video_id"]))}
                               for i, v in sorted(videos.items()) if i != selected and facts.decisions.get(i) != INCLUDE]
    if video is not None and video["bunny_video_id"]:
        plan["selected_video"] = {**_video(video), "bunny_present": _in_bunny(bunny, str(video["bunny_video_id"]))}
        if plan["selected_video"]["bunny_present"] is False:
            plan["blocking"].append("SELECTED_PART_VIDEO_MISSING_IN_BUNNY")
    if plan["blocking"]:
        return plan
    # The planner never restarts a track whose last job waits for an operator, nor a FAILED / REVIEW video row:
    # name the exact job to retry instead of letting the import silently not happen.
    stalled = [{"job_id": str(j["job_id"]), "job_kind": kind, "status": j["status"],
                "last_error_code": j["last_error_code"]}
               for kind, j in jobs.items() if j is not None and j["status"] in ("REVIEW_REQUIRED", "FAILED")]
    if stalled:
        plan["operator_retry_needed"] = stalled
    imported = video is not None and video["import_status"] not in ("FAILED", "REVIEW_REQUIRED")
    if video is not None and not imported:
        plan["import_waits_for"] = "OPERATOR_RETRY"          # its safe retry path, unchanged
    if included != [selected]:
        plan["action"] = RESELECT_KEEP_VIDEO if imported else RESELECT_AND_IMPORT
    else:
        plan["action"] = NO_CHANGE if imported else IMPORT_PENDING
    if not imported and facts.lms_status != "VERIFIED" and "import_waits_for" not in plan:
        plan["import_waits_for"] = f"LMS_NOT_VERIFIED:{facts.lms_status}"   # the import gate, unchanged
    return plan


def preview_day(connect, worker, day, *, library_id, bunny=None) -> dict:
    """plan_lecture for every lecture of one Cairo business date (lecture_sessions.session_date)."""
    with connect() as connection:
        connection.read_only = True
        ids = [r[0] for r in connection.execute(LECTURES_OF_DAY, (day,)).fetchall()]
    plans = [plan_lecture(connect, worker, lid, library_id=library_id, bunny=bunny) for lid in ids]
    counts: dict = {}
    for plan in plans:
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    return {"session_date": str(day), "library_id": library_id, "lectures": len(plans), "actions": counts,
            "plans": plans}


def apply(connect, worker, lecture_id, *, actor: str, reason: str, library_id, planner=None) -> dict:
    """
    Persist the re-selection of ONE lecture (recording discovery only), audit it, and - with a planner -
    queue the selected part's next Bunny job at once: the planner's tick only plans lectures inside its
    lookback window, and a re-selected lecture is usually older than that. Same planning rules as the tick
    (a live video row, a job waiting for review and a live job are all respected), so nothing is uploaded
    twice; the job itself still passes the import gate.
    """
    actor, reason = str(actor or "").strip()[:120], str(reason or "").strip()
    if not actor or not reason:
        raise OperatorActionRefused("ACTOR_AND_REASON_REQUIRED", "a re-selection needs an actor and a reason")
    before = plan_lecture(connect, worker, lecture_id, library_id=library_id)
    if before["action"] == BLOCKED:
        raise OperatorActionRefused("RESELECTION_BLOCKED", ", ".join(map(str, before["blocking"])) or "blocked")
    if before["action"] == NO_CHANGE:
        return {"lecture_id": before["lecture_id"], "applied": False, "plan": before}
    outcome = worker.run(lecture_id, rediscover=True)
    if outcome.kind != LOCATED or outcome.reason == INCONCLUSIVE:
        raise OperatorActionRefused("RESELECTION_NOT_APPLIED", outcome.detail.get("reselection") or outcome.reason)
    selection = outcome.detail.get("recording_selection") or {}
    with connect() as connection:
        connection.autocommit = True
        with connection.transaction():
            PublisherQueueRepository().event(
                connection, "OPERATOR_RECORDING_RESELECTED", lecture_id=before["lecture_id"], actor=actor,
                detail={"reason": reason[:300], "rule": selection.get("rule"), "version": selection.get("version"),
                        "selected_part": selection.get("part_index"), "candidates": selection.get("candidates"),
                        "previously_included": [p["part_index"] for p in before["current"] if p["policy"] == INCLUDE],
                        "stop_delivering": [p["part_index"] for p in before["stop_delivering"]],
                        "planned_action": before["action"]})
    queued = planner.plan_lecture(before["lecture_id"]) if planner is not None else None
    return {"lecture_id": before["lecture_id"], "applied": True, "outcome": outcome.summary(),
            "selected_part": selection.get("part_index"), "planned_action": before["action"],
            "stop_delivering": before["stop_delivering"], "queued": queued,
            "operator_retry_needed": before.get("operator_retry_needed")}
