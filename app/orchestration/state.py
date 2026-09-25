"""
Phase 4A Part A: the canonical pipeline state resolver.

ONE place answers "where is this lecture?". Before this, the answer was spread
across fifteen tables and every caller re-derived it slightly differently -
which is precisely how a scheduler ends up re-running work that is already
done, or skipping work that only looks done.

THE RULE THAT MATTERS
---------------------
A stage is never COMPLETE merely because a downstream row exists. Existence
answers "did this ever happen?"; COMPLETE has to answer "is what exists still
the right answer for the inputs we hold now?". Those diverge constantly in a
versioned platform:

  * a canonical document parsed under `webvtt_canonical_v1` is a real
    document, but it is not the current answer for a lecture whose evaluation
    was rebuilt under `webvtt_canonical_v2_seam_dedup`;
  * an engagement row computed from a superseded attendance snapshot is a real
    engagement row, and it is wrong;
  * a rendered session belonging to a superseded evaluation is a real render
    of yesterday's checklist.

So every stage here is judged on version and lineage, not on row count. A
stage whose row exists but whose lineage has moved is STALE, which is a
different instruction from MISSING: STALE means redo this from evidence we
already hold, MISSING means we never had it.

READ-ONLY
---------
Nothing in this module writes, calls Graph, calls a provider, or reads
`public.kbc_attendance`. The optional attendance probe is the single exception
and it is injected, never constructed here - see `attendance_probe`.
"""
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timezone as _timezone

from app.attendance.coverage import is_authoritative
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.common.errors import DATABASE_ERROR, PlatformError
from app.db.repositories.transcript_selections import TranscriptSelectionRepository
from app.lectures.duplicate_service import DuplicateResolutionService
from app.lectures.duplicates import (
    CASE_NOT_DUPLICATE,
    CASE_SUPPRESSED,
    DUPLICATE_RESOLUTION_VERSION,
    PENDING_REASON,
    SUPPRESSED_REASON,
)
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    OPERATOR_ONLY_ACTIONS,
    IDLE_ACTIONS,
    ATTENDANCE,
    BUILD_CANONICAL_CUES,
    CALCULATE_ENGAGEMENT,
    CANONICAL_CUES,
    COMPLETE,
    DISCOVERY,
    ENGAGEMENT,
    EVALUATE_PERFECT,
    EXCEL_SYNC,
    FAILED,
    LEGACY_QA_SYNC,
    LINK_RECORDING,
    MANUAL_REVIEW_REQUIRED,
    MEETING,
    MISSING,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    OBSERVED_ONLY_STAGES,
    ORCHESTRATION_VERSION,
    PERFECT_ELIGIBILITY,
    PERFECT_SYNC,
    QA_EVALUATION,
    QA_RENDER,
    RECORDING_LINK,
    RECOVER_ATTENDANCE,
    REFRESH_DETERMINISTIC_QA,
    RENDER_QA,
    RESOLVE_ATTENDANCE,
    RESOLVE_MEETING,
    RESOLVE_SPEAKERS,
    REVALIDATE_EVIDENCE,
    REVIEW_REQUIRED,
    RUN_DISCOVERY,
    RUN_QA,
    SELECT_TRANSCRIPT,
    SELECTION,
    SETTLED_STATES,
    SPEAKERS,
    STAGE_ORDER,
    STALE,
    SUPPRESS_DUPLICATE_EVENT,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
    TRANSCRIPT,
    WAIT_FOR_ATTENDANCE_SOURCE,
    WAIT_FOR_EXCEL_SYNC,
    WAIT_FOR_RECORDING,
    WAITING,
)
from app.qa.delivery import TRANSCRIPT_COVERAGE_INCOMPLETE, classify_delivery
from app.qa.deterministic import NON_DELIVERED
from app.qa.evidence_policy import DEFAULT_EVIDENCE_POLICY
from app.qa.perfect import DEFAULT_PERFECT_ELIGIBILITY_VERSION, PENDING_ATTENDANCE_DATA
from app.qa.recovery import recovery_state
from app.qa.inputs import attendance_flag
from app.qa.structured_output import DEFAULT_PROVIDER_CONTRACT
from app.qa.provider import PROVIDER_CONFIGURATION_ERROR
from app.recordings.models import NO_RECORDING_EXPECTED_CANCELLED
from app.rendering.evidence import RENDERER_VERSION
from app.transcripts.seam import SEAM_PARSER_VERSION
from app.transcripts.identity import canonical_key
from app.transcripts.selection import (
    SELECTED,
    SELECTION_VERSION,
    combined_source_fingerprint,
    relevant_candidates,
    select_transcript_parts,
)
from app.transcripts.speakers import SPEAKER_INVENTORY_VERSION
from app.transcripts.webvtt import PARSER_VERSION
from app.writer.legacy_identity import (
    AMBIGUOUS,
    FOREIGN_SAME_OCCURRENCE,
    OWNED_SAME_OCCURRENCE,
)
from app.writer.mapping import WRITER_VERSION
from app.writer.perfect_mapping import PERFECT_WRITER_VERSION


# Both canonical parser versions are legitimate current answers: Phase 3C2.3B
# rebuilt one lecture under seam dedup without invalidating the rest. Which one
# a given lecture should hold is decided by its own evaluation, not by a
# platform-wide preference.
ACCEPTED_PARSER_VERSIONS = (PARSER_VERSION, SEAM_PARSER_VERSION)

# A QA status that is a real, finished answer rather than work in progress.
TERMINAL_QA_STATUSES = ("COMPLETED", "NON_DELIVERED")

# Both are successful renders. `RENDERED_NON_DELIVERED` is the legacy cancelled
# output - eleven Not Met rows and a fixed evidence string - and treating it as
# a failure would make every cancelled lecture look broken forever.
RENDERED_STATUSES = ("RENDERED", "RENDERED_NON_DELIVERED")

# A legacy row this platform created and still owns. 'UPDATED' is a row that
# was written and later repaired - Phase 3C2.3F's `attended_count` backfill is
# exactly that - and treating it as anything less than written would report a
# corrected row as missing. The other statuses are real absences: NOOP wrote
# nothing, ROLLED_BACK undid itself, and a failed verification is not a row we
# may rely on.
OWNED_WRITE_STATUSES = ("WRITTEN", "UPDATED")

# `RESOLVED` is the current name; `EXACT_JOIN_URL_MATCH` is the pre-migration-002
# spelling of the same fact and is retained so historical rows keep their
# meaning rather than silently becoming unresolved.
RESOLVED_MAPPING_STATUSES = ("RESOLVED", "EXACT_JOIN_URL_MATCH")
AMBIGUOUS_MAPPING_STATUSES = ("AMBIGUOUS_ONLINE_MEETING", "FORBIDDEN_FOR_ORGANIZER",
                              "ORGANIZER_NOT_RESOLVABLE")

CONTENT_STORED_STATUSES = ("CONTENT_STORED", "CONTENT_STORED_NO_SPEAKER_ATTRIBUTION")
# The source has answered and the answer is "there is no transcript". Retrying
# is not free (it is a Graph call) and it will not change until the provider
# changes, so these are reported rather than hammered.
TRANSCRIPT_PENDING_STATUSES = ("TRANSCRIPT_NOT_READY", "DISCOVERED")
TRANSCRIPT_FAILED_STATUSES = ("BLOCKED_TRANSCRIPT_ACCESS", "CONTENT_FETCH_ERROR")


LECTURE = """
SELECT l.lecture_id, l.subject, l.normalized_subject, l.module, l.session_date,
       l.scheduled_start, l.is_cancelled, l.discovery_status,
       l.calendar_mapping_status, l.group_match_status, l.meeting_id,
       l.downstream_ready, l.metadata -> 'duplicate_suppression'
  FROM public.lecture_sessions l
 WHERE l.lecture_id = %s
"""

LECTURES_FOR_DAY = """
SELECT l.lecture_id
  FROM public.lecture_sessions l
 WHERE l.session_date = %s
 ORDER BY l.scheduled_start, l.lecture_id
"""

# Artifacts are provider-scoped since migration 006: one Graph transcript can
# legitimately belong to several lectures, so the lecture link lives in
# lecture_transcript_candidates and the join goes through it.
ARTIFACTS = """
SELECT count(*)::int AS total,
       count(*) FILTER (WHERE a.artifact_status = ANY(%s))::int AS stored,
       count(*) FILTER (WHERE a.artifact_status = ANY(%s))::int AS failed,
       max(a.content_fetched_at) AS newest_content_at
  FROM public.lecture_transcript_candidates c
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = c.artifact_id
 WHERE c.lecture_id = %s
"""

# The schedule the selector judged this occurrence against. Separate from
# LECTURE so the selection-relevance question reads exactly the fields the
# selector itself is given.
OCCURRENCE = """
SELECT l.scheduled_start, l.scheduled_end, l.session_date, l.meeting_id
  FROM public.lecture_sessions l
 WHERE l.lecture_id = %s
"""

SELECTION_ROW = """
SELECT s.selection_id, s.selection_status, s.selected_part_count,
       s.combined_content_sha256, s.updated_at,
       s.metadata ->> 'selection_input_fingerprint',
       s.actual_start, s.actual_end,
       cb.source_fingerprint, cb.duration_minutes, cb.duration_seconds
  FROM public.lecture_transcript_selections s
  LEFT JOIN public.lecture_combined_transcripts cb ON cb.selection_id = s.selection_id
 WHERE s.lecture_id = %s AND s.selection_version = %s
"""

SELECTED_PART_IDS = """
SELECT p.provider_transcript_id
  FROM public.lecture_transcript_selection_parts p
 WHERE p.selection_id = %s
 ORDER BY p.part_index
"""

DOCUMENTS = """
SELECT d.document_id, d.parser_version, d.parse_status, d.cue_count,
       d.selection_id, d.updated_at, d.source_content_sha256
  FROM public.lecture_transcript_documents d
 WHERE d.lecture_id = %s AND d.parser_version = ANY(%s)
 ORDER BY d.updated_at DESC, d.document_id DESC
"""

ENGAGEMENT_LINEAGE = """
SELECT m.engagement_id, m.document_id, m.attendance_snapshot_id,
       m.calculation_status, m.attended_count, m.spoke_count,
       m.item7_override_applied, m.engagement_score,
       m.metadata ->> 'engagement_status_detail',
       coalesce((m.metadata ->> 'attendance_zero_confirmed')::boolean, false),
       m.updated_at
  FROM public.lecture_engagement_metrics m
 WHERE m.lecture_id = %s
 ORDER BY m.updated_at DESC, m.engagement_id DESC
"""

SPEAKER_COUNT = """
SELECT count(*)::int
  FROM public.lecture_transcript_speakers sp
 WHERE sp.document_id = %s AND sp.speaker_inventory_version = %s
"""

LEGACY_WRITES = """
SELECT w.write_id, w.rendered_session_id, w.evaluation_id, w.write_status,
       w.writer_version, w.legacy_session_id, w.written_at
  FROM public.lecture_qa_legacy_writes w
 WHERE w.lecture_id = %s AND w.writer_version = %s
 ORDER BY w.written_at DESC, w.write_id DESC
"""

PERFECT_WRITES = """
SELECT p.write_status, p.writer_version, p.eligibility_version,
       p.legacy_lecture_key, p.result_id
  FROM public.lecture_perfect_lecture_legacy_writes p
 WHERE p.lecture_id = %s AND p.writer_version = %s
 ORDER BY p.written_at DESC
"""

# Phase 4B asked "did a SIBLING calendar event resolve?" with its own query
# here. Phase 4C1 needs the same question answered to a higher standard - the
# whole group, the organizer check, and which cases may be decided without a
# human - so the question now belongs to app/lectures/duplicates.py and this
# module asks that rule instead of keeping a second, looser copy of it.

PERFECT_RESULT_KEY = """
SELECT r.legacy_lecture_key, r.legacy_session_id, r.is_perfect
  FROM public.lecture_perfect_lecture_results r
 WHERE r.lecture_id = %s AND r.eligibility_version = %s
"""


def _is_coverage_review(evaluation) -> bool:
    return bool(evaluation and evaluation.get("qa_status") == "REVIEW_REQUIRED"
                and evaluation.get("review_reason") == TRANSCRIPT_COVERAGE_INCOMPLETE)


def _fetch(connection, sql, params):
    try:
        return connection.execute(sql, params).fetchall()
    except Exception as exc:
        raise PlatformError(DATABASE_ERROR, "pipeline state read failed") from exc


def _stage(state, *, action=None, **detail) -> dict:
    return {"state": state, "action": action, **detail}


class PipelineStateResolver:
    """
    Resolve the canonical stage matrix for ONE lecture, from persisted state.

    `attendance_probe`, when supplied, is asked a single yes/no question:
    "does the external source hold present rows for this lecture today?". It is
    what turns a WAITING attendance stage into RECOVER_ATTENDANCE. It is
    injected rather than built here so the resolver stays free of any
    dependency on `public.kbc_attendance`, and so the default behaviour of
    reading nothing external is the behaviour you get by not asking for
    anything else.
    """

    def __init__(self, *, selection_version: str = SELECTION_VERSION,
                 parser_versions=ACCEPTED_PARSER_VERSIONS,
                 speaker_inventory_version: str = SPEAKER_INVENTORY_VERSION,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION,
                 renderer_version: str = RENDERER_VERSION,
                 writer_version: str = WRITER_VERSION,
                 perfect_writer_version: str = PERFECT_WRITER_VERSION,
                 perfect_eligibility_version: str = DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                 evidence_policy_version: str = DEFAULT_EVIDENCE_POLICY,
                 legacy_observations=None,
                 attendance_probe=None,
                 duplicate_service=None,
                 selection_repository=None,
                 recording_link_mode: str = "observe",
                 recording_links=None,
                 clock=None):
        self.selection_version = selection_version
        self.parser_versions = list(parser_versions)
        self.speaker_inventory_version = speaker_inventory_version
        self.attendance_resolution_version = attendance_resolution_version
        self.renderer_version = renderer_version
        self.writer_version = writer_version
        self.perfect_writer_version = perfect_writer_version
        self.perfect_eligibility_version = perfect_eligibility_version
        self.evidence_policy_version = evidence_policy_version
        self.legacy_observations = legacy_observations
        self.attendance_probe = attendance_probe
        # Read-only: it is asked to CLASSIFY, never to resolve. Persisting a
        # suppression is the runner's job, through the same action dispatch as
        # every other stage.
        self.duplicate_service = duplicate_service or DuplicateResolutionService()
        # The SAME candidate loader the selector uses, collapse of re-serialized
        # transcript ids included. Freshness is judged on the evidence the
        # selector would actually see, never on a second reading of it.
        self.selection_repository = selection_repository or TranscriptSelectionRepository()
        # RECORDING_LINK. "observe" never reads lecture_recording_links.
        self.recording_link_mode = recording_link_mode
        # Until the stage is armed it is observed exactly as in Phase 4A: its
        # wait is reported, and never becomes the lecture's executable action.
        self.observed_only_stages = (
            OBSERVED_ONLY_STAGES if recording_link_mode == "write"
            else OBSERVED_ONLY_STAGES | {RECORDING_LINK})
        self.recording_links = recording_links
        self.clock = clock

    # -- entry points -------------------------------------------------------

    def lecture_ids_for_day(self, connection, session_date: _date) -> list[str]:
        return [str(row[0]) for row in
                _fetch(connection, LECTURES_FOR_DAY, (session_date,))]

    def for_day(self, connection, session_date: _date) -> list[dict]:
        return [self.for_lecture(connection, lecture_id)
                for lecture_id in self.lecture_ids_for_day(connection, session_date)]

    def for_lecture(self, connection, lecture_id) -> dict:
        lecture_id = str(lecture_id)
        rows = _fetch(connection, LECTURE, (lecture_id,))
        if not rows:
            raise PlatformError(DATABASE_ERROR, f"unknown lecture {lecture_id}")
        row = rows[0]
        lecture = {
            "lecture_id": str(row[0]), "subject": row[1],
            "normalized_subject": row[2], "module": row[3],
            "session_date": row[4].isoformat(),
            "scheduled_start": row[5].isoformat() if row[5] else None,
            "is_cancelled": row[6], "discovery_status": row[7],
            "calendar_mapping_status": row[8], "group_match_status": row[9],
            "meeting_id": row[10], "downstream_ready": row[11],
            "scheduled_start_raw": row[5],
            "meeting_resolution_reason": None,
            "duplicate_suppression": row[12],
        }

        # Phase 4C1. A suppressed duplicate is answered here and nowhere else.
        # Short-circuiting before a single downstream query is not an
        # optimisation: it is what makes "cannot enter downstream processing"
        # structural rather than a rule fifteen stages have to remember.
        if lecture["duplicate_suppression"]:
            return self._suppressed_state(lecture)

        duplicate = self._duplicate(connection, lecture)
        occurrence = _fetch(connection, OCCURRENCE, (lecture_id,))
        lecture["_occurrence"] = occurrence[0] if occurrence else None

        # Phase 3C3D/3C3E already answers attendance onwards for one lecture,
        # attempt budget and all. Re-deriving any of it here would be a second
        # implementation of the hardest half of the state model.
        downstream = recovery_state(connection, lecture_id)

        stages = {}
        stages[DISCOVERY] = self._discovery(lecture, duplicate)
        stages[MEETING] = self._meeting(lecture, duplicate)
        transcript, artifacts = self._transcript(connection, lecture)
        stages[TRANSCRIPT] = transcript
        selection, selection_row = self._selection(connection, lecture, artifacts)
        stages[SELECTION] = selection
        current_evaluation = downstream.get("current_evaluation")
        cues, document = self._canonical_cues(connection, lecture, selection_row,
                                              current_evaluation)
        stages[CANONICAL_CUES] = cues
        stages[SPEAKERS] = self._speakers(connection, lecture, document)
        stages[ATTENDANCE] = self._attendance(connection, lecture, downstream)
        engagement_stage, engagement_row = self._engagement(
            connection, lecture, downstream, document)
        stages[ENGAGEMENT] = engagement_stage
        stages[QA_EVALUATION] = self._qa_evaluation(lecture, downstream,
                                                    engagement_row, document,
                                                    selection_row)
        stages[QA_RENDER] = self._qa_render(lecture, downstream)
        legacy_sync, legacy_session_id = self._legacy_qa_sync(
            connection, lecture, downstream)
        stages[LEGACY_QA_SYNC] = legacy_sync
        eligibility, perfect_result = self._perfect_eligibility(lecture, downstream)
        stages[PERFECT_ELIGIBILITY] = eligibility
        perfect_sync, perfect_key = self._perfect_sync(
            connection, lecture, perfect_result)
        stages[PERFECT_SYNC] = perfect_sync
        stages[RECORDING_LINK] = self._recording_link(connection, lecture,
                                                      legacy_session_id)
        stages[EXCEL_SYNC] = self._excel_sync(connection, perfect_result, perfect_key)

        headline, runnable, operator_actions = self._next_action(stages)
        next_action, blocking_stage = headline
        executable_action, executable_stage = runnable
        executable = (NOTHING_TO_DO
                      if executable_stage is None
                      or executable_stage in self.observed_only_stages
                      else executable_action)
        lecture.pop("_occurrence", None)
        return {
            **lecture,
            "orchestration_version": ORCHESTRATION_VERSION,
            "stages": stages,
            "stage_order": list(STAGE_ORDER),
            "next_action": next_action,
            "next_executable_action": executable,
            "blocking_stage": blocking_stage,
            "executable_stage": executable_stage,
            # What a human still owes. Stepping over these while looking for
            # runnable work must never mean forgetting them.
            "operator_actions": operator_actions,
            "is_complete": all(item["state"] in SETTLED_STATES
                               for item in stages.values()),
            "requires_review": any(item["state"] in (REVIEW_REQUIRED, FAILED)
                                   for item in stages.values()),
            "is_waiting": next_action in (WAIT_FOR_ATTENDANCE_SOURCE,
                                          WAIT_FOR_RECORDING, WAIT_FOR_EXCEL_SYNC),
            "attendance_coverage_status": downstream["attendance_coverage_status"],
            "attendance_source_authoritative":
                downstream["attendance_source_authoritative"],
            # Informational only: missing attendance never means QA failed.
            **attendance_flag(downstream["attendance_source_authoritative"]),
            "perfect_policy_version": self.perfect_eligibility_version,
            "is_suppressed_duplicate": False,
            "duplicate_resolution": duplicate,
            "versions": {
                "selection_version": self.selection_version,
                "parser_versions": list(self.parser_versions),
                "speaker_inventory_version": self.speaker_inventory_version,
                "attendance_resolution_version": self.attendance_resolution_version,
                "renderer_version": self.renderer_version,
                "writer_version": self.writer_version,
                "perfect_writer_version": self.perfect_writer_version,
                "perfect_eligibility_version": self.perfect_eligibility_version,
                "evidence_policy_version": self.evidence_policy_version,
                "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
            },
            "downstream": downstream,
        }

    # -- duplicate calendar events ------------------------------------------

    def _duplicate(self, connection, lecture):
        """
        This lecture's place in its duplicate candidate group, or None.

        A lecture with no sibling at the same identity is the overwhelmingly
        common case and costs one indexed lookup; the answer is then None and
        every stage behaves exactly as it did before Phase 4C1.
        """
        decision = self.duplicate_service.classify_lecture(
            connection, lecture["lecture_id"])
        if decision["case"] == CASE_NOT_DUPLICATE:
            return None
        return {"case": decision["case"], "role": decision["role"],
                "group_size": decision["group_size"],
                "requires_manual_review": decision["requires_manual_review"],
                "winner_lecture_id": (decision["winner"]["lecture_id"]
                                      if decision["winner"] else None),
                "winner_meeting_id": (decision["winner"]["meeting_id"]
                                      if decision["winner"] else None),
                "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
                "sibling_lecture_ids": [member["lecture_id"]
                                        for member in decision["members"]
                                        if member["lecture_id"]
                                        != lecture["lecture_id"]]}

    def _is_suppressible(self, duplicate) -> bool:
        return bool(duplicate and duplicate["case"] == CASE_SUPPRESSED
                    and duplicate["role"] == "SUPPRESSIBLE_DUPLICATE")

    def _suppressed_state(self, lecture) -> dict:
        """
        The whole matrix for an occurrence that IS a suppressed duplicate.

        Every stage is NOT_APPLICABLE, which is a settled state, so the lecture
        is complete, needs no review, and offers the orchestrator nothing to
        do. No downstream table is read to produce this answer, and no
        downstream table could change it.
        """
        annotation = lecture["duplicate_suppression"]
        stages = {stage: _stage(NOT_APPLICABLE, reason=SUPPRESSED_REASON,
                                winner_lecture_id=annotation.get(
                                    "winner_lecture_id"))
                  for stage in STAGE_ORDER}
        return {
            **lecture,
            "orchestration_version": ORCHESTRATION_VERSION,
            "stages": stages, "stage_order": list(STAGE_ORDER),
            "next_action": NOTHING_TO_DO,
            "next_executable_action": NOTHING_TO_DO,
            "blocking_stage": None, "executable_stage": None,
            "operator_actions": [],
            "is_complete": True, "requires_review": False, "is_waiting": False,
            "is_suppressed_duplicate": True,
            "duplicate_resolution": {
                "case": CASE_SUPPRESSED, "role": "SUPPRESSED_DUPLICATE",
                "winner_lecture_id": annotation.get("winner_lecture_id"),
                "winner_meeting_id": annotation.get("winner_meeting_id"),
                "requires_manual_review": False,
                "duplicate_resolution_version": annotation.get(
                    "duplicate_resolution_version"),
                "resolved_at": annotation.get("resolved_at")},
            "attendance_coverage_status": None,
            "attendance_source_authoritative": False,
            "perfect_policy_version": self.perfect_eligibility_version,
            "versions": {"duplicate_resolution_version":
                         DUPLICATE_RESOLUTION_VERSION},
            "downstream": {"attendance_coverage_status": None,
                           "attendance_source_authoritative": False},
        }

    # -- individual stages --------------------------------------------------

    def _discovery(self, lecture, duplicate=None) -> dict:
        status = lecture["discovery_status"]
        if self._is_suppressible(duplicate):
            # Discovery found the event and described it correctly. What is
            # outstanding is not a better discovery run - it is the decision
            # that this event is a duplicate, and that decision is runnable.
            return _stage(MISSING, action=SUPPRESS_DUPLICATE_EVENT,
                          reason=PENDING_REASON, discovery_status=status,
                          winner_lecture_id=duplicate["winner_lecture_id"])
        if status == "READY":
            return _stage(COMPLETE, discovery_status=status)
        # UNMATCHED and REVIEW are both "a human has to look": re-running
        # discovery will produce the same answer, because the answer is about
        # the calendar event, not about whether we asked recently enough.
        return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                      discovery_status=status,
                      group_match_status=lecture["group_match_status"],
                      **({"reason": duplicate["case"]} if duplicate else {}))

    def _meeting(self, lecture, duplicate=None) -> dict:
        mapping = lecture["calendar_mapping_status"]
        if duplicate is not None and duplicate["requires_manual_review"]:
            # Checked BEFORE the resolved case, and deliberately. In a group
            # the rule refuses to decide, holding a meeting does not make this
            # occurrence the lecture - two events that both resolved would
            # otherwise each run the pipeline and each write a legacy row for
            # one real class. Stopping both is the safe direction to be wrong.
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason=duplicate["case"],
                          calendar_mapping_status=mapping,
                          duplicate_group_size=duplicate["group_size"],
                          sibling_lecture_ids=duplicate["sibling_lecture_ids"],
                          winner_lecture_id=duplicate["winner_lecture_id"])
        if mapping in RESOLVED_MAPPING_STATUSES and lecture["meeting_id"]:
            return _stage(COMPLETE, calendar_mapping_status=mapping,
                          meeting_id=lecture["meeting_id"])
        if self._is_suppressible(duplicate):
            # There is no meeting to resolve here and there never was: the
            # sibling event carries the lecture. Saying RESOLVE_MEETING would
            # send the scheduler to Graph for a meeting that does not exist.
            return _stage(MISSING, action=SUPPRESS_DUPLICATE_EVENT,
                          reason=PENDING_REASON,
                          calendar_mapping_status=mapping,
                          winner_lecture_id=duplicate["winner_lecture_id"],
                          winner_meeting_id=duplicate["winner_meeting_id"])
        if mapping in AMBIGUOUS_MAPPING_STATUSES:
            # Re-running discovery will produce the same ambiguity, because the
            # ambiguity is in the calendar, not in how recently we asked.
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          calendar_mapping_status=mapping)
        return _stage(MISSING, action=RESOLVE_MEETING,
                      calendar_mapping_status=mapping,
                      reason=(lecture.get("meeting_resolution_reason")
                              or "NO_ONLINE_MEETING_RESOLVED"))

    def _transcript(self, connection, lecture):
        row = _fetch(connection, ARTIFACTS,
                     (list(CONTENT_STORED_STATUSES), list(TRANSCRIPT_FAILED_STATUSES),
                      lecture["lecture_id"]))[0]
        artifacts = {"artifact_count": row[0], "stored_count": row[1],
                     "failed_count": row[2], "newest_content_at": row[3]}
        if lecture["is_cancelled"] and not row[0]:
            return _stage(NOT_APPLICABLE, reason="CANCELLED_SESSION",
                          **artifacts), artifacts
        if row[1]:
            return _stage(COMPLETE, **artifacts), artifacts
        if row[2]:
            return _stage(FAILED, action=ACQUIRE_TRANSCRIPT, **artifacts), artifacts
        return _stage(MISSING, action=ACQUIRE_TRANSCRIPT, **artifacts), artifacts

    def _selection(self, connection, lecture, artifacts):
        rows = _fetch(connection, SELECTION_ROW,
                      (lecture["lecture_id"], self.selection_version))
        if not rows:
            if lecture["is_cancelled"] and not artifacts["stored_count"]:
                return _stage(NOT_APPLICABLE, reason="CANCELLED_SESSION"), None
            return _stage(MISSING, action=SELECT_TRANSCRIPT,
                          selection_version=self.selection_version), None
        row = rows[0]
        selection = {"selection_id": str(row[0]), "selection_status": row[1],
                     "selected_part_count": row[2],
                     "selection_version": self.selection_version}
        # Carried to the later stages, never reported as stage detail.
        lineage = {**selection, "combined_content_sha256": row[3],
                   "actual_start": row[6], "actual_end": row[7],
                   "combined_duration_minutes": row[9],
                   "combined_duration_seconds": row[10]}
        if row[1] != "SELECTED":
            # A real, recorded answer of "nothing to select". Re-running is
            # free (pure DB) and is exactly what makes a late transcript
            # recoverable, so this is MISSING rather than FAILED.
            return _stage(MISSING, action=SELECT_TRANSCRIPT, **selection), lineage
        freshness = self._selection_freshness(connection, lecture, row)
        if freshness.pop("stale"):
            # The evidence the selector would read NOW differs from what it
            # read when it chose. Re-running is pure DB and cheap.
            return _stage(STALE, action=SELECT_TRANSCRIPT, **freshness,
                          **selection), lineage
        return _stage(COMPLETE, **freshness, **selection), lineage

    def _selection_freshness(self, connection, lecture, row) -> dict:
        """
        Is this selection still the answer for the evidence we hold?

        Judged on MEANINGFUL INPUTS, through the selector's own definition of
        which candidates are relevant to this occurrence. It used to compare
        the newest `content_fetched_at` across every candidate linked to the
        lecture - which, for a Teams series meeting, is every occurrence's
        transcript - against the selection's `updated_at`. A 09-11 transcript,
        or a Graph re-fetch of identical bytes, made 09-04 STALE.

        Selections written since `selection_input_fingerprint_v1` carry the
        fingerprint of their relevant candidates, and this recomputes it. Older
        rows carry none, so for them the question is answered directly: would
        re-running the selector pick different parts, or recombine different
        bytes? Either way nothing here depends on when anything was fetched.
        """
        occurrence = lecture.get("_occurrence") or _fetch(
            connection, OCCURRENCE, (lecture["lecture_id"],))[0]
        schedule = {"scheduled_start": occurrence[0], "scheduled_end": occurrence[1],
                    "target_date": occurrence[2], "meeting_id": occurrence[3]}
        candidates = self.selection_repository.load_candidates(
            connection, lecture["lecture_id"])
        relevance = relevant_candidates(candidates, **schedule)
        detail = {"relevant_candidate_count": len(relevance.candidates),
                  "selection_input_fingerprint_prefix": relevance.fingerprint[:16]}
        stored = row[5]
        if stored:
            if stored != relevance.fingerprint:
                return {"stale": True, "reason": "SELECTION_INPUTS_CHANGED",
                        "freshness_basis": "SELECTION_INPUT_FINGERPRINT", **detail}
            return {"stale": False, "freshness_basis": "SELECTION_INPUT_FINGERPRINT",
                    **detail}

        basis = {"freshness_basis": "LEGACY_SELECTION_EQUIVALENCE", **detail}
        outcome = select_transcript_parts(candidates, **schedule)
        if outcome.status != SELECTED:
            return {"stale": True, "reason": "SELECTION_OUTCOME_CHANGED", **basis}
        persisted = [canonical_key(item[0]) for item in
                     _fetch(connection, SELECTED_PART_IDS, (row[0],))]
        chosen = [canonical_key(part.candidate.provider_transcript_id)
                  for part in outcome.parts]
        if persisted != chosen:
            return {"stale": True, "reason": "SELECTED_PARTS_CHANGED", **basis}
        recombined = combined_source_fingerprint(
            self.selection_version,
            [part.candidate.content_sha256 for part in outcome.parts])
        if row[8] is None or recombined != row[8]:
            return {"stale": True, "reason": "SELECTED_CONTENT_CHANGED", **basis}
        return {"stale": False, **basis}

    def _canonical_cues(self, connection, lecture, selection, current_evaluation):
        """
        Which document is the CURRENT one is not a platform-wide preference.

        A lecture may legitimately hold two canonical documents - Phase 3C2.3B
        rebuilt exactly one lecture under `webvtt_canonical_v2_seam_dedup`
        without invalidating anything parsed under v1 - and "newest wins" would
        declare a lecture stale the moment a second parser version existed
        anywhere. The evaluation is what declares which document the lecture's
        answer was built from, so the evaluation decides.
        """
        rows = _fetch(connection, DOCUMENTS,
                      (lecture["lecture_id"], self.parser_versions))
        if not rows:
            if lecture["is_cancelled"]:
                return _stage(NOT_APPLICABLE, reason="CANCELLED_SESSION"), None
            return _stage(MISSING, action=BUILD_CANONICAL_CUES,
                          parser_versions=self.parser_versions), None
        if selection is not None:
            # Same selection AND parsed from the bytes it holds now. A
            # reselection rewrites the selection and combined rows in place,
            # so selection_id alone would keep a document parsed from the
            # superseded content looking current.
            matching = [item for item in rows
                        if str(item[4]) == str(selection["selection_id"])
                        and (selection.get("combined_content_sha256") is None
                             or item[6] == selection["combined_content_sha256"])]
        else:
            matching = []
        declared = (current_evaluation or {}).get("document_id")
        if declared:
            anchored = [item for item in matching if str(item[0]) == str(declared)]
            if anchored:
                matching = anchored
        if not matching:
            # Documents exist, but none was parsed from the selection we now
            # hold. That is a lineage break, not an absence.
            return _stage(STALE, action=BUILD_CANONICAL_CUES,
                          reason="NO_DOCUMENT_FOR_CURRENT_SELECTION",
                          document_count=len(rows)), None
        row = matching[0]
        document = {"document_id": str(row[0]), "parser_version": row[1],
                    "parse_status": row[2], "cue_count": row[3]}
        if row[2] in ("PARSED", "PARSED_WITH_WARNINGS"):
            return _stage(COMPLETE, **document), document
        if row[2] == "EMPTY_TRANSCRIPT":
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          **document), document
        return _stage(FAILED, action=MANUAL_REVIEW_REQUIRED, **document), document

    def _speakers(self, connection, lecture, document) -> dict:
        if document is None:
            if lecture["is_cancelled"]:
                return _stage(NOT_APPLICABLE, reason="CANCELLED_SESSION")
            return _stage(MISSING, action=BUILD_CANONICAL_CUES,
                          reason="NO_CURRENT_CANONICAL_DOCUMENT")
        count = _fetch(connection, SPEAKER_COUNT,
                       (document["document_id"], self.speaker_inventory_version))[0][0]
        if count:
            return _stage(COMPLETE, speaker_count=count,
                          speaker_inventory_version=self.speaker_inventory_version)
        return _stage(MISSING, action=RESOLVE_SPEAKERS, speaker_count=0,
                      speaker_inventory_version=self.speaker_inventory_version)

    def _attendance(self, connection, lecture, downstream) -> dict:
        status = downstream["attendance_coverage_status"]
        detail = {
            "attendance_coverage_status": status,
            "attendance_source_authoritative":
                downstream["attendance_source_authoritative"],
            "attendance_snapshot_id": downstream.get("attendance_snapshot_id"),
            "attendance_source_rows_any_status":
                downstream.get("attendance_source_rows_any_status"),
        }
        if is_authoritative(status):
            return _stage(COMPLETE, **detail)
        if downstream.get("attendance_snapshot_id") is None:
            # Never resolved at all. Phase 2C3 has simply not run.
            return _stage(MISSING, action=RESOLVE_ATTENDANCE, **detail)
        # Resolved, and the honest answer was "the source has not told us".
        # The only thing that changes this is the source itself, so the state
        # is WAITING - work we cannot do, not work we forgot. It is a FLAG on
        # the lecture, not a gate: `_next_action` steps over it, so QA, render
        # and the legacy sync still run from the transcript.
        detail.update(attendance_flag(False), blocks_qa=False)
        if self.attendance_probe is not None and self.attendance_probe(
                connection, lecture):
            return _stage(STALE, action=RECOVER_ATTENDANCE,
                          reason="ATTENDANCE_SOURCE_NOW_HAS_ROWS", **detail)
        return _stage(WAITING, action=WAIT_FOR_ATTENDANCE_SOURCE, **detail)

    def _engagement(self, connection, lecture, downstream, document):
        """
        Engagement identity is (document, snapshot, algorithm versions), so a
        lecture holding two canonical documents legitimately holds two
        engagement rows for the SAME attendance snapshot. Picking the newest of
        those and calling the other one stale would report a lecture as needing
        work purely because two rows shared a timestamp and a uuid broke the
        tie - which is a property of the rows, not of the lecture.

        The current row is the one matching BOTH the current document and the
        current snapshot. Anything else is a real lineage break.
        """
        snapshot_id = downstream.get("attendance_snapshot_id")
        rows = _fetch(connection, ENGAGEMENT_LINEAGE, (lecture["lecture_id"],))
        if not rows:
            if snapshot_id is None:
                return _stage(MISSING, action=RESOLVE_ATTENDANCE,
                              reason="NO_ATTENDANCE_SNAPSHOT"), None
            return _stage(MISSING, action=CALCULATE_ENGAGEMENT), None

        def _row(item):
            return {"engagement_id": str(item[0]),
                    "document_id": str(item[1]) if item[1] else None,
                    "attendance_snapshot_id": str(item[2]),
                    "calculation_status": item[3], "attended_count": item[4],
                    "spoke_count": item[5], "item7_override_applied": item[6],
                    "engagement_score": item[7],
                    "engagement_status_detail": item[8] or item[3],
                    "attendance_zero_confirmed": item[9]}

        candidates = [_row(item) for item in rows]
        current = [item for item in candidates
                   if str(item["attendance_snapshot_id"]) == str(snapshot_id)
                   and (document is None
                        or str(item["document_id"]) == str(document["document_id"]))]
        if not current:
            return _stage(STALE, action=CALCULATE_ENGAGEMENT,
                          reason="NO_ENGAGEMENT_FOR_CURRENT_DOCUMENT_AND_SNAPSHOT",
                          engagement_row_count=len(candidates),
                          attendance_snapshot_id=str(snapshot_id)), None
        row = current[0]
        return _stage(COMPLETE, is_current_snapshot=True, **row), row

    def _qa_evaluation(self, lecture, downstream, engagement, document=None,
                       selection=None) -> dict:
        current = downstream.get("current_evaluation")
        if current is None:
            return _stage(MISSING, action=RUN_QA)
        detail = {key: current.get(key) for key in (
            "evaluation_id", "qa_status", "review_reason", "met_count",
            "partial_count", "not_met_count", "provider_contract_version",
            "attempts_used", "attempts_remaining", "model_output_reusable",
            "evidence_policy_version", "source_fingerprint_prefix",
            "attempt_records", "configuration_failures")}
        if (document is not None and current.get("document_id")
                and str(current["document_id"]) != str(document["document_id"])):
            # The answer was built from a DIFFERENT transcript document. The
            # model was asked about other words, so its answer - and a
            # NON_DELIVERED verdict drawn from the old document's length - is
            # not an answer for this one. A real re-evaluation, never a
            # deterministic refresh: the refresh reuses the frozen model
            # output, which is exactly what must not survive a new transcript.
            return _stage(STALE, action=RUN_QA,
                          reason="EVALUATION_DOCUMENT_SUPERSEDED",
                          evaluation_document_id=str(current["document_id"]),
                          current_document_id=str(document["document_id"]),
                          **detail)
        if (current["qa_status"] == "REVIEW_REQUIRED"
                and current.get("review_reason") == TRANSCRIPT_COVERAGE_INCOMPLETE):
            # The lecture ran; its transcript cannot support QA. Re-running
            # the same inputs gives the same answer, so this is a human's
            # decision, not scheduler work. A better transcript arrives as a
            # new document and is caught by the lineage rule above.
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason=TRANSCRIPT_COVERAGE_INCOMPLETE, **detail)
        if current["qa_status"] not in TERMINAL_QA_STATUSES:
            # Phase 4B: an answer rejected by an OLDER evidence rule can be
            # re-judged under the current one for nothing. Trying that before
            # spending a generation is the difference between a free recovery
            # and a paid re-roll of the same dice.
            if (current.get("evidence_policy_version")
                    != self.evidence_policy_version
                    and current.get("model_output_reusable") is not None
                    and current["qa_status"] == "INVALID_EVIDENCE"):
                return _stage(STALE, action=REVALIDATE_EVIDENCE,
                              reason="EVIDENCE_POLICY_SUPERSEDED", **detail)
            # Phase 3C3D's budget, not a fresh count. A contract that has
            # exhausted its attempts must never be handed back to the
            # scheduler as ordinary retryable work.
            if current["attempts_remaining"] > 0:
                if current.get("last_attempt_configuration_failure"):
                    # The provider refused the credential; the lecture itself
                    # is untouched and its budget unspent. Retried as soon as
                    # a cycle finds the credential working.
                    return _stage(REVIEW_REQUIRED, action=RUN_QA,
                                  reason=PROVIDER_CONFIGURATION_ERROR, **detail)
                return _stage(REVIEW_REQUIRED, action=RUN_QA, **detail)
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason="GENERATION_BUDGET_EXHAUSTED", **detail)
        if current["qa_status"] == NON_DELIVERED:
            decision = self._delivery_decision(lecture, selection)
            if decision is not None and decision.classification != NON_DELIVERED:
                # Same document, but the versioned delivery policy no longer
                # calls this lecture undelivered. Re-running QA re-classifies
                # it; the old answer stays on record under its own fingerprint.
                return _stage(STALE, action=RUN_QA,
                              reason="DELIVERY_POLICY_RECLASSIFIES_NON_DELIVERED",
                              delivery_classification=decision.classification,
                              delivery=decision.diagnostics, **detail)
        if (current["qa_status"] == "COMPLETED"
                and not downstream["attendance_source_authoritative"]
                and current.get("carries_attendance_values")):
            # Built before attendance became optional: it states numbers the
            # empty snapshot never supported (0 attended, score 1). If the
            # model answer can still be reused, the fix is the free
            # deterministic refresh, which re-derives those fields as UNKNOWN.
            if (current.get("model_output_reusable")
                    and current.get("provider_contract_version")
                    == DEFAULT_PROVIDER_CONTRACT):
                return _stage(STALE, action=REFRESH_DETERMINISTIC_QA,
                              reason="EVALUATION_CARRIES_UNVERIFIED_ATTENDANCE", **detail)
            # An answer bought under another provider contract answers a
            # different question and cannot be reused; replacing it would be a
            # new paid generation AND a rewrite of a published row. Neither is
            # the scheduler's call.
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason="UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE", **detail)
        engagement = engagement or {}
        if (engagement.get("engagement_id") and current.get("engagement_id")
                and engagement["engagement_id"] != current["engagement_id"]):
            # New attendance evidence. The transcript did not change, so the
            # model answer is still the answer and this is a deterministic
            # refresh rather than a regeneration.
            return _stage(STALE, action=REFRESH_DETERMINISTIC_QA,
                          reason="EVALUATION_PREDATES_CURRENT_ENGAGEMENT", **detail)
        return _stage(COMPLETE, **detail)

    def _delivery_decision(self, lecture, selection):
        """The QA service's own delivery policy, asked of the persisted inputs."""
        if not selection or selection.get("combined_duration_minutes") is None:
            return None
        occurrence = lecture.get("_occurrence")
        if occurrence is None:
            return None
        seconds = selection.get("combined_duration_seconds")
        return classify_delivery(
            duration_minutes=selection["combined_duration_minutes"],
            scheduled_start=occurrence[0], scheduled_end=occurrence[1],
            call_start=selection.get("actual_start"),
            call_end=selection.get("actual_end"),
            transcript_span_seconds=float(seconds) if seconds is not None else None)

    def _qa_render(self, lecture, downstream) -> dict:
        rendered = downstream.get("rendered")
        current = downstream.get("current_evaluation")
        if _is_coverage_review(current):
            # Nothing to render: there is no verdict, and the renderer would
            # refuse one. Not MISSING - there is no work that could fill it.
            return _stage(NOT_APPLICABLE, reason=TRANSCRIPT_COVERAGE_INCOMPLETE,
                          evaluation_id=current.get("evaluation_id"))
        if current is None or current["qa_status"] not in TERMINAL_QA_STATUSES:
            return _stage(MISSING, action=RUN_QA, reason="NO_TERMINAL_EVALUATION")
        if rendered is None:
            return _stage(MISSING, action=RENDER_QA)
        detail = {key: rendered.get(key) for key in (
            "rendered_session_id", "render_status", "renderer_version",
            "evaluation_id", "met_count", "partial_count", "not_met_count")}
        if rendered.get("renderer_version") != self.renderer_version:
            return _stage(STALE, action=RENDER_QA,
                          reason="RENDERER_VERSION_SUPERSEDED", **detail)
        if rendered["render_status"] not in RENDERED_STATUSES:
            return _stage(FAILED, action=RENDER_QA, **detail)
        if str(rendered.get("evaluation_id")) != str(current.get("evaluation_id")):
            return _stage(STALE, action=RENDER_QA,
                          reason="RENDER_BELONGS_TO_SUPERSEDED_EVALUATION", **detail)
        return _stage(COMPLETE, **detail)

    def _legacy_qa_sync(self, connection, lecture, downstream):
        """
        Three genuinely different situations, and conflating any two of them
        would be a production incident.

        1. the coded platform wrote the legacy row -> ours, and current or not;
        2. a legacy row exists that the coded platform did NOT write -> it
           belongs to the n8n pipeline, it is PROTECTED in every mode, and the
           honest state is NOT_APPLICABLE rather than "we still owe a write";
        3. no legacy row at all -> a coded write is genuinely available.

        Case 2 is the whole ownership model. Reporting it as MISSING would put
        a "sync this" instruction in front of an operator for a row we must
        never touch, which is exactly how a careful guard gets clicked through.
        """
        rendered = downstream.get("rendered")
        rows = _fetch(connection, LEGACY_WRITES,
                      (lecture["lecture_id"], self.writer_version))
        written = [item for item in rows if item[3] in OWNED_WRITE_STATUSES]
        current = downstream.get("current_evaluation")
        if _is_coverage_review(current):
            # The canonical answer is "the lecture ran; QA cannot be trusted".
            # qa_doctors_sessions has no truthful encoding of that, so the only
            # correct legacy state is NO coded verdict at all. A coded-owned
            # row written from a superseded evaluation contradicts it and is
            # withdrawn by an operator (`withdraw-legacy-qa`); a foreign row
            # stays untouched by every path, exactly as before.
            if written:
                row = written[0]
                return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                              reason="LEGACY_ROW_CONTRADICTS_CURRENT_EVALUATION",
                              write_id=str(row[0]), write_status=row[3],
                              legacy_session_id=row[5], coded_owned=True,
                              written_from_evaluation_id=str(row[2]),
                              current_evaluation_id=current.get("evaluation_id")), None
            return _stage(NOT_APPLICABLE, reason="NO_PUBLISHABLE_QA_RESULT",
                          review_reason=TRANSCRIPT_COVERAGE_INCOMPLETE), None
        if written:
            row = written[0]
            detail = {"write_id": str(row[0]), "write_status": row[3],
                      "writer_version": row[4], "legacy_session_id": row[5],
                      "coded_owned": True}
            if rendered is not None and str(row[1]) != str(
                    rendered.get("rendered_session_id")):
                return _stage(STALE, action=SYNC_LEGACY_QA,
                              reason="LEGACY_ROW_PREDATES_CURRENT_RENDER",
                              **detail), row[5]
            return _stage(COMPLETE, **detail), row[5]

        if rendered is None or rendered.get("render_status") not in RENDERED_STATUSES:
            return _stage(MISSING, action=RENDER_QA,
                          reason="NOTHING_RENDERED_TO_SYNC"), None
        target = rendered.get("legacy_session_id")
        if target and self.legacy_observations is not None and (
                self.legacy_observations.legacy_qa_session(connection, target)
                is not None):
            return _stage(NOT_APPLICABLE, reason="LEGACY_ROW_NOT_CODED_OWNED",
                          legacy_session_id=target, coded_owned=False,
                          owner="LEGACY_N8N_PIPELINE"), target
        # F-02. "No row under this exact session_id" is not "no row for this
        # lecture": Graph can re-serialize a transcript id, and the old answer
        # here put SYNC_LEGACY_QA in front of the scheduler for lectures n8n
        # had already recorded. Ask the same guard the writer asks.
        if target and self.legacy_observations is not None:
            occurrence = self.legacy_observations.legacy_qa_occurrence(
                connection, lecture_id=lecture["lecture_id"], session_id=target,
                writer_version=self.writer_version)
            detail = {**occurrence.as_dict(), "legacy_session_id": target}
            if occurrence.verdict == FOREIGN_SAME_OCCURRENCE:
                # Exactly the architecture's answer for an exact-id foreign
                # row: protected history, not work we owe.
                return _stage(NOT_APPLICABLE, reason="LEGACY_ROW_NOT_CODED_OWNED",
                              coded_owned=False, owner="LEGACY_N8N_PIPELINE",
                              **{**detail, "legacy_session_id":
                                 occurrence.target_session_id}), \
                    occurrence.target_session_id
            if occurrence.verdict == OWNED_SAME_OCCURRENCE:
                # Ours, under the id it was first published with. The writer
                # re-addresses to that row; it can never insert a second one.
                return _stage(STALE, action=SYNC_LEGACY_QA,
                              reason="LEGACY_ROW_UNDER_EQUIVALENT_SESSION_ID",
                              coded_owned=True,
                              **{**detail, "legacy_session_id":
                                 occurrence.target_session_id}), \
                    occurrence.target_session_id
            if occurrence.verdict == AMBIGUOUS:
                return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                              reason="LEGACY_IDENTITY_AMBIGUOUS",
                              coded_owned=False, **detail), None
        return _stage(MISSING, action=SYNC_LEGACY_QA,
                      writer_version=self.writer_version,
                      legacy_session_id=target, coded_owned=False), None

    def _perfect_eligibility(self, lecture, downstream):
        if _is_coverage_review(downstream.get("current_evaluation")):
            # Perfect is judged from a verdict, and there is none. Any older
            # result was computed from a superseded evaluation and must not be
            # offered as this lecture's answer.
            return _stage(NOT_APPLICABLE, reason="QA_REVIEW_REQUIRED",
                          review_reason=TRANSCRIPT_COVERAGE_INCOMPLETE), None
        results = [item for item in downstream["perfect_results"]
                   if item["eligibility_version"] == self.perfect_eligibility_version]
        if not results:
            return _stage(MISSING, action=EVALUATE_PERFECT,
                          eligibility_version=self.perfect_eligibility_version), None
        result = results[0]
        detail = {"eligibility_version": result["eligibility_version"],
                  "is_perfect": result["is_perfect"], "reason": result["reason"],
                  "attendance_coverage_status": result["attendance_coverage_status"]}
        if result["reason"] == PENDING_ATTENDANCE_DATA:
            # The v2 policy's whole point: an answer that is correctly
            # withheld, not an answer that is missing.
            return _stage(WAITING, action=WAIT_FOR_ATTENDANCE_SOURCE, **detail), result
        return _stage(COMPLETE, **detail), result

    def _perfect_sync(self, connection, lecture, perfect_result):
        if perfect_result is None:
            return _stage(NOT_APPLICABLE, reason="NO_ELIGIBILITY_ANSWER_YET"), None
        rows = _fetch(connection, PERFECT_RESULT_KEY,
                      (lecture["lecture_id"], self.perfect_eligibility_version))
        key = rows[0][0] if rows else None
        if not perfect_result["is_perfect"]:
            return _stage(NOT_APPLICABLE, reason=perfect_result["reason"]), key
        writes = _fetch(connection, PERFECT_WRITES,
                        (lecture["lecture_id"], self.perfect_writer_version))
        written = [item for item in writes if item[0] in OWNED_WRITE_STATUSES]
        if written:
            return _stage(COMPLETE, write_status=written[0][0], coded_owned=True,
                          legacy_lecture_key=written[0][3],
                          eligibility_version=written[0][2]), written[0][3]
        # Same ownership rule as the QA row: a Perfect row this platform did
        # not create belongs to the legacy pipeline and is never rewritten.
        if key and self.legacy_observations is not None and (
                self.legacy_observations.legacy_perfect_row(connection, key)
                is not None):
            return _stage(NOT_APPLICABLE, reason="LEGACY_PERFECT_ROW_NOT_CODED_OWNED",
                          legacy_lecture_key=key, coded_owned=False,
                          owner="LEGACY_N8N_PIPELINE"), key
        return _stage(MISSING, action=SYNC_PERFECT, legacy_lecture_key=key,
                      coded_owned=False), key

    def _recording_link(self, connection, lecture, legacy_session_id) -> dict:
        """
        The recording link carried by this lecture's legacy QA row.

        Present -> COMPLETE, whoever wrote it; it is never re-evaluated.
        Cancelled / non-delivered -> NOT_APPLICABLE: no recording is expected,
        so a missing one is not a defect and is never retried.

        Otherwise it depends on RECORDING_LINK_MODE. In "observe" mode this is
        exactly the Phase 4A answer - MISSING / WAIT_FOR_RECORDING, owned by the
        n8n branch - and `lecture_recording_links` is not even read, so the
        code is safe to deploy before migration 021. In "write" mode the
        stage's own durable state decides: nothing yet, or a retry that is
        due -> LINK_RECORDING; a retry not yet due -> WAITING; a terminal
        outcome -> REVIEW_REQUIRED until an operator acts.
        """
        if legacy_session_id is None or self.legacy_observations is None:
            return _stage(NOT_APPLICABLE, reason="NO_LEGACY_QA_ROW")
        observed = self.legacy_observations.recording_link(
            connection, legacy_session_id)
        if observed is None:
            return _stage(NOT_APPLICABLE, reason="LEGACY_ROW_NOT_FOUND",
                          legacy_session_id=legacy_session_id)
        writable = self.recording_link_mode == "write"
        attempt = (self._recording_links().attempt(connection, lecture["lecture_id"])
                   if writable else None)
        if str(observed.get("recording_url") or "").strip():
            owner = ("CODED_RECORDING_LINK"
                     if attempt and attempt.get("recording_url_written")
                     else "LEGACY_RECORDING_BRANCH")
            return _stage(COMPLETE, legacy_session_id=legacy_session_id, owner=owner)
        legacy_cancelled = str(observed.get("cancelled_session") or "").strip().lower() == "true"
        if lecture.get("is_cancelled") or legacy_cancelled:
            return _stage(NOT_APPLICABLE, reason=NO_RECORDING_EXPECTED_CANCELLED,
                          legacy_session_id=legacy_session_id,
                          cancelled_source=("LECTURE_CANCELLED" if lecture.get("is_cancelled")
                                            else "LEGACY_ROW_CANCELLED_SESSION"))
        if not writable:
            # Observed only: the n8n recording branch still owns the write.
            return _stage(MISSING, action=WAIT_FOR_RECORDING,
                          legacy_session_id=legacy_session_id,
                          owner="LEGACY_RECORDING_BRANCH")
        detail = {"legacy_session_id": legacy_session_id, "owner": "CODED_RECORDING_LINK"}
        if attempt is None or attempt.get("legacy_session_id") != legacy_session_id:
            return _stage(MISSING, action=LINK_RECORDING, **detail)
        last = {"last_status": attempt["status"], "attempt_count": attempt["attempt_count"]}
        if attempt.get("recording_url_written"):
            # We wrote a link and it is gone. Re-linking automatically would
            # overrule whoever cleared it.
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason="RECORDING_URL_CLEARED_AFTER_CODED_WRITE", **detail, **last)
        if attempt["stage_state"] == REVIEW_REQUIRED:
            return _stage(REVIEW_REQUIRED, action=MANUAL_REVIEW_REQUIRED,
                          reason=attempt["status"], review_detail=attempt.get("reason"),
                          **detail, **last)
        due = attempt.get("next_attempt_after")
        if attempt["stage_state"] == WAITING and due is not None and due > self._now():
            return _stage(WAITING, action=WAIT_FOR_RECORDING,
                          next_attempt_after=due.isoformat(), **detail, **last)
        return _stage(MISSING, action=LINK_RECORDING, **detail, **last)

    def _recording_links(self):
        if self.recording_links is None:
            from app.db.repositories.recording_links import RecordingLinkRepository
            self.recording_links = RecordingLinkRepository()
        return self.recording_links

    def _now(self):
        return self.clock() if self.clock else _datetime.now(_timezone.utc)

    def _excel_sync(self, connection, perfect_result, perfect_key) -> dict:
        if perfect_result is None or not perfect_result["is_perfect"]:
            return _stage(NOT_APPLICABLE, reason="NOT_A_PERFECT_LECTURE")
        if perfect_key is None or self.legacy_observations is None:
            return _stage(NOT_APPLICABLE, reason="NO_LEGACY_PERFECT_ROW")
        observed = self.legacy_observations.excel_sync(connection, perfect_key)
        if observed is None:
            return _stage(NOT_APPLICABLE, reason="LEGACY_PERFECT_ROW_NOT_FOUND",
                          legacy_lecture_key=perfect_key)
        if observed.get("excel_synced_at"):
            return _stage(COMPLETE, legacy_lecture_key=perfect_key,
                          owner="LEGACY_EXCEL_SYNC_WORKFLOW")
        return _stage(MISSING, action=WAIT_FOR_EXCEL_SYNC,
                      legacy_lecture_key=perfect_key,
                      owner="LEGACY_EXCEL_SYNC_WORKFLOW")

    # -- the resume rule ----------------------------------------------------

    def _next_action(self, stages):
        """
        Two answers, because there are two questions.

        `next_action` is the action for the EARLIEST unsettled stage, in
        declared order. First-wins, not most-severe-wins: a lecture with a
        missing transcript AND a missing render has one real problem, and the
        render is not it.

        `next_executable_action` is the earliest thing the coded platform can
        do ITSELF, and it steps over anything in `OPERATOR_ONLY_ACTIONS` -
        empty since Phase 4B made the two legacy syncs automatable. Phase 4A
        needed the step-over because a pending human action would otherwise
        block Perfect eligibility for ever; the stage order now states that
        dependency correctly instead, so nothing needs stepping over.

        `operator_actions` collects whatever a human still owes, so re-gating
        an action later loses nothing.

        A WAITING stage is someone else's move - the attendance source, the
        Perfect policy's attendance requirement, a recording - so it is
        stepped over rather than treated as a wall: everything that does not
        need it still runs. Only when nothing else is left does the first wait
        become the answer, which is also how the orchestrator still hands a
        waiting lecture to the free RECOVER_ATTENDANCE probe every cycle.
        Stages that genuinely depend on a waiting one say so themselves
        (Perfect sync on a pending eligibility is NOT_APPLICABLE, Perfect
        eligibility on missing attendance is itself WAITING).
        """
        headline = (NOTHING_TO_DO, None)
        executable = (NOTHING_TO_DO, None)
        first_wait = None
        operator = []
        for stage in STAGE_ORDER:
            item = stages[stage]
            if item["state"] in SETTLED_STATES:
                continue
            action = item["action"] or MANUAL_REVIEW_REQUIRED
            if item["state"] == WAITING or action in IDLE_ACTIONS:
                # Includes the observed-only waits (recording, Excel), so a
                # missing recording link can never hide the attendance wait
                # that keeps the late-attendance probe running.
                if first_wait is None:
                    first_wait = (action, stage)
                continue
            if headline[1] is None:
                headline = (action, stage)
            if action in OPERATOR_ONLY_ACTIONS:
                operator.append({"stage": stage, "action": action,
                                 "state": item["state"]})
                continue
            if executable[1] is None:
                executable = (action, stage)
        if first_wait is not None:
            if headline[1] is None:
                headline = first_wait
            if executable[1] is None:
                executable = first_wait
        return headline, executable, operator
