"""
One lecture's facts across every track, the multipart policy, and readiness.

The single definition of "fully ready", read by the planner, the import gate,
the reconciler, the CLI and the operations API:

    a lecture is FULLY READY in a library when
      * its recording discovery is LOCATED (every Graph recording part has a
        verified SharePoint file - P0-C), and
      * every INCLUDED part has a live Bunny video whose encoding is VERIFIED, and
      * every included part's video has a VERIFIED current caption built from
        that part's own raw WebVTT, and
      * no part is still waiting for an operator's fragment decision, and
      * the lecture is not blocked as a duplicate (below).

DUPLICATE CALENDAR EVENTS (P0-F: decided here, never waited for from QA)
    The publisher applies Lecture Intelligence's validated duplicate rule
    (DuplicateResolutionService.classify_lecture, read-only) itself:
    no other live sibling / siblings cancelled or already suppressed -> proceed
    the WINNER of a confidently resolved group (case A)                 -> proceed
    the clearly-empty loser of such a group                            -> DUPLICATE_LOSER
    any group the rule leaves to a human (cases B, C, ...)             -> DUPLICATE_AMBIGUOUS
    A blocked lecture never reaches Bunny; its import job goes to review and
    the reconciler raises DUPLICATE_BLOCKED. Writing the suppression stays
    QA's/the operator's job - the publisher only reads the decision.

MULTIPART / FRAGMENT POLICY (never drops or adds a part on its own)
    operator decision present            -> that decision (INCLUDE / EXCLUDE)
    part not LOCATED                     -> NOT_LOCATED
    classification FRAGMENT, paired with
      a transcript part of the lecture   -> INCLUDE  (it carries the lecture's own speech:
                                                      identity and necessity both proven)
    classification FRAGMENT, unpaired    -> NEEDS_DECISION (REVIEW_REQUIRED; never auto-published)
    anything else (FULL / PARTIAL)       -> INCLUDE
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.db.repositories.bunny_publisher import BunnyCaptionRepository, BunnyVideoRepository
from app.db.repositories.lecture_duplicates import DuplicateEventRepository
from app.db.repositories.publisher_ops import PartDecisionRepository
from app.lectures.duplicate_service import DuplicateResolutionService
from app.lectures.duplicates import CASE_NOT_DUPLICATE, CASE_SUPPRESSED

INCLUDE, EXCLUDE, NEEDS_DECISION, NOT_LOCATED = "INCLUDE", "EXCLUDE", "NEEDS_DECISION", "NOT_LOCATED"
DUPLICATE_LOSER, DUPLICATE_AMBIGUOUS = "DUPLICATE_LOSER", "DUPLICATE_AMBIGUOUS"

LECTURE = """
SELECT l.lecture_id, l.subject, l.session_date, l.scheduled_start, l.scheduled_end, l.is_cancelled,
       l.metadata -> 'duplicate_suppression' IS NOT NULL
  FROM public.lecture_sessions l WHERE l.lecture_id = %s
"""
LMS = """
SELECT e.match_status,
       (SELECT k.lms_occurrence_id FROM public.bunny_lms_occurrence_links k
         WHERE k.evaluation_id = e.evaluation_id AND k.link_role = 'PRIMARY')
  FROM public.bunny_lms_match_evaluations e WHERE e.lecture_id = %s AND e.is_current
"""
DISCOVERY = """
SELECT status, reason_code, expected_part_count, located_part_count
  FROM public.bunny_recording_discoveries WHERE lecture_id = %s
"""
PARTS = """
SELECT recording_part_index, locate_status, classification, source_drive_id, source_item_id, source_filename,
       source_size_bytes, source_duration_seconds, graph_recording_id, call_id, content_correlation_id,
       recording_created_at, recording_end_at, media_origin, file_name_lead_seconds, transcript_artifact_id
  FROM public.bunny_recording_sources WHERE lecture_id = %s ORDER BY recording_part_index
"""
PART_FIELDS = ("part_index", "locate_status", "classification", "drive_id", "item_id", "file_name", "size_bytes",
               "duration_seconds", "graph_recording_id", "call_id", "content_correlation_id", "recording_created_at",
               "recording_end_at", "media_origin", "lead_seconds", "transcript_artifact_id")
TRANSCRIPT = """
SELECT selection_status FROM public.lecture_transcript_selections
 WHERE lecture_id = %s AND selection_version = %s
"""


@dataclass
class LectureFacts:
    lecture_id: str
    exists: bool = True
    subject: str | None = None
    session_date: object = None
    scheduled_start: object = None
    scheduled_end: object = None
    cancelled: bool = False
    suppressed: bool = False
    lms_status: str = "PENDING"
    lms_occurrence_id: str | None = None
    recording_status: str = "PENDING"
    recording_reason: str | None = None
    expected_part_count: int | None = None
    transcript_status: str = "PENDING"
    parts: list = field(default_factory=list)
    decisions: dict = field(default_factory=dict)
    duplicate_unresolved: bool = False          # True = blocked: never imported to Bunny
    duplicate_block: str | None = None          # DUPLICATE_LOSER / DUPLICATE_AMBIGUOUS
    duplicate_case: str | None = None
    duplicate_role: str | None = None

    def part(self, index: int) -> dict | None:
        return next((p for p in self.parts if p["part_index"] == index), None)

    def policy(self, part: dict) -> str:
        return part_policy(part, self.decisions.get(part["part_index"]))


def part_policy(part: dict, decision: str | None) -> str:
    if decision in (INCLUDE, EXCLUDE):
        return decision
    if part["locate_status"] != "LOCATED":
        return NOT_LOCATED
    if part.get("classification") == "FRAGMENT":
        return INCLUDE if part.get("transcript_artifact_id") else NEEDS_DECISION
    return INCLUDE


def load_facts(connection, lecture_id) -> LectureFacts:
    from app.transcripts.selection import SELECTION_VERSION
    lecture_id = str(lecture_id)
    row = connection.execute(LECTURE, (lecture_id,)).fetchone()
    if row is None:
        return LectureFacts(lecture_id, exists=False)
    facts = LectureFacts(lecture_id, subject=row[1], session_date=row[2], scheduled_start=row[3],
                         scheduled_end=row[4], cancelled=bool(row[5]), suppressed=bool(row[6]))
    lms = connection.execute(LMS, (lecture_id,)).fetchone()
    if lms:
        facts.lms_status, facts.lms_occurrence_id = lms[0], lms[1]
    discovery = connection.execute(DISCOVERY, (lecture_id,)).fetchone()
    if discovery:
        facts.recording_status, facts.recording_reason, facts.expected_part_count = discovery[0], discovery[1], \
            discovery[2]
    facts.parts = [dict(zip(PART_FIELDS, r)) for r in connection.execute(PARTS, (lecture_id,)).fetchall()]
    transcript = connection.execute(TRANSCRIPT, (lecture_id, SELECTION_VERSION)).fetchone()
    facts.transcript_status = transcript[0] if transcript else "PENDING"
    facts.decisions = PartDecisionRepository().decisions(connection, lecture_id)
    group = DuplicateEventRepository().load_group_for_lecture(connection, lecture_id)
    open_siblings = (not facts.suppressed) and any(
        member["lecture_id"] != lecture_id and not member["is_cancelled"] and not member["suppression"]
        for member in group)
    if open_siblings:
        facts.duplicate_block, facts.duplicate_case, facts.duplicate_role = duplicate_block(connection, lecture_id)
    facts.duplicate_unresolved = facts.duplicate_block is not None
    return facts


def duplicate_block(connection, lecture_id) -> tuple[str | None, str, str]:
    """(block or None, case, role) from the validated duplicate rule, read-only."""
    decision = DuplicateResolutionService().classify_lecture(connection, lecture_id)
    case, role = decision["case"], decision["role"]
    if case == CASE_NOT_DUPLICATE:
        return None, case, role
    if role == "WINNER" and case == CASE_SUPPRESSED and not decision["requires_manual_review"]:
        return None, case, role                 # the one canonical lecture of the group
    return (DUPLICATE_LOSER if role == "SUPPRESSIBLE_DUPLICATE" else DUPLICATE_AMBIGUOUS), case, role


@dataclass
class Readiness:
    lecture_id: str
    library_id: int | None
    fully_ready: bool
    state: str
    parts: list
    blocking: list

    def as_dict(self) -> dict:
        return {"lecture_id": self.lecture_id, "library_id": self.library_id, "fully_ready": self.fully_ready,
                "state": self.state, "parts": self.parts, "blocking": self.blocking}


def readiness(connection, facts: LectureFacts, library_id: int | None, language: str = "en") -> Readiness:
    """Where this lecture stands in ONE library, part by part."""
    videos, captions = BunnyVideoRepository(), BunnyCaptionRepository()
    parts, blocking = [], []
    if not facts.exists:
        return Readiness(facts.lecture_id, library_id, False, "UNKNOWN_LECTURE", [], ["LECTURE_NOT_FOUND"])
    if facts.cancelled or facts.suppressed:
        return Readiness(facts.lecture_id, library_id, False, "NOT_APPLICABLE", [],
                         ["CANCELLED" if facts.cancelled else "LI_DUPLICATE"])
    if facts.duplicate_block:
        blocking.append(facts.duplicate_block)
    if facts.lms_status != "VERIFIED":
        blocking.append(f"LMS_{facts.lms_status}")
    if facts.recording_status != "LOCATED":
        blocking.append(f"RECORDING_{facts.recording_status}")
    for part in facts.parts:
        policy = facts.policy(part)
        entry = {"part_index": part["part_index"], "locate_status": part["locate_status"],
                 "classification": part["classification"], "policy": policy,
                 "video": None, "encoding": None, "caption": None}
        if library_id is not None and policy == INCLUDE:
            video = videos.live_for_part(connection, library_id, facts.lecture_id, part["part_index"])
            if video:
                entry.update(video=video["import_status"], encoding=video["encoding_status"],
                             bunny_video_id=str(video["bunny_video_id"]) if video["bunny_video_id"] else None)
                if video["bunny_video_id"]:
                    current = captions.current(connection, library_id, video["bunny_video_id"], language)
                    entry["caption"] = current["publication_status"] if current else None
        if policy == NEEDS_DECISION:
            blocking.append(f"PART_{part['part_index']}_FRAGMENT_NEEDS_DECISION")
        elif policy == INCLUDE and (entry["encoding"] != "VERIFIED" or entry["caption"] != "VERIFIED"):
            blocking.append(f"PART_{part['part_index']}_NOT_PUBLISHED")
        parts.append(entry)
    if facts.recording_status == "LOCATED" and not any(p["policy"] == INCLUDE for p in parts):
        blocking.append("NO_INCLUDED_PART")
    ready = library_id is not None and not blocking
    state = "FULLY_READY" if ready else ("REVIEW_REQUIRED" if any(
        b.endswith("NEEDS_DECISION") or b in ("LMS_AMBIGUOUS", "LMS_CONFLICT", "LMS_STALE", "RECORDING_REVIEW_REQUIRED",
                                               DUPLICATE_LOSER, DUPLICATE_AMBIGUOUS)
        for b in blocking) else "IN_PROGRESS")
    return Readiness(facts.lecture_id, library_id, ready, state, parts, blocking)
