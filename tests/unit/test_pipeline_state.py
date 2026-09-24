"""
Phase 4A unit tests: the canonical stage-state model and the resume rule.

These run against a stub connection rather than a database, deliberately. The
question being tested is "given these rows, what does the platform conclude?",
and the rows are the input to that question - fetching them from Postgres would
test the fetching, not the concluding, and would make it impossible to write
down the awkward states (a render belonging to a superseded evaluation, an
engagement row for the wrong document) that are exactly where a scheduler goes
wrong.

The stub answers the real SQL, keyed on a fragment unique to each statement, so
a query that changes shape without its fixture changing fails loudly instead of
silently returning somebody else's rows.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.common.errors import PlatformError
from app.orchestration.reconciliation import DayReconciliation, bucket_for
from app.lectures.duplicates import (
    CASE_MULTIPLE_VALID_MEETINGS,
    CASE_NONE_RESOLVED,
    CASE_SHARED_MEETING,
    CASE_SUPPRESSED,
    CASE_UNRESOLVED_NOT_SUPPRESSIBLE,
    DUPLICATE_RESOLUTION_VERSION,
    PENDING_REASON,
    SUPPRESSED_REASON,
)
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
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
    MANUAL_REVIEW_REQUIRED,
    MEETING,
    MISSING,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    OBSERVED_ONLY_STAGES,
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
    RUN_QA,
    SELECT_TRANSCRIPT,
    SELECTION,
    SETTLED_STATES,
    SPEAKERS,
    STAGE_ORDER,
    STAGE_STATES,
    STALE,
    SUPPRESS_DUPLICATE_EVENT,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
    TRANSCRIPT,
    WAIT_FOR_ATTENDANCE_SOURCE,
    WAIT_FOR_RECORDING,
    WAITING,
)
from app.orchestration.state import PipelineStateResolver
from app.qa.evidence_policy import (
    DEFAULT_EVIDENCE_POLICY,
    EVIDENCE_POLICY_V1,
)
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    ELIGIBLE,
    PENDING_ATTENDANCE_DATA,
)
from app.rendering.evidence import RENDERER_VERSION
from app.transcripts.selection import (
    SELECTION_VERSION,
    CandidateArtifact,
    combined_source_fingerprint,
    relevant_candidates,
)
from app.transcripts.speakers import SPEAKER_INVENTORY_VERSION
from app.transcripts.webvtt import PARSER_VERSION
from app.writer.mapping import WRITER_VERSION
from app.writer.perfect_mapping import PERFECT_WRITER_VERSION


LECTURE_ID = "11111111-1111-5111-8111-111111111111"
SNAPSHOT_ID = "22222222-2222-5222-8222-222222222222"
DOCUMENT_ID = "33333333-3333-5333-8333-333333333333"
SELECTION_ID = "44444444-4444-5444-8444-444444444444"
ENGAGEMENT_ID = "55555555-5555-5555-8555-555555555555"
EVALUATION_ID = "66666666-6666-5666-8666-666666666666"
RENDER_ID = "77777777-7777-5777-8777-777777777777"
WRITE_ID = "88888888-8888-5888-8888-888888888888"

FINGERPRINT = "a" * 64
LEGACY_SESSION_ID = "legacy-session-1"
LEGACY_LECTURE_KEY = "2026-09-17|Example Lecture"
NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
SESSION_DATE = date(2026, 9, 17)


# The occurrence the selector judges: 2026-09-17 08:00-10:00 UTC, i.e.
# 11:00-13:00 Cairo, on the fixture's meeting.
SCHEDULED_START = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
SCHEDULED_END = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def candidate_row(transcript_id, start, end, *, sha, call_id="call-1",
                  meeting_id="meeting-1", artifact_id=None, first_seen=None):
    """One row of the selector's own candidate query, in column order."""
    return (artifact_id or f"artifact-{transcript_id}", transcript_id, start, end,
            call_id, meeting_id, sha, 1000, first_seen or NOW - timedelta(days=1))


# Two parts of one call, both inside the occurrence window.
CANDIDATES = [
    candidate_row("transcript-1", SCHEDULED_START, SCHEDULED_START + timedelta(hours=1),
                  sha="1" * 64),
    candidate_row("transcript-2", SCHEDULED_START + timedelta(minutes=65),
                  SCHEDULED_END, sha="2" * 64),
]


def input_fingerprint(rows) -> str:
    """What the selector would have stamped on a selection made from `rows`."""
    candidates = [CandidateArtifact(
        artifact_id=row[0], provider_transcript_id=row[1],
        provider_created_at=row[2], provider_end_at=row[3],
        provider_call_id=row[4], meeting_id=row[5],
        content_sha256=row[6], content_bytes=row[7]) for row in rows]
    return relevant_candidates(candidates, scheduled_start=SCHEDULED_START,
                               scheduled_end=SCHEDULED_END,
                               target_date=SESSION_DATE,
                               meeting_id="meeting-1").fingerprint


COMBINED_FINGERPRINT = combined_source_fingerprint(SELECTION_VERSION, ["1" * 64, "2" * 64])


def selection_row(*, fingerprint=None, combined=COMBINED_FINGERPRINT, status="SELECTED",
                  duration_minutes=120, actual_start=SCHEDULED_START,
                  actual_end=SCHEDULED_END, updated_at=None):
    """The resolver's selection row. `fingerprint=None` means 'as selected'."""
    return (SELECTION_ID, status, 2, "b" * 64, updated_at or NOW - timedelta(hours=3),
            input_fingerprint(CANDIDATES) if fingerprint is None else fingerprint,
            actual_start, actual_end, combined, duration_minutes,
            None if duration_minutes is None else duration_minutes * 60.0)


# Fragments unique to each statement, most specific first.
ROUTES = (
    ("occurrence", "SELECT l.scheduled_start, l.scheduled_end"),
    ("selection_candidates", "a.first_seen_at"),
    ("selected_parts", "FROM public.lecture_transcript_selection_parts p"),
    ("keys", "SELECT r.legacy_lecture_key"),
    ("perfect_results", "SELECT r.eligibility_version"),
    ("perfect_writes_state", "p.result_id"),
    ("perfect_writes", "SELECT p.write_status"),
    ("legacy_writes_state", "SELECT w.write_id, w.rendered_session_id"),
    ("qa_writes", "SELECT w.write_id, w.write_status"),
    ("duplicate_group", "JOIN public.lecture_sessions self"),
    ("lecture_day", "WHERE l.session_date"),
    ("lecture_state", "l.normalized_subject"),
    ("lecture_recovery", "SELECT l.lecture_id, l.subject, l.session_date"),
    ("coverage", "lecture_attendance_snapshots"),
    ("engagement_lineage", "m.engagement_id, m.document_id"),
    ("engagement_recovery", "m.engagement_id, m.calculation_status"),
    ("evaluations", "FROM public.lecture_qa_evaluations e"),
    ("attempts", "lecture_qa_generation_attempts"),
    ("rendered", "lecture_qa_rendered_sessions"),
    ("artifacts", "lecture_transcript_candidates"),
    ("selection", "lecture_transcript_selections"),
    ("documents", "lecture_transcript_documents"),
    ("speakers", "lecture_transcript_speakers"),
)


class StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class StubConnection:
    """Answers the real statements from a dict of fixture rows."""

    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def execute(self, sql, params=None):
        for name, fragment in ROUTES:
            if fragment in sql:
                self.seen.append(name)
                return StubCursor(self.rows.get(name, []))
        raise AssertionError(f"no fixture route for statement: {sql[:120]}")


class StubObservations:
    def __init__(self, *, recording_url=None, excel_synced_at=None,
                 legacy_row=True, perfect_row=True, occurrence=None):
        self.recording_url = recording_url
        self.excel_synced_at = excel_synced_at
        self.legacy_row = legacy_row
        self.perfect_row = perfect_row
        # F-02. What the same-occurrence guard would say. The default world of
        # this stub holds no other legacy rows, so the honest default is CLEAR.
        self.occurrence = occurrence

    def legacy_qa_occurrence(self, connection, *, lecture_id, session_id,
                             writer_version):
        from app.writer.legacy_identity import CLEAR, OccurrenceVerdict

        return self.occurrence or OccurrenceVerdict(CLEAR)

    def legacy_qa_session(self, connection, legacy_session_id):
        if not self.legacy_row:
            return None
        return {"legacy_session_id": legacy_session_id,
                "recording_url": self.recording_url}

    def recording_link(self, connection, legacy_session_id):
        return self.legacy_qa_session(connection, legacy_session_id)

    def legacy_perfect_row(self, connection, key):
        return self.excel_sync(connection, key)

    def excel_sync(self, connection, key):
        if not self.perfect_row:
            return None
        return {"legacy_lecture_key": key, "excel_synced_at": self.excel_synced_at}


def group_row(lecture_id=None, *, meeting_id="meeting-1",
              mapping="RESOLVED", organizer="ORGANIZER_ID_CONFIRMED",
              downstream_ready=True, suppression=None, event_id=None,
              i_cal_uid=None, subject="Example Lecture",
              normalized="example lecture", module="Example Module",
              start=None, end=None, cancelled=False, discovery="READY"):
    """
    One row of the duplicate candidate group query, in column order.

    Defaults describe a confidently resolved occurrence, so a test that cares
    about the unresolved sibling only has to say which parts are missing.
    """
    lecture_id = lecture_id or LECTURE_ID
    return (lecture_id, event_id or f"event-{lecture_id[:4]}",
            i_cal_uid or f"ical-{lecture_id[:4]}", meeting_id, mapping,
            organizer, discovery, downstream_ready, cancelled, SESSION_DATE,
            normalized, module, start or NOW,
            end or (NOW + timedelta(hours=2)), subject, suppression)


def complete_rows(**overrides) -> dict:
    """
    Every row a fully finished lecture would have.

    Written out longhand on purpose. Each test then changes exactly one thing,
    so what the test is about is the diff rather than a paragraph of setup.
    """
    rows = {
        "lecture_state": [(LECTURE_ID, "Example Lecture", "example lecture",
                           "Example Module", SESSION_DATE, NOW, False, "READY",
                           "RESOLVED", "MATCHED", "meeting-1", True, None)],
        "lecture_recovery": [(LECTURE_ID, "Example Lecture", SESSION_DATE,
                              False, True)],
        # A group of one: the ordinary case, and the one that must behave
        # exactly as it did before Phase 4C1.
        "duplicate_group": [group_row()],
        "lecture_day": [(LECTURE_ID,)],
        "coverage": [(SNAPSHOT_ID, 12, 12, 12, ATTENDANCE_RESOLUTION_VERSION, 12)],
        "artifacts": [(2, 2, 0, NOW - timedelta(hours=4))],
        "occurrence": [(SCHEDULED_START, SCHEDULED_END, SESSION_DATE, "meeting-1")],
        "selection_candidates": list(CANDIDATES),
        "selected_parts": [("transcript-1",), ("transcript-2",)],
        "selection": [selection_row()],
        "documents": [(DOCUMENT_ID, PARSER_VERSION, "PARSED", 400, SELECTION_ID, NOW,
                       "b" * 64)],
        "speakers": [(9,)],
        "engagement_lineage": [(ENGAGEMENT_ID, DOCUMENT_ID, SNAPSHOT_ID,
                                "CALCULATED", 12, 9, True, 4, "CALCULATED",
                                False, NOW)],
        "engagement_recovery": [(ENGAGEMENT_ID, "CALCULATED", 12, 9, 4, True,
                                 "SOURCE_AVAILABLE_WITH_MEMBERS", "CALCULATED",
                                 SNAPSHOT_ID, False, "attendance_coverage_v1")],
        "evaluations": [(EVALUATION_ID, FINGERPRINT, "COMPLETED", None, 1, True,
                         11, 0, 0, "strict_json_schema_v1",
                         "canonical_cue_bounds_v1", NOW, ENGAGEMENT_ID,
                         SNAPSHOT_ID, "c" * 64, None, DOCUMENT_ID,
                         DEFAULT_EVIDENCE_POLICY)],
        "attempts": [(FINGERPRINT, 1, 1, NOW, NOW, ["SUCCESS"], [""], [""], [""], None)],
        "rendered": [(RENDER_ID, "RENDERED", RENDERER_VERSION, FINGERPRINT,
                      11, 0, 0, EVALUATION_ID, LEGACY_SESSION_ID)],
        "qa_writes": [(WRITE_ID, "WRITTEN", WRITER_VERSION, LEGACY_SESSION_ID)],
        "legacy_writes_state": [(WRITE_ID, RENDER_ID, EVALUATION_ID, "WRITTEN",
                                 WRITER_VERSION, LEGACY_SESSION_ID, NOW)],
        "perfect_results": [(DEFAULT_PERFECT_ELIGIBILITY_VERSION, True, ELIGIBLE,
                             "SOURCE_AVAILABLE_WITH_MEMBERS", ELIGIBLE, NOW)],
        "keys": [(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, True)],
        "perfect_writes": [("WRITTEN", PERFECT_WRITER_VERSION,
                            DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                            LEGACY_LECTURE_KEY)],
        "perfect_writes_state": [("WRITTEN", PERFECT_WRITER_VERSION,
                                  DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                                  LEGACY_LECTURE_KEY, "result-1")],
    }
    rows.update(overrides)
    return rows


def resolve(rows=None, *, observations=None, probe=None) -> dict:
    connection = StubConnection(rows if rows is not None else complete_rows())
    resolver = PipelineStateResolver(
        legacy_observations=observations or StubObservations(
            recording_url="https://example.invalid/recording",
            excel_synced_at=NOW),
        attendance_probe=probe)
    return resolver.for_lecture(connection, LECTURE_ID)


def states_of(result) -> dict:
    return {stage: item["state"] for stage, item in result["stages"].items()}


# --- 1. a finished lecture ---------------------------------------------------

def test_a_fully_complete_lecture_has_nothing_to_do():
    result = resolve()
    assert states_of(result) == {stage: COMPLETE for stage in STAGE_ORDER}
    assert result["next_action"] == NOTHING_TO_DO
    assert result["next_executable_action"] == NOTHING_TO_DO
    assert result["blocking_stage"] is None
    assert result["is_complete"] is True
    assert result["requires_review"] is False


def test_every_stage_reports_a_state_from_the_declared_vocabulary():
    for stage, item in resolve()["stages"].items():
        assert item["state"] in STAGE_STATES, (stage, item)


# --- 2..7. the upstream stages ----------------------------------------------

def test_a_missing_transcript_asks_for_the_transcript():
    result = resolve(complete_rows(artifacts=[(0, 0, 0, None)], selection=[],
                                   documents=[], speakers=[(0,)]))
    assert result["stages"][TRANSCRIPT]["state"] == MISSING
    assert result["next_action"] == ACQUIRE_TRANSCRIPT
    assert result["blocking_stage"] == TRANSCRIPT


def test_a_failed_transcript_fetch_is_not_the_same_as_a_missing_one():
    result = resolve(complete_rows(artifacts=[(2, 0, 2, None)], selection=[],
                                   documents=[], speakers=[(0,)]))
    assert result["stages"][TRANSCRIPT]["state"] == FAILED
    assert result["next_action"] == ACQUIRE_TRANSCRIPT


def test_a_missing_selection_asks_for_the_selection():
    result = resolve(complete_rows(selection=[], documents=[], speakers=[(0,)]))
    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["next_action"] == SELECT_TRANSCRIPT


def test_transcript_content_arriving_after_the_selection_makes_it_stale():
    """
    A choice is not wrong because evidence arrived late - it is UNINFORMED.

    Since selection_input_fingerprint_v1 "arrived" means a relevant transcript
    the selector has not seen, not a fetch timestamp: a newer fetch of the
    same bytes is the false staleness this used to report.
    """
    late = candidate_row("transcript-3", SCHEDULED_START + timedelta(minutes=125),
                         SCHEDULED_END + timedelta(minutes=30), sha="3" * 64)
    rows = complete_rows(artifacts=[(3, 3, 0, NOW + timedelta(hours=1))],
                         selection_candidates=CANDIDATES + [late])
    result = resolve(rows)
    assert result["stages"][SELECTION]["state"] == STALE
    assert result["stages"][SELECTION]["reason"] == "SELECTION_INPUTS_CHANGED"
    assert result["next_action"] == SELECT_TRANSCRIPT


def test_missing_canonical_cues_ask_for_the_parse():
    result = resolve(complete_rows(documents=[], speakers=[(0,)]))
    assert result["stages"][CANONICAL_CUES]["state"] == MISSING
    assert result["next_action"] == BUILD_CANONICAL_CUES


def test_a_document_parsed_from_a_different_selection_is_a_lineage_break():
    rows = complete_rows(documents=[(DOCUMENT_ID, PARSER_VERSION, "PARSED", 400,
                                     "other-selection", NOW, "b" * 64)])
    result = resolve(rows)
    assert result["stages"][CANONICAL_CUES]["state"] == STALE
    assert result["next_action"] == BUILD_CANONICAL_CUES


def test_missing_speakers_ask_for_the_speaker_inventory():
    result = resolve(complete_rows(speakers=[(0,)]))
    assert result["stages"][SPEAKERS]["state"] == MISSING
    assert result["next_action"] == RESOLVE_SPEAKERS


def test_an_unresolved_meeting_with_no_sibling_asks_for_the_meeting():
    rows = complete_rows(lecture_state=[(
        LECTURE_ID, "Example Lecture", "example lecture", "Example Module",
        SESSION_DATE, NOW, False, "READY", "ONLINE_MEETING_NOT_FOUND",
        "MATCHED", None, True, None)],
        duplicate_group=[group_row(meeting_id=None,
                                   mapping="ONLINE_MEETING_NOT_FOUND",
                                   organizer="NOT_ATTEMPTED",
                                   downstream_ready=False)])
    result = resolve(rows)
    assert result["stages"][MEETING]["state"] == MISSING
    assert result["next_action"] == RESOLVE_MEETING


SIBLING_ID = "99999999-9999-5999-8999-999999999999"


def duplicate_pair(**loser):
    """
    The real 2026-09-18 Ray-MSP shape: two calendar events at the same instant
    with the same normalized subject and module, different iCalUIDs, and only
    one of them a real Teams meeting.
    """
    defaults = {"meeting_id": None, "mapping": "ONLINE_MEETING_NOT_FOUND",
                "organizer": "NOT_ATTEMPTED", "downstream_ready": False,
                "discovery": "REVIEW"}
    return complete_rows(
        lecture_state=[(LECTURE_ID, "Example Lecture  ", "example lecture",
                        "Example Module", SESSION_DATE, NOW, False,
                        defaults["discovery"] if "discovery" not in loser
                        else loser["discovery"],
                        loser.get("mapping", defaults["mapping"]), "MATCHED",
                        loser.get("meeting_id"), False, None)],
        duplicate_group=[group_row(**{**defaults, **loser}),
                         group_row(SIBLING_ID)])


# --- Phase 4C1: deterministic duplicate resolution --------------------------

def test_1_one_valid_meeting_and_one_unresolved_sibling_suppresses_the_empty_one():
    """
    Case A, and the only case that is automated. There is nothing to choose
    between: one candidate holds a confirmed Teams meeting and the other holds
    no meeting at all.
    """
    result = resolve(duplicate_pair())
    meeting = result["stages"][MEETING]
    assert meeting["state"] == MISSING
    assert meeting["reason"] == PENDING_REASON
    assert meeting["winner_lecture_id"] == SIBLING_ID
    assert meeting["winner_meeting_id"] == "meeting-1"
    assert result["next_action"] == SUPPRESS_DUPLICATE_EVENT
    assert result["next_executable_action"] == SUPPRESS_DUPLICATE_EVENT


def test_1b_the_suppressible_duplicate_is_not_a_manual_review_any_more():
    """The whole point of the phase: this exact shape used to need a human."""
    result = resolve(duplicate_pair())
    assert result["next_action"] != MANUAL_REVIEW_REQUIRED
    assert result["requires_review"] is False
    assert result["operator_actions"] == []


def test_1c_discovery_stops_demanding_a_review_it_cannot_resolve():
    """
    The loser\'s discovery_status is REVIEW, and DISCOVERY comes first in the
    resume order - so leaving it alone would have masked the runnable action
    behind a review nobody could clear.
    """
    discovery = resolve(duplicate_pair())["stages"][DISCOVERY]
    assert discovery["state"] == MISSING
    assert discovery["action"] == SUPPRESS_DUPLICATE_EVENT
    assert discovery["discovery_status"] == "REVIEW"


def test_8_two_siblings_with_different_valid_meetings_stay_a_human_decision():
    result = resolve(duplicate_pair(meeting_id="meeting-2", mapping="RESOLVED",
                                    organizer="ORGANIZER_ID_CONFIRMED",
                                    downstream_ready=True, discovery="READY"))
    meeting = result["stages"][MEETING]
    assert meeting["state"] == REVIEW_REQUIRED
    assert meeting["reason"] == CASE_MULTIPLE_VALID_MEETINGS
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_8b_two_events_sharing_one_meeting_are_also_a_human_decision():
    """
    Not two lectures - but still a choice about which event owns the meeting,
    and the platform has no deterministic basis for making it.
    """
    result = resolve(duplicate_pair(meeting_id="meeting-1", mapping="RESOLVED",
                                    organizer="ORGANIZER_ID_CONFIRMED",
                                    downstream_ready=True, discovery="READY"))
    assert result["stages"][MEETING]["reason"] == CASE_SHARED_MEETING
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_9_two_unresolved_siblings_are_never_suppressed_arbitrarily():
    rows = duplicate_pair()
    rows["duplicate_group"] = [
        group_row(meeting_id=None, mapping="ONLINE_MEETING_NOT_FOUND",
                  organizer="NOT_ATTEMPTED", downstream_ready=False),
        group_row(SIBLING_ID, meeting_id=None,
                  mapping="ONLINE_MEETING_NOT_FOUND",
                  organizer="NOT_ATTEMPTED", downstream_ready=False)]
    result = resolve(rows)
    meeting = result["stages"][MEETING]
    assert meeting["state"] == REVIEW_REQUIRED
    assert meeting["reason"] == CASE_NONE_RESOLVED
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_9b_an_ambiguous_sibling_blocks_suppression_of_the_whole_group():
    """
    AMBIGUOUS_ONLINE_MEETING means Graph found SEVERAL candidate meetings -
    evidence a real meeting exists, which is the opposite of the emptiness the
    rule requires. The group is refused whole rather than half-decided.
    """
    rows = duplicate_pair(mapping="AMBIGUOUS_ONLINE_MEETING")
    result = resolve(rows)
    assert result["stages"][MEETING]["reason"] == CASE_UNRESOLVED_NOT_SUPPRESSIBLE
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_10_a_different_module_is_never_auto_deduplicated():
    """
    Two events, same title, different active Aptem groups. The database query
    would not group them and the rule would not either; asserted here because
    a widened group key is the one change that could make this rule dangerous.
    """
    rows = duplicate_pair()
    rows["duplicate_group"] = [group_row(meeting_id=None,
                                         mapping="ONLINE_MEETING_NOT_FOUND",
                                         organizer="NOT_ATTEMPTED",
                                         downstream_ready=False,
                                         module="Other Module")]
    result = resolve(rows)
    assert result["next_action"] != SUPPRESS_DUPLICATE_EVENT
    assert result["stages"][MEETING]["action"] == RESOLVE_MEETING


def test_11_a_different_start_time_is_never_auto_deduplicated():
    rows = duplicate_pair()
    rows["duplicate_group"] = [group_row(meeting_id=None,
                                         mapping="ONLINE_MEETING_NOT_FOUND",
                                         organizer="NOT_ATTEMPTED",
                                         downstream_ready=False,
                                         start=NOW + timedelta(hours=3))]
    result = resolve(rows)
    assert result["next_action"] != SUPPRESS_DUPLICATE_EVENT


def test_a_winner_with_an_unconfirmed_organizer_does_not_win():
    """
    Stricter than `downstream_ready` on purpose. A lecture is about to be
    declared a duplicate of this one; a meeting whose organizer was never
    confirmed is not a strong enough basis for retiring another occurrence.
    """
    rows = duplicate_pair()
    rows["duplicate_group"] = [
        group_row(meeting_id=None, mapping="ONLINE_MEETING_NOT_FOUND",
                  organizer="NOT_ATTEMPTED", downstream_ready=False),
        group_row(SIBLING_ID, organizer="ORGANIZER_ID_UNAVAILABLE")]
    result = resolve(rows)
    assert result["stages"][MEETING]["reason"] == CASE_NONE_RESOLVED
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_an_ambiguous_meeting_is_a_human_decision_not_a_retry():
    rows = complete_rows(lecture_state=[(
        LECTURE_ID, "Example Lecture", "example lecture", "Example Module",
        SESSION_DATE, NOW, False, "READY", "AMBIGUOUS_ONLINE_MEETING",
        "MATCHED", "meeting-1", True, None)])
    result = resolve(rows)
    assert result["stages"][MEETING]["state"] == REVIEW_REQUIRED
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


def test_an_unmatched_discovery_blocks_at_discovery():
    rows = complete_rows(lecture_state=[(
        LECTURE_ID, "Example Lecture", "example lecture", "Example Module",
        SESSION_DATE, NOW, False, "REVIEW", "RESOLVED", "MATCHED",
        "meeting-1", True, None)])
    result = resolve(rows)
    assert result["stages"][DISCOVERY]["state"] == REVIEW_REQUIRED
    assert result["blocking_stage"] == DISCOVERY
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


# --- 4. engagement -----------------------------------------------------------

def test_missing_engagement_asks_for_the_calculation():
    result = resolve(complete_rows(engagement_lineage=[],
                                   engagement_recovery=[], evaluations=[]))
    assert result["stages"][ENGAGEMENT]["state"] == MISSING
    assert result["next_action"] == CALCULATE_ENGAGEMENT


def test_engagement_for_a_superseded_snapshot_is_stale():
    rows = complete_rows(engagement_lineage=[(
        ENGAGEMENT_ID, DOCUMENT_ID, "old-snapshot", "CALCULATED", 0, 0, False,
        0, "ATTENDANCE_SOURCE_MISSING", False, NOW)])
    result = resolve(rows)
    assert result["stages"][ENGAGEMENT]["state"] == STALE
    assert result["next_action"] == CALCULATE_ENGAGEMENT


def test_two_engagement_rows_on_one_snapshot_do_not_make_a_lecture_stale():
    """
    The real Andrew case. A lecture rebuilt under seam dedup holds two
    canonical documents and therefore two engagement rows for the SAME
    attendance snapshot. Picking the newest and calling the other superseded
    would report a settled lecture as needing work because a uuid broke a tie.
    """
    rows = complete_rows(engagement_lineage=[
        ("other-engagement", "other-document", SNAPSHOT_ID, "CALCULATED", 12, 9,
         True, 4, "CALCULATED", False, NOW),
        (ENGAGEMENT_ID, DOCUMENT_ID, SNAPSHOT_ID, "CALCULATED", 12, 9, True, 4,
         "CALCULATED", False, NOW)])
    result = resolve(rows)
    assert result["stages"][ENGAGEMENT]["state"] == COMPLETE
    assert result["stages"][ENGAGEMENT]["engagement_id"] == ENGAGEMENT_ID
    assert result["next_action"] == NOTHING_TO_DO


def test_the_evaluation_decides_which_canonical_document_is_current():
    rows = complete_rows(documents=[
        ("newer-v2-document", "webvtt_canonical_v2_seam_dedup", "PARSED", 410,
         SELECTION_ID, NOW + timedelta(hours=1), "b" * 64),
        (DOCUMENT_ID, PARSER_VERSION, "PARSED", 400, SELECTION_ID, NOW, "b" * 64)])
    result = resolve(rows)
    assert result["stages"][CANONICAL_CUES]["document_id"] == DOCUMENT_ID
    assert result["next_action"] == NOTHING_TO_DO


# --- 5..6. QA and render -----------------------------------------------------

def test_missing_qa_asks_for_qa():
    result = resolve(complete_rows(evaluations=[], attempts=[], rendered=[],
                                   qa_writes=[], legacy_writes_state=[],
                                   perfect_results=[], keys=[]))
    assert result["stages"][QA_EVALUATION]["state"] == MISSING
    assert result["next_action"] == RUN_QA


def test_an_evaluation_built_on_a_superseded_engagement_refreshes_deterministically():
    """Attendance changed; the transcript did not. The model answer still holds."""
    rows = complete_rows(evaluations=[(
        EVALUATION_ID, FINGERPRINT, "COMPLETED", None, 1, True, 11, 0, 0,
        "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW,
        "superseded-engagement", SNAPSHOT_ID, "c" * 64, None, DOCUMENT_ID,
        DEFAULT_EVIDENCE_POLICY)])
    result = resolve(rows)
    assert result["stages"][QA_EVALUATION]["state"] == STALE
    assert result["next_action"] == REFRESH_DETERMINISTIC_QA


def test_missing_render_asks_for_the_render():
    result = resolve(complete_rows(rendered=[], qa_writes=[],
                                   legacy_writes_state=[]))
    assert result["stages"][QA_RENDER]["state"] == MISSING
    assert result["next_action"] == RENDER_QA


def test_a_render_of_a_superseded_evaluation_is_stale_not_complete():
    rows = complete_rows(rendered=[(RENDER_ID, "RENDERED", RENDERER_VERSION,
                                    "d" * 64, 11, 0, 0, "older-evaluation",
                                    LEGACY_SESSION_ID)])
    result = resolve(rows)
    assert result["stages"][QA_RENDER]["state"] == STALE
    assert result["stages"][QA_RENDER]["reason"] == \
        "RENDER_BELONGS_TO_SUPERSEDED_EVALUATION"
    assert result["next_action"] == RENDER_QA


def test_a_cancelled_session_render_is_a_success_not_a_failure():
    rows = complete_rows(rendered=[(RENDER_ID, "RENDERED_NON_DELIVERED",
                                    RENDERER_VERSION, FINGERPRINT, 0, 0, 11,
                                    EVALUATION_ID, LEGACY_SESSION_ID)])
    result = resolve(rows)
    assert result["stages"][QA_RENDER]["state"] == COMPLETE


# --- 11. review required and the attempt budget ------------------------------

def test_review_required_with_budget_left_is_retryable_qa():
    rows = complete_rows(evaluations=[(
        EVALUATION_ID, FINGERPRINT, "REVIEW_REQUIRED", "INVALID_KSB_TYPE", 1,
        True, 0, 0, 0, "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW,
        ENGAGEMENT_ID, SNAPSHOT_ID, None, None, DOCUMENT_ID,
        DEFAULT_EVIDENCE_POLICY)])
    result = resolve(rows)
    assert result["stages"][QA_EVALUATION]["state"] == REVIEW_REQUIRED
    assert result["stages"][QA_EVALUATION]["attempts_remaining"] > 0
    assert result["next_action"] == RUN_QA


def test_review_required_with_an_exhausted_budget_is_a_human_decision():
    """
    The Phase 3C3D cap, honoured. A scheduler that hands an exhausted contract
    back as ordinary retryable work is a scheduler that buys endless
    generations for a lecture that will keep failing the same way.
    """
    rows = complete_rows(
        evaluations=[(EVALUATION_ID, FINGERPRINT, "REVIEW_REQUIRED",
                      "INVALID_KSB_TYPE", 3, True, 0, 0, 0,
                      "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW,
                      ENGAGEMENT_ID, SNAPSHOT_ID, None, None, DOCUMENT_ID,
                      DEFAULT_EVIDENCE_POLICY)],
        attempts=[(FINGERPRINT, 3, 3, NOW, NOW,
                   ["FAILED", "FAILED", "FAILED"], ["INVALID_KSB_TYPE"] * 3,
                   [""] * 3, [""] * 3, None)])
    result = resolve(rows)
    assert result["stages"][QA_EVALUATION]["attempts_remaining"] == 0
    assert result["stages"][QA_EVALUATION]["reason"] == \
        "GENERATION_BUDGET_EXHAUSTED"
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


# --- 8..9. attendance --------------------------------------------------------

def test_a_missing_attendance_source_waits_rather_than_failing():
    rows = complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)])
    result = resolve(rows)
    assert result["stages"][ATTENDANCE]["state"] == WAITING
    assert result["next_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    assert result["is_waiting"] is True
    assert result["requires_review"] is False


def test_recorded_absences_also_wait_and_never_confirm_a_zero():
    """The G2 Keith blind spot from Phase 3C3E, carried into the state model."""
    rows = complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 1)])
    result = resolve(rows)
    assert result["stages"][ATTENDANCE]["state"] == WAITING
    assert result["stages"][ATTENDANCE][
        "attendance_coverage_status"] == "SOURCE_PARTIAL_OR_INVALID"
    assert result["stages"][ATTENDANCE]["attendance_source_authoritative"] is False


def test_attendance_appearing_turns_the_wait_into_a_recovery():
    rows = complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)])
    result = resolve(rows, probe=lambda connection, lecture: True)
    assert result["stages"][ATTENDANCE]["state"] == STALE
    assert result["stages"][ATTENDANCE]["reason"] == \
        "ATTENDANCE_SOURCE_NOW_HAS_ROWS"
    assert result["next_action"] == RECOVER_ATTENDANCE


def test_no_snapshot_at_all_means_resolve_attendance_not_wait():
    result = resolve(complete_rows(coverage=[], engagement_lineage=[],
                                   engagement_recovery=[], evaluations=[]))
    assert result["stages"][ATTENDANCE]["state"] == MISSING
    assert result["next_action"] == RESOLVE_ATTENDANCE


def test_the_resolver_asks_the_attendance_source_nothing_by_default():
    connection = StubConnection(complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)]))
    resolver = PipelineStateResolver(legacy_observations=StubObservations())
    result = resolver.for_lecture(connection, LECTURE_ID)
    assert result["next_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    # Not one statement against the external attendance table.
    assert not [name for name in connection.seen if "kbc_attendance" in name]


# --- 7, 10. the sync stages and ownership ------------------------------------

def test_a_missing_legacy_qa_row_asks_for_the_sync():
    result = resolve(complete_rows(qa_writes=[], legacy_writes_state=[]),
                     observations=StubObservations(legacy_row=False,
                                                   perfect_row=False))
    assert result["stages"][LEGACY_QA_SYNC]["state"] == MISSING
    assert result["next_action"] == SYNC_LEGACY_QA


def test_a_legacy_row_the_platform_did_not_write_is_protected_not_pending():
    """
    The ownership model, stated as a state. Reporting somebody else's row as
    "sync missing" puts a do-this instruction in front of an operator for a row
    the platform must never touch.
    """
    result = resolve(complete_rows(qa_writes=[], legacy_writes_state=[]),
                     observations=StubObservations(
                         recording_url="https://example.invalid/r"))
    assert result["stages"][LEGACY_QA_SYNC]["state"] == NOT_APPLICABLE
    assert result["stages"][LEGACY_QA_SYNC]["reason"] == \
        "LEGACY_ROW_NOT_CODED_OWNED"
    assert result["stages"][LEGACY_QA_SYNC]["coded_owned"] is False


def test_a_legacy_row_written_for_a_superseded_render_is_stale():
    rows = complete_rows(legacy_writes_state=[(
        WRITE_ID, "older-render", EVALUATION_ID, "WRITTEN", WRITER_VERSION,
        LEGACY_SESSION_ID, NOW)])
    result = resolve(rows)
    assert result["stages"][LEGACY_QA_SYNC]["state"] == STALE
    assert result["next_action"] == SYNC_LEGACY_QA


def test_a_perfect_lecture_without_a_legacy_row_asks_for_the_perfect_sync():
    result = resolve(complete_rows(perfect_writes=[], perfect_writes_state=[]),
                     observations=StubObservations(
                         recording_url="https://example.invalid/r",
                         perfect_row=False))
    assert result["stages"][PERFECT_SYNC]["state"] == MISSING
    assert result["next_action"] == SYNC_PERFECT


def test_a_lecture_that_is_not_perfect_needs_no_perfect_sync_or_excel():
    rows = complete_rows(
        perfect_results=[(DEFAULT_PERFECT_ELIGIBILITY_VERSION, False,
                          "NOT_ELIGIBLE_STATUS_NOT_ALL_MET",
                          "SOURCE_AVAILABLE_WITH_MEMBERS",
                          "NOT_ELIGIBLE_STATUS_NOT_ALL_MET", NOW)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)])
    result = resolve(rows)
    assert result["stages"][PERFECT_SYNC]["state"] == NOT_APPLICABLE
    assert result["stages"][EXCEL_SYNC]["state"] == NOT_APPLICABLE
    assert result["next_action"] == NOTHING_TO_DO


def test_a_missing_perfect_answer_asks_for_the_evaluation():
    result = resolve(complete_rows(perfect_results=[], keys=[],
                                   perfect_writes=[], perfect_writes_state=[]))
    assert result["stages"][PERFECT_ELIGIBILITY]["state"] == MISSING
    assert result["next_action"] == EVALUATE_PERFECT


def test_perfect_pending_attendance_waits_and_does_not_look_eligible():
    rows = complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)],
        perfect_results=[(DEFAULT_PERFECT_ELIGIBILITY_VERSION, False,
                          PENDING_ATTENDANCE_DATA, "SOURCE_MISSING",
                          ELIGIBLE, NOW)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)],
        perfect_writes=[], perfect_writes_state=[])
    result = resolve(rows)
    assert result["stages"][PERFECT_ELIGIBILITY]["state"] == WAITING
    # Attendance blocks first, which is the earlier and truer cause.
    assert result["next_action"] == WAIT_FOR_ATTENDANCE_SOURCE


# --- the observed-only stages ------------------------------------------------

def test_a_missing_recording_link_never_blocks_the_coded_pipeline():
    result = resolve(observations=StubObservations(recording_url=None,
                                                   excel_synced_at=NOW))
    assert result["stages"][RECORDING_LINK]["state"] == MISSING
    assert result["stages"][RECORDING_LINK]["owner"] == "LEGACY_RECORDING_BRANCH"
    assert result["next_action"] == WAIT_FOR_RECORDING
    # The distinction that matters: nothing for US to do.
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_a_pending_excel_sync_is_reported_and_owned_by_the_legacy_workflow():
    result = resolve(observations=StubObservations(
        recording_url="https://example.invalid/r", excel_synced_at=None))
    assert result["stages"][EXCEL_SYNC]["state"] == MISSING
    assert result["stages"][EXCEL_SYNC]["owner"] == "LEGACY_EXCEL_SYNC_WORKFLOW"
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_the_observed_stages_are_exactly_the_two_legacy_workflows_own():
    assert OBSERVED_ONLY_STAGES == {RECORDING_LINK, EXCEL_SYNC}


# --- the resume rule itself --------------------------------------------------

def test_the_earliest_incomplete_stage_wins_not_the_most_severe():
    """
    A lecture with a missing transcript AND a missing render has one real
    problem, and it is not the render.
    """
    rows = complete_rows(artifacts=[(0, 0, 0, None)], selection=[], documents=[],
                         speakers=[(0,)], rendered=[], qa_writes=[],
                         legacy_writes_state=[])
    result = resolve(rows)
    assert result["blocking_stage"] == TRANSCRIPT
    assert result["next_action"] == ACQUIRE_TRANSCRIPT


def test_the_stage_order_is_the_resume_order_and_has_no_duplicates():
    assert len(STAGE_ORDER) == len(set(STAGE_ORDER))
    assert STAGE_ORDER.index(ATTENDANCE) < STAGE_ORDER.index(ENGAGEMENT)
    assert STAGE_ORDER.index(ENGAGEMENT) < STAGE_ORDER.index(QA_EVALUATION)
    assert STAGE_ORDER.index(QA_EVALUATION) < STAGE_ORDER.index(QA_RENDER)
    assert STAGE_ORDER.index(QA_RENDER) < STAGE_ORDER.index(LEGACY_QA_SYNC)
    assert STAGE_ORDER.index(PERFECT_ELIGIBILITY) < STAGE_ORDER.index(PERFECT_SYNC)


def test_an_unknown_lecture_is_refused_rather_than_answered():
    connection = StubConnection({"lecture_state": []})
    resolver = PipelineStateResolver(legacy_observations=StubObservations())
    with pytest.raises(PlatformError):
        resolver.for_lecture(connection, LECTURE_ID)


def test_the_resolver_reports_the_versions_its_answer_depends_on():
    versions = resolve()["versions"]
    assert versions["selection_version"] == SELECTION_VERSION
    assert versions["speaker_inventory_version"] == SPEAKER_INVENTORY_VERSION
    assert versions["renderer_version"] == RENDERER_VERSION
    assert versions["writer_version"] == WRITER_VERSION
    assert versions["perfect_eligibility_version"] == \
        DEFAULT_PERFECT_ELIGIBILITY_VERSION


def test_the_platform_default_perfect_policy_is_still_the_attendance_aware_one():
    assert resolve()["perfect_policy_version"] == \
        "kbc_perfect_v2_attendance_required"


# --- 28..30. reconciliation ---------------------------------------------------

def _states(*results):
    return list(results)


def test_day_counts_are_a_partition_of_the_lectures():
    complete = resolve()
    waiting = resolve(complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)]))
    review = resolve(complete_rows(lecture_state=[(
        LECTURE_ID, "Example", "example", "Module", SESSION_DATE, NOW, False,
        "REVIEW", "RESOLVED", "MATCHED", "meeting-1", True, None)]))
    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, _states(complete, waiting, review))
    total = (report["complete_count"] + report["waiting_count"]
             + report["review_count"] + report["failed_count"]
             + report["in_progress_count"])
    assert total == report["canonical_lecture_count"] == 3
    assert report["complete_count"] == 1
    assert report["waiting_count"] == 1
    assert report["review_count"] == 1


def test_pending_attendance_is_reported_distinctly_from_review():
    waiting = resolve(complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)],
        perfect_results=[(DEFAULT_PERFECT_ELIGIBILITY_VERSION, False,
                          PENDING_ATTENDANCE_DATA, "SOURCE_MISSING", ELIGIBLE,
                          NOW)],
        keys=[(LEGACY_LECTURE_KEY, LEGACY_SESSION_ID, False)],
        perfect_writes=[], perfect_writes_state=[]))
    report = DayReconciliation(resolver=None).from_states(SESSION_DATE, [waiting])
    assert report["attendance_waiting_count"] == 1
    assert report["perfect_pending_attendance_count"] == 1
    assert report["review_count"] == 0
    assert report["failed_count"] == 0


def test_reason_codes_survive_into_the_report_verbatim():
    stale = resolve(complete_rows(rendered=[(
        RENDER_ID, "RENDERED", RENDERER_VERSION, "d" * 64, 11, 0, 0,
        "older-evaluation", LEGACY_SESSION_ID)]))
    report = DayReconciliation(resolver=None).from_states(SESSION_DATE, [stale])
    reasons = report["lectures"][0]["reason_codes"]
    assert reasons[QA_RENDER] == "RENDER_BELONGS_TO_SUPERSEDED_EVALUATION"


def test_the_report_counts_recording_and_excel_without_blocking_on_them():
    result = resolve(observations=StubObservations(recording_url=None,
                                                   excel_synced_at=None))
    report = DayReconciliation(resolver=None).from_states(SESSION_DATE, [result])
    assert report["recording_missing_count"] == 1
    assert report["excel_pending_count"] == 1
    # Still complete, because both belong to live legacy workflows.
    assert report["complete_count"] == 1


def test_bucket_is_severity_first_while_the_resume_rule_is_earliest_first():
    failed = resolve(complete_rows(documents=[(
        DOCUMENT_ID, PARSER_VERSION, "INVALID_WEBVTT", 0, SELECTION_ID, NOW,
        "b" * 64)]))
    assert bucket_for(failed) == "failed"
    assert failed["blocking_stage"] == CANONICAL_CUES


# --- operator gates do not block work that does not depend on them -----------

def test_perfect_eligibility_is_decided_before_either_legacy_write():
    """
    Phase 4A stepped the executable search OVER a legacy sync so a pending
    human action could not block Perfect eligibility. That was a workaround for
    an ordering mistake, and Phase 4B fixed the order instead: eligibility is
    derived from the frozen Phase 3B render and never needed the legacy row, so
    it simply comes first. Only PERFECT_SYNC depends on the QA row existing.
    """
    assert STAGE_ORDER.index(PERFECT_ELIGIBILITY) < STAGE_ORDER.index(LEGACY_QA_SYNC)
    assert STAGE_ORDER.index(LEGACY_QA_SYNC) < STAGE_ORDER.index(PERFECT_SYNC)

    result = resolve(complete_rows(qa_writes=[], legacy_writes_state=[],
                                   perfect_results=[], keys=[],
                                   perfect_writes=[], perfect_writes_state=[]),
                     observations=StubObservations(legacy_row=False,
                                                   perfect_row=False))
    assert result["stages"][LEGACY_QA_SYNC]["state"] == MISSING
    assert result["next_action"] == EVALUATE_PERFECT
    assert result["blocking_stage"] == PERFECT_ELIGIBILITY


def test_a_legacy_sync_is_now_the_scheduler_own_next_action():
    """
    The Phase 4B change, stated plainly. A rendered lecture whose Perfect
    answer exists and whose legacy row does not is no longer parked waiting for
    a human - the sync IS the next executable action.
    """
    result = resolve(complete_rows(qa_writes=[], legacy_writes_state=[]),
                     observations=StubObservations(legacy_row=False,
                                                   perfect_row=False))
    assert result["next_action"] == SYNC_LEGACY_QA
    assert result["next_executable_action"] == SYNC_LEGACY_QA
    assert result["operator_actions"] == []


def test_waiting_and_review_still_stop_the_executable_search():
    """
    The limit of the rule. A WAITING attendance source and an unreviewed
    discovery both make everything below them provisional, so the executable
    search stops there - unlike a pending legacy write, which invalidates
    nothing.
    """
    waiting = resolve(complete_rows(
        coverage=[(SNAPSHOT_ID, 0, 0, 0, ATTENDANCE_RESOLUTION_VERSION, 0)]))
    assert waiting["next_executable_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    assert waiting["executable_stage"] == ATTENDANCE

    review = resolve(complete_rows(lecture_state=[(
        LECTURE_ID, "Example", "example", "Module", SESSION_DATE, NOW, False,
        "REVIEW", "RESOLVED", "MATCHED", "meeting-1", True, None)]))
    assert review["next_executable_action"] == MANUAL_REVIEW_REQUIRED
    assert review["executable_stage"] == DISCOVERY


def test_a_repaired_legacy_row_still_counts_as_written():
    """
    Andrew's real case. Phase 3C2.3F repaired his Perfect row's
    `attended_count` from NULL to 7, leaving `write_status = 'UPDATED'`.
    Accepting only 'WRITTEN' reported him as ELIGIBLE with PERFECT_SYNC =
    NOT_APPLICABLE - a pairing that cannot be true - and would have invited an
    operator to write a row that already exists and is already correct.
    """
    rows = complete_rows(
        legacy_writes_state=[(WRITE_ID, RENDER_ID, EVALUATION_ID, "UPDATED",
                              WRITER_VERSION, LEGACY_SESSION_ID, NOW)],
        perfect_writes_state=[("UPDATED", PERFECT_WRITER_VERSION,
                               DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                               LEGACY_LECTURE_KEY, "result-1")])
    result = resolve(rows)
    assert result["stages"][LEGACY_QA_SYNC]["state"] == COMPLETE
    assert result["stages"][PERFECT_SYNC]["state"] == COMPLETE
    assert result["stages"][PERFECT_SYNC]["write_status"] == "UPDATED"
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_a_rolled_back_or_failed_write_is_not_an_owned_row():
    """The limit: these statuses are real absences, not repairs."""
    for status in ("NOOP", "ROLLED_BACK", "WRITE_VERIFICATION_FAILED"):
        rows = complete_rows(
            legacy_writes_state=[(WRITE_ID, RENDER_ID, EVALUATION_ID, status,
                                  WRITER_VERSION, LEGACY_SESSION_ID, NOW)])
        result = resolve(rows, observations=StubObservations(
            legacy_row=False, perfect_row=False))
        assert result["stages"][LEGACY_QA_SYNC]["state"] == MISSING, status


# --- Phase 4B: evidence revalidation is a free, distinct action --------------

def test_an_answer_rejected_by_an_older_evidence_rule_is_re_judged_not_re_bought():
    """
    The real 2026-09-18 shape. The model returned a complete checklist and two
    degenerate clips out of fifty-six; the old rule discarded the lot. The
    recovery is to re-judge the stored answer, which costs nothing - not to buy
    another generation and hope the dice land differently.
    """
    rows = complete_rows(evaluations=[(
        EVALUATION_ID, FINGERPRINT, "INVALID_EVIDENCE", None, 1, True, 11, 0, 0,
        "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW, ENGAGEMENT_ID,
        SNAPSHOT_ID, "c" * 64, None, DOCUMENT_ID, EVIDENCE_POLICY_V1)])
    result = resolve(rows)
    assert result["stages"][QA_EVALUATION]["state"] == STALE
    assert result["stages"][QA_EVALUATION]["reason"] == "EVIDENCE_POLICY_SUPERSEDED"
    assert result["next_action"] == REVALIDATE_EVIDENCE


def test_an_answer_already_judged_by_the_current_rule_is_not_re_judged():
    """Re-judging under the same rule cannot reach a different answer."""
    rows = complete_rows(evaluations=[(
        EVALUATION_ID, FINGERPRINT, "INVALID_EVIDENCE", None, 1, True, 11, 0, 0,
        "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW, ENGAGEMENT_ID,
        SNAPSHOT_ID, "c" * 64, None, DOCUMENT_ID, DEFAULT_EVIDENCE_POLICY)])
    result = resolve(rows)
    assert result["next_action"] == RUN_QA


def test_an_exhausted_budget_still_wins_over_a_superseded_policy():
    """
    Order matters. A lecture whose evidence rule moved gets the free re-judge
    first; but if that re-judge is not available, an exhausted contract must
    still reach a human rather than the provider.
    """
    rows = complete_rows(
        evaluations=[(EVALUATION_ID, FINGERPRINT, "REVIEW_REQUIRED",
                      "MAX_GENERATIONS_EXHAUSTED", 3, True, 0, 0, 0,
                      "strict_json_schema_v1", "canonical_cue_bounds_v1", NOW,
                      ENGAGEMENT_ID, SNAPSHOT_ID, None, None, DOCUMENT_ID,
                      EVIDENCE_POLICY_V1)],
        attempts=[(FINGERPRINT, 3, 3, NOW, NOW,
                   ["FAILED", "FAILED", "FAILED"], ["X"] * 3, [""] * 3, [""] * 3, None)])
    result = resolve(rows)
    assert result["next_action"] == MANUAL_REVIEW_REQUIRED


# --- Phase 4C1: the suppressed duplicate, seen by the rest of the platform ---

SUPPRESSION = {
    "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
    "duplicate_resolution_reason": SUPPRESSED_REASON,
    "winner_lecture_id": SIBLING_ID, "winner_meeting_id": "meeting-1",
    "suppressed_lecture_id": LECTURE_ID,
    "resolved_at": "2026-09-19T08:00:00+00:00"}


def suppressed():
    """The registry row of an occurrence that has already been retired."""
    return complete_rows(lecture_state=[(
        LECTURE_ID, "Example Lecture  ", "example lecture", "Example Module",
        SESSION_DATE, NOW, False, "REVIEW", "ONLINE_MEETING_NOT_FOUND",
        "MATCHED", None, False, SUPPRESSION)])


def test_4_a_suppressed_duplicate_has_nothing_to_do():
    result = resolve(suppressed())
    assert result["next_action"] == NOTHING_TO_DO
    assert result["next_executable_action"] == NOTHING_TO_DO
    assert result["blocking_stage"] is None
    assert result["is_suppressed_duplicate"] is True


def test_3_a_suppressed_duplicate_is_not_downstream_ready():
    assert resolve(suppressed())["downstream_ready"] is False


def test_every_stage_of_a_suppressed_duplicate_is_settled():
    result = resolve(suppressed())
    assert states_of(result) == {stage: NOT_APPLICABLE for stage in STAGE_ORDER}
    assert result["is_complete"] is True
    assert result["requires_review"] is False
    assert all(item["reason"] == SUPPRESSED_REASON
               for item in result["stages"].values())


def test_5_6_7_a_suppressed_duplicate_is_never_even_ASKED_about_downstream_work():
    """
    Points 5, 6 and 7 in one assertion, and the strongest form of them.

    The resolver does not read the transcript, QA or writer tables at all for a
    suppressed row - so "it cannot enter downstream processing" is structural,
    not a rule that fifteen separate stages have to remember to apply.
    """
    connection = StubConnection(suppressed())
    PipelineStateResolver(legacy_observations=StubObservations()).for_lecture(
        connection, LECTURE_ID)
    assert connection.seen == ["lecture_state"]


def test_the_suppressed_row_still_points_at_the_winner():
    resolution = resolve(suppressed())["duplicate_resolution"]
    assert resolution["winner_lecture_id"] == SIBLING_ID
    assert resolution["requires_manual_review"] is False
    assert resolution["resolved_at"] == "2026-09-19T08:00:00+00:00"


def test_15_the_day_report_counts_a_suppression_as_neither_failure_nor_review():
    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, _states(resolve(), resolve(suppressed())))
    assert report["suppressed_duplicate_count"] == 1
    assert report["failed_count"] == 0
    assert report["review_count"] == 0
    assert report["complete_count"] == 1
    assert report["canonical_lecture_count"] == 2
    # One calendar event, one actual lecture.
    assert report["business_lecture_count"] == 1


def test_15b_the_suppressed_duplicate_stays_visible_in_the_day_report():
    """
    Not counted, but never hidden: the source calendar really did contain the
    extra event, and a report that omitted it would disagree with the registry.
    """
    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, _states(resolve(), resolve(suppressed())))
    rows = {row["bucket"] for row in report["lectures"]}
    assert "suppressed_duplicate" in rows
    row = next(item for item in report["lectures"]
               if item["is_suppressed_duplicate"])
    assert row["duplicate_winner_lecture_id"] == SIBLING_ID


def test_15c_the_buckets_still_partition_the_day():
    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, _states(resolve(), resolve(suppressed())))
    total = (report["complete_count"] + report["waiting_count"]
             + report["review_count"] + report["failed_count"]
             + report["in_progress_count"]
             + report["suppressed_duplicate_count"])
    assert total == report["canonical_lecture_count"]


def test_a_suppressed_duplicate_does_not_inflate_the_derived_counters():
    """
    Its LEGACY_QA_SYNC stage is NOT_APPLICABLE, which for a real lecture means
    "an n8n-owned legacy row". Counting it there would invent a legacy row that
    does not exist.
    """
    report = DayReconciliation(resolver=None).from_states(
        SESSION_DATE, _states(resolve(), resolve(suppressed())))
    assert report["legacy_not_coded_owned_count"] == 0
    assert report["by_next_action"] == {NOTHING_TO_DO: 1}
