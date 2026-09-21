"""
Phase 6B: turn planned Positive Clips into durable media jobs.

WHAT THIS REPLACES
------------------
`automation/positive_clips/sql/010+011` computed clip boundaries in SQL:
`start = original_start - 60`, `end = original_end + 60`. That produced the
right answer for a single-part lecture starting near zero and had no way to be
right for anything else, because SQL had no media timeline to consult. It also
had no upper bound - it could ask FFmpeg for seconds past the end of the file.

This producer keeps the SQL version's IDENTITIES exactly (`clip_key`,
`job_key`, `output_filename`), so a replan reuses the rows that already exist
rather than queueing duplicates, and takes its COORDINATES from the one
validated transform.

WHAT IT WRITES
--------------
`public.qa_media_jobs` only - the existing durable queue, the existing worker
contract. No new queue and no new table: the plan's provenance goes into the
job's `metadata`, which is where the previous producer already recorded
padding and analysis fields.

IDEMPOTENCY
-----------
`job_key` is UNIQUE. A second run over the same moments is a no-op unless the
planner version or the source fingerprint changed, in which case the pending
row is updated in place. A job that is already processing or completed is
never touched - the queue owns it at that point, not the planner.
"""
import hashlib
import json
import re
from dataclasses import dataclass

from app.common.errors import DATABASE_ERROR, PlatformError
from app.media.coordinates import MEDIA_COORDINATE_VERSION, MediaTimeline
from app.media.positive_clips import (
    POSITIVE_CLIP_PLANNER_VERSION,
    ClipPlan,
    ClipPolicy,
    PositiveMoment,
    plan_lecture,
)


PRODUCED_BY = "positive-clip-producer-v2"

ANALYSED = """
SELECT d.session_id, d.subject, d.date, d.trainer, d.positive_clips,
       d.recording_drive_id, d.recording_item_id,
       d.clips_analysis_completeness
  FROM public.qa_doctors_sessions d
 WHERE d.session_id = %s
"""

EXISTING_JOB = """
SELECT job_id, status, metadata
  FROM public.qa_media_jobs
 WHERE job_key = %s
"""

INSERT_JOB = """
INSERT INTO public.qa_media_jobs (
    job_key, session_id, job_type, part_number, clip_key,
    start_seconds, end_seconds, cut_mode, output_filename,
    source_drive_id, source_item_id,
    destination_drive_id, destination_folder_item_id,
    status, attempt_count, not_before, metadata)
VALUES (%(job_key)s, %(session_id)s, 'positive_clip', NULL, %(clip_key)s,
        %(start_seconds)s, %(end_seconds)s, %(cut_mode)s, %(output_filename)s,
        %(source_drive_id)s, %(source_item_id)s,
        %(destination_drive_id)s, %(destination_folder_item_id)s,
        'pending', 0, NOW(), %(metadata)s)
ON CONFLICT (job_key) DO NOTHING
RETURNING job_id
"""

# Only a job the queue is not currently responsible for may be re-planned.
REPLAN_JOB = """
UPDATE public.qa_media_jobs
   SET start_seconds = %(start_seconds)s,
       end_seconds   = %(end_seconds)s,
       cut_mode      = %(cut_mode)s,
       output_filename = %(output_filename)s,
       metadata      = metadata || %(metadata)s,
       updated_at    = NOW()
 WHERE job_key = %(job_key)s
   AND status IN ('pending', 'failed')
RETURNING job_id
"""

COMPLETED_ASSET = """
SELECT trim_status, trim_start_seconds, trim_end_seconds, clip_url
  FROM public.qa_positive_clip_assets
 WHERE clip_key = %s
"""

TIMESTAMP = re.compile(r"^\d{1,3}:[0-5]\d:[0-5]\d\.\d{1,3}$")


def seconds(stamp: str) -> float | None:
    if not isinstance(stamp, str) or not TIMESTAMP.match(stamp.strip()):
        return None
    hours, minutes, rest = stamp.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def safe_subject(subject: str | None, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", subject or "lecture").strip("-")
    return slug[:limit].strip("-") or "lecture"


def output_filename(session_id: str, subject: str | None, date,
                    clip_index: int, start_cue: int, end_cue: int) -> str:
    """The existing convention, character for character."""
    short = hashlib.md5(session_id.encode()).hexdigest()[:8]
    day = str(date or "")[:10].replace("-", "") or "00000000"
    return (f"{day}_{safe_subject(subject)}_{short}_clip-"
            f"{clip_index:02d}_cue-{start_cue}-{end_cue}.mp4")


def analysis_fields(clip: dict) -> dict:
    """
    The fields the DOWNSTREAM asset-sync contract reads out of job metadata.

    `020_complete_endpoint_asset_sync.sql` refuses any job whose metadata has no
    `original_start`/`original_end`, and reads speaker, category, quote, reason
    and confidence from the same place to build the delivered asset row. The
    pilot proved this the hard way: the first v2 job completed and uploaded, and
    then created no asset at all, because v2 had renamed those two keys.

    So the producer keeps the ORIGINAL key names verbatim and adds its new
    provenance alongside them, rather than replacing them.
    """
    return {
        "original_start": clip.get("start"),
        "original_end": clip.get("end"),
        "start_cue": clip.get("start_cue"),
        "end_cue": clip.get("end_cue"),
        "speaker": clip.get("speaker"),
        "positive_speakers": clip.get("positive_speakers"),
        "category": clip.get("category"),
        "positive_quote": clip.get("positive_quote"),
        "quote": clip.get("quote"),
        "reason": clip.get("reason"),
        "confidence": clip.get("confidence"),
        "semantic_verification": clip.get("semantic_verification"),
    }


def moments_from_analysis(clips) -> list[PositiveMoment]:
    """
    Read the stored analysis as evidence. Nothing is recomputed and no model is
    called; a moment whose timestamps are unreadable is skipped, never guessed.
    """
    moments = []
    for index, clip in enumerate(clips or [], start=1):
        if not isinstance(clip, dict):
            continue
        start = seconds(clip.get("start"))
        end = seconds(clip.get("end"))
        start_cue = clip.get("start_cue")
        end_cue = clip.get("end_cue")
        if start is None or end is None:
            continue
        if not (isinstance(start_cue, int) and isinstance(end_cue, int)):
            continue
        moments.append(PositiveMoment(
            clip_index=index, start_seconds=start, end_seconds=end,
            start_cue=start_cue, end_cue=end_cue,
            speaker=clip.get("speaker"), category=clip.get("category"),
            quote=clip.get("positive_quote") or clip.get("quote")))
    return moments


def source_fingerprint(session_id: str, clips, timeline: MediaTimeline) -> str:
    """
    What the plan was derived from.

    Changing the analysis, or re-measuring the recording to a different
    duration, changes this - and only then is an existing pending job replanned.
    """
    payload = json.dumps({
        "session_id": session_id,
        "moments": [
            {"start": clip.get("start"), "end": clip.get("end"),
             "start_cue": clip.get("start_cue"), "end_cue": clip.get("end_cue")}
            for clip in (clips or []) if isinstance(clip, dict)],
        "timeline": timeline.describe(),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ProducerOutcome:
    session_id: str
    planned: int
    inserted: int
    replanned: int
    unchanged: int
    already_delivered: int
    refused: list[dict]
    jobs: list[dict]


class PositiveClipJobProducer:
    """Plans a lecture's clips and queues them. Idempotent by construction."""

    def __init__(self, *, destination: dict, policy: ClipPolicy | None = None):
        if not destination.get("destination_drive_id") \
                or not destination.get("destination_folder_item_id"):
            raise ValueError("a SharePoint destination drive and folder are required")
        self.destination = destination
        self.policy = policy or ClipPolicy()

    def produce(self, connection, session_id: str, timeline: MediaTimeline,
                *, dry_run: bool = True) -> ProducerOutcome:
        row = connection.execute(ANALYSED, (session_id,)).fetchone()
        if not row:
            raise PlatformError(DATABASE_ERROR, f"no analysed session {session_id!r}")
        (_id, subject, date, trainer, clips, drive_id, item_id,
         completeness) = row

        moments = moments_from_analysis(clips)
        plans, refused = plan_lecture(timeline, session_id, moments, self.policy)
        # clip_index is the 1-based ordinal of the stored analysis array, so it
        # indexes straight back into it.
        by_index = {index: clip for index, clip in enumerate(clips or [], start=1)
                    if isinstance(clip, dict)}
        fingerprint = source_fingerprint(session_id, clips, timeline)

        inserted = replanned = unchanged = delivered = 0
        described = []
        for plan in plans:
            asset = connection.execute(COMPLETED_ASSET, (plan.clip_key,)).fetchone()
            if asset and asset[0] == "completed":
                # Already delivered. Re-queueing would produce a second copy of
                # a clip somebody already has.
                delivered += 1
                described.append(self._describe(plan, timeline, subject, date,
                                                trainer, fingerprint,
                                                state="already_delivered",
                                                delivered_start=asset[1]))
                continue

            analysis = analysis_fields(by_index.get(plan.clip_index, {}))
            payload = self._payload(plan, timeline, session_id, subject, date,
                                    trainer, drive_id, item_id, fingerprint,
                                    analysis)
            existing = connection.execute(EXISTING_JOB, (plan.job_key,)).fetchone()
            state = "insert"
            if existing:
                status, metadata = existing[1], existing[2] or {}
                same = (metadata.get("source_fingerprint") == fingerprint
                        and metadata.get("planner_version")
                        == POSITIVE_CLIP_PLANNER_VERSION)
                if same:
                    unchanged += 1
                    state = "unchanged"
                elif status in ("processing", "completed"):
                    # The queue owns it now. Rewriting its boundaries mid-flight
                    # would change what a running worker is producing.
                    unchanged += 1
                    state = f"left_alone_{status}"
                else:
                    state = "replan"

            if not dry_run:
                if state == "insert":
                    if connection.execute(INSERT_JOB, payload).fetchone():
                        inserted += 1
                elif state == "replan":
                    if connection.execute(REPLAN_JOB, payload).fetchone():
                        replanned += 1
            elif state == "insert":
                inserted += 1
            elif state == "replan":
                replanned += 1

            described.append(self._describe(plan, timeline, subject, date,
                                            trainer, fingerprint, state=state))

        return ProducerOutcome(
            session_id=session_id, planned=len(plans), inserted=inserted,
            replanned=replanned, unchanged=unchanged,
            already_delivered=delivered, refused=refused, jobs=described)

    # -- shaping ---------------------------------------------------------------

    def _metadata(self, plan: ClipPlan, timeline, subject, date, trainer,
                  fingerprint, analysis: dict) -> dict:
        return {
            "lecture_date": str(date or "")[:10],
            "subject": subject,
            "trainer": trainer,
            "clip_index": plan.clip_index,
            # The existing downstream contract, unchanged. See analysis_fields.
            **analysis,
            "padding_before_seconds": round(plan.applied_padding_before_seconds, 3),
            "padding_after_seconds": round(plan.applied_padding_after_seconds, 3),
            "produced_by": PRODUCED_BY,
            "source_fingerprint": fingerprint,
            # ...and the new coordinate provenance beside it.
            **plan.provenance(timeline),
        }

    def _payload(self, plan, timeline, session_id, subject, date, trainer,
                 drive_id, item_id, fingerprint, analysis) -> dict:
        start_cue, end_cue = plan.clip_key.rsplit(":", 2)[-2:]
        return {
            "job_key": plan.job_key,
            "session_id": session_id,
            "clip_key": plan.clip_key,
            "start_seconds": round(plan.media_start_seconds, 3),
            "end_seconds": round(plan.media_end_seconds, 3),
            "cut_mode": plan.cut_mode,
            "output_filename": output_filename(session_id, subject, date,
                                               plan.clip_index,
                                               int(start_cue), int(end_cue)),
            "source_drive_id": drive_id,
            "source_item_id": item_id,
            "destination_drive_id": self.destination["destination_drive_id"],
            "destination_folder_item_id":
                self.destination["destination_folder_item_id"],
            "metadata": json.dumps(self._metadata(plan, timeline, subject, date,
                                                  trainer, fingerprint, analysis)),
        }

    def _describe(self, plan, timeline, subject, date, trainer, fingerprint,
                  *, state, delivered_start=None) -> dict:
        start_cue, end_cue = plan.clip_key.rsplit(":", 2)[-2:]
        described = {
            "state": state,
            "clip_index": plan.clip_index,
            "clip_key": plan.clip_key,
            "job_key": plan.job_key,
            "moment_start_seconds": round(plan.moment_start_seconds, 3),
            "media_start_seconds": round(plan.media_start_seconds, 3),
            "media_end_seconds": round(plan.media_end_seconds, 3),
            "duration_seconds": round(plan.duration_seconds, 3),
            "cut_mode": plan.cut_mode,
            "recording_id": plan.recording_id,
            "output_filename": output_filename(
                plan.clip_key.split(":", 1)[0], subject, date,
                plan.clip_index, int(start_cue), int(end_cue)),
            "planner_version": POSITIVE_CLIP_PLANNER_VERSION,
            "media_coordinate_version": MEDIA_COORDINATE_VERSION,
        }
        if delivered_start is not None:
            described["delivered_trim_start_seconds"] = float(delivered_start)
        return described
