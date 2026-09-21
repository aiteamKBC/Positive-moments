"""
Phase 6C: queue the three lecture parts as media jobs.

Same queue, same worker, same contract as Positive Clips - `qa_media_jobs`
with `job_type = 'lecture_part'` and `part_number` 1..3. The worker has
accepted that job type since it was written; nothing new is introduced here.

The identities (`job_key`, `output_filename`) are the ones
`automation/lecture_parts/planner.py` already defines, so a replan reuses the
rows a previous run created instead of queueing a second set of parts.

WHAT IS DIFFERENT FROM CLIPS
----------------------------
A part is tens of minutes long, so the cost of getting a boundary wrong is
higher, not lower. Parts are cut `precise` (re-encode): a stream copy seeks to
the preceding keyframe, which would make each part start a little early and
repeat seconds the previous part already ended with.
"""
import json
from dataclasses import dataclass

from app.common.errors import DATABASE_ERROR, PlatformError
from app.media.coordinates import MEDIA_COORDINATE_VERSION
from app.media.lecture_parts import (
    LECTURE_PART_MEDIA_VERSION,
    PART_CUT_MODE,
    UNRESOLVED_PART_SOURCE,
    PartMediaPlan,
    PartPlanRefused,
    assert_producible,
    verify_coverage,
)
from automation.lecture_parts.planner import (
    SPLIT_VERSION,
    part_job_key,
    part_output_filename,
    split_plan_key,
)


EXISTING_JOB = """
SELECT job_id, status, metadata FROM public.qa_media_jobs WHERE job_key = %s
"""

INSERT_JOB = """
INSERT INTO public.qa_media_jobs (
    job_key, session_id, job_type, part_number, clip_key,
    start_seconds, end_seconds, cut_mode, output_filename,
    source_drive_id, source_item_id,
    destination_drive_id, destination_folder_item_id,
    status, attempt_count, not_before, metadata)
VALUES (%(job_key)s, %(session_id)s, 'lecture_part', %(part_number)s, NULL,
        %(start_seconds)s, %(end_seconds)s, %(cut_mode)s, %(output_filename)s,
        %(source_drive_id)s, %(source_item_id)s,
        %(destination_drive_id)s, %(destination_folder_item_id)s,
        'pending', 0, NOW(), %(metadata)s)
ON CONFLICT (job_key) DO NOTHING
RETURNING job_id
"""

REPLAN_JOB = """
UPDATE public.qa_media_jobs
   SET start_seconds = %(start_seconds)s,
       end_seconds   = %(end_seconds)s,
       cut_mode      = %(cut_mode)s,
       output_filename = %(output_filename)s,
       metadata      = metadata || %(metadata)s,
       updated_at    = NOW()
 WHERE job_key = %(job_key)s AND status IN ('pending', 'failed')
RETURNING job_id
"""


def resolve_part_sources(plan: PartMediaPlan, session: dict,
                         recording_sources: dict | None = None) -> dict:
    """
    Decide which SharePoint file each part is cut from.

    `recording_sources` maps a recording_id to its own
    {"source_drive_id", "source_item_id"} - migration 019's per-part location.

    A single-recording lecture may fall back to the session's location, which
    is what `qa_doctors_sessions` has always held and what the Femi pilot used:
    with one recording there is only one file it can mean.

    A MULTIPART lecture may not. The session carries one location for two
    files, so a fallback would hand some part the other recording's media - a
    cut that succeeds, matches its requested duration, passes coverage, and
    contains the wrong hour of teaching. This refuses instead.
    """
    sources = recording_sources or {}
    multipart = len({part.segment.recording_id for part in plan.parts}) > 1
    session_source = {
        "source_drive_id": session.get("recording_drive_id"),
        "source_item_id": session.get("recording_item_id"),
    }

    resolved, unresolved = {}, []
    for part in plan.parts:
        recording_id = part.segment.recording_id
        found = sources.get(recording_id)
        if not found and not multipart:
            found = session_source
        if not (found or {}).get("source_drive_id") \
                or not (found or {}).get("source_item_id"):
            unresolved.append({
                "part_number": part.part_number,
                "recording_id": recording_id,
                "recording_part_index": part.segment.part_index,
            })
            continue
        resolved[part.part_number] = {
            "source_drive_id": found["source_drive_id"],
            "source_item_id": found["source_item_id"],
        }

    if unresolved:
        raise PartPlanRefused(
            UNRESOLVED_PART_SOURCE,
            "no SharePoint recording is located for part(s) "
            + ", ".join(str(item["part_number"]) for item in unresolved)
            + (" (multipart: the session's single location cannot stand in "
               "for a second recording)" if multipart else ""),
            detail={"multipart": multipart, "unresolved": unresolved})

    # Distinct parts of one lecture may legitimately share a file - parts 1 and
    # 2 of Andrew both come from recording 1 - so sharing is not an error. What
    # would be an error is two DIFFERENT recordings resolving to one file.
    by_recording = {}
    for part in plan.parts:
        item = resolved[part.part_number]["source_item_id"]
        by_recording.setdefault(part.segment.recording_id, set()).add(item)
    collisions = {}
    for recording_id, items in by_recording.items():
        for other, other_items in by_recording.items():
            if other != recording_id and items & other_items:
                collisions[recording_id] = sorted(items)
    if collisions:
        raise PartPlanRefused(
            UNRESOLVED_PART_SOURCE,
            "two different recordings resolved to the same SharePoint file",
            detail={"collisions": collisions})
    return resolved


@dataclass(frozen=True)
class PartJobOutcome:
    session_id: str
    inserted: int
    replanned: int
    unchanged: int
    jobs: list[dict]
    coverage: dict


class LecturePartJobProducer:
    """Queues three parts, or refuses the lecture. Never queues one of three."""

    def __init__(self, *, destination: dict):
        if not destination.get("destination_drive_id") \
                or not destination.get("destination_folder_item_id"):
            raise ValueError("a SharePoint destination drive and folder are required")
        self.destination = destination

    def produce(self, connection, session: dict, plan: PartMediaPlan,
                lecture_start: float, lecture_end: float, *,
                recording_sources: dict | None = None,
                provenance: dict | None = None,
                dry_run: bool = True) -> PartJobOutcome:
        # All three or none. A lecture delivered as "parts 1 and 3" is worse
        # than one delivered as nothing, because it looks complete.
        assert_producible(plan)
        coverage = verify_coverage(plan, lecture_start, lecture_end)
        if not coverage["complete"]:
            raise PartPlanRefused(
                "INCOMPLETE_COVERAGE",
                "the three parts do not tile the lecture exactly once",
                detail=coverage)

        # Before anything is queued: every part must know its own file.
        sources = resolve_part_sources(plan, session, recording_sources)

        session_id = session["session_id"]
        inserted = replanned = unchanged = 0
        described = []

        for part in plan.parts:
            segment = part.segment
            key = part_job_key(session_id, part.part_number, SPLIT_VERSION)
            filename = part_output_filename(session, part.part_number)
            payload = {
                "job_key": key,
                "session_id": session_id,
                "part_number": part.part_number,
                "start_seconds": round(segment.media_start_seconds, 3),
                "end_seconds": round(segment.media_end_seconds, 3),
                "cut_mode": PART_CUT_MODE,
                "output_filename": filename,
                "source_drive_id": sources[part.part_number]["source_drive_id"],
                "source_item_id": sources[part.part_number]["source_item_id"],
                "destination_drive_id": self.destination["destination_drive_id"],
                "destination_folder_item_id":
                    self.destination["destination_folder_item_id"],
                "metadata": json.dumps({
                    "split_version": SPLIT_VERSION,
                    "split_plan_key": split_plan_key(session_id, SPLIT_VERSION),
                    "part_number": part.part_number,
                    "part_title": session.get(f"part_{part.part_number}_title"),
                    "lecture_date": str(session.get("date") or "")[:10],
                    "subject": session.get("subject"),
                    "trainer": session.get("trainer"),
                    "produced_by": "lecture-part-producer-v1",
                    "media_policy_version": LECTURE_PART_MEDIA_VERSION,
                    "media_coordinate_version": MEDIA_COORDINATE_VERSION,
                    "canonical_start_seconds": round(part.canonical_start_seconds, 3),
                    "canonical_end_seconds": round(part.canonical_end_seconds, 3),
                    "recording_id": segment.recording_id,
                    "recording_part_index": segment.part_index,
                    **(provenance or {}),
                }),
            }

            existing = connection.execute(EXISTING_JOB, (key,)).fetchone()
            state = "insert"
            if existing:
                status = existing[1]
                if status in ("processing", "completed"):
                    state = f"left_alone_{status}"
                else:
                    state = "replan"

            if not dry_run:
                if state == "insert" and connection.execute(INSERT_JOB, payload).fetchone():
                    inserted += 1
                elif state == "replan" and connection.execute(REPLAN_JOB, payload).fetchone():
                    replanned += 1
                elif state.startswith("left_alone"):
                    unchanged += 1
            else:
                if state == "insert":
                    inserted += 1
                elif state == "replan":
                    replanned += 1
                else:
                    unchanged += 1

            described.append({
                "state": state, "part_number": part.part_number, "job_key": key,
                "output_filename": filename, "cut_mode": PART_CUT_MODE,
                "canonical_start_seconds": round(part.canonical_start_seconds, 3),
                "canonical_end_seconds": round(part.canonical_end_seconds, 3),
                "media_start_seconds": round(segment.media_start_seconds, 3),
                "media_end_seconds": round(segment.media_end_seconds, 3),
                "duration_seconds": round(segment.duration_seconds, 3),
                "recording_id": segment.recording_id,
            })

        if not described:
            raise PlatformError(DATABASE_ERROR, "no parts were produced")
        return PartJobOutcome(session_id=session_id, inserted=inserted,
                              replanned=replanned, unchanged=unchanged,
                              jobs=described, coverage=coverage)
