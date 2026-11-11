"""
QA Core RC3: the legacy compatibility contract of the September Backfill.

`public.qa_doctors_sessions` is NOT dormant history. Other company projects
read it - the positive-clips producer keys its whole media pipeline on
`session_id` + `date` + `subject` + `trainer` + `recording_*`, and the Django
dashboard reads it through an unmanaged model. So a lecture the coded platform
recovers is only actually recovered when it exists in BOTH places:

    public.lecture_sessions + coded pipeline state   (canonical, authoritative)
    public.qa_doctors_sessions                       (one-way compatibility projection)

These tests fix that contract in place. They assert the projection happens
through the EXISTING `LEGACY_QA_SYNC` stage and the existing guarded writer -
never a second SQL path - and that every ownership refusal the writer already
makes survives being reached from a backfill.

The defect they were written for is the first test below: the orchestrator's
pass cap was 8 while the executable stage chain is 13 long, so a lecture
discovered from zero stopped at QA_RENDER and never produced its compatibility
row. A nightly window hid it - yesterday's lectures are already most of the way
down the chain - and a backfill, which starts every lecture at zero and visits
each business date exactly once, did not.
"""
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from app.orchestration.orchestrator import DEFAULT_MAX_PASSES
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    BUILD_CANONICAL_CUES,
    CALCULATE_ENGAGEMENT,
    EVALUATE_PERFECT,
    EXECUTABLE_STAGES,
    LEGACY_QA_SYNC,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    PERFECT_SYNC,
    RENDER_QA,
    RESOLVE_ATTENDANCE,
    RESOLVE_SPEAKERS,
    RUN_QA,
    SELECT_TRANSCRIPT,
    STAGE_ORDER,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
    WAIT_FOR_ATTENDANCE_SOURCE,
)
from app.orchestration.sync_safety import classify_plan, classify_qa
from app.writer.mapping import LEGACY_SESSION_KEY, SESSION_COLUMNS
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PRODUCTION_NEW_ONLY,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
    plan_decision,
    plan_perfect_decision,
)
from app.transcripts.identity import canonical_key
from app.writer.legacy_identity import Candidate, LegacyOccurrenceGuard
from app.writer.service import LegacyQaWriter

from test_legacy_writer import items, rendered
from test_orchestration import RecordingRunner, ScriptedLecture, orchestrator_for


TARGET = date(2026, 9, 17)

# What a lecture discovered from nothing actually has to walk. Day-scoped
# services are shared across the day, but each still costs the lecture one
# pass, because a lecture advances by at most one stage per pass.
FRESH_LECTURE_CHAIN = (
    ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT, BUILD_CANONICAL_CUES, RESOLVE_SPEAKERS,
    RESOLVE_ATTENDANCE, CALCULATE_ENGAGEMENT, RUN_QA, RENDER_QA, EVALUATE_PERFECT,
    SYNC_LEGACY_QA, SYNC_PERFECT,
)
DAY_SCOPED = (ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT, BUILD_CANONICAL_CUES,
              RESOLVE_SPEAKERS)


# --- an in-memory stand-in for the two legacy targets -------------------------

class FakeConnection:
    """Enough of a psycopg connection for the writer's savepoint."""

    @contextmanager
    def transaction(self):
        yield self


class FakeLegacyTarget:
    """
    `public.qa_doctors_sessions` as a dict keyed on `session_id`.

    Deliberately keyed exactly as the real primary key is, so "did a second run
    create a duplicate row?" is a question this fake can actually answer: an
    upsert on the same key replaces, and anything else shows up as a second
    entry.
    """

    def __init__(self, sessions=None, checklists=None):
        self.sessions = dict(sessions or {})
        self.checklists = dict(checklists or {})
        self.writes = []

    def load_session(self, connection, session_id):
        row = self.sessions.get(session_id)
        return dict(row) if row else None

    def load_checklist(self, connection, session_id):
        return [dict(row) for row in self.checklists.get(session_id, [])]

    def write_session(self, connection, row, *, allow_write):
        assert allow_write, "the writer must never write without a write-enabled mode"
        session_id = row[LEGACY_SESSION_KEY]
        # The real statement updates only the 21 non-key mapped columns, so a
        # foreign-owned column already on the row survives. Mirrored here.
        merged = dict(self.sessions.get(session_id, {}))
        merged.update({name: row[name] for name in SESSION_COLUMNS})
        self.sessions[session_id] = merged
        self.writes.append(session_id)

    def write_checklist(self, connection, rows, *, allow_write):
        assert allow_write
        self.checklists[rows[0]["session_id"]] = [dict(row) for row in rows]


class FakeOwnership:
    """`public.lecture_qa_legacy_writes`, keyed the way the real unique key is."""

    def __init__(self, owned=None):
        self.owned = dict(owned or {})

    def find(self, connection, legacy_session_id, writer_version):
        row = self.owned.get(legacy_session_id)
        if row is None or row.get("writer_version", writer_version) != writer_version:
            return None
        return {"write_id": row.get("write_id", uuid.UUID(int=9)),
                "lecture_id": row.get("lecture_id"),
                "source_fingerprint": row["source_fingerprint"],
                "write_status": "WRITTEN", "write_mode": PRODUCTION_NEW_ONLY,
                "post_write_digest": row.get("post_write_digest")}

    def record(self, connection, entry):
        created = entry["legacy_session_id"] not in self.owned
        self.owned[entry["legacy_session_id"]] = {
            "write_id": entry["write_id"], "lecture_id": entry["lecture_id"],
            "source_fingerprint": entry["source_fingerprint"],
            "writer_version": entry["writer_version"]}
        return {"write_id": entry["write_id"], "created": created}


class FakeOccurrenceRepository:
    """
    The same-occurrence guard's facts, read from the SAME in-memory legacy
    table and ownership ledger the writer writes to - so the real guard logic
    runs against exactly the rows the test built, and a row the writer inserts
    is immediately visible to the next plan.

    `lectures` maps lecture_id -> {"meeting_id", "dates", "transcripts"}.
    """

    def __init__(self, legacy, ownership, lectures=None):
        self.legacy = legacy
        self.ownership = ownership
        self.lectures = lectures or {}

    def load(self, connection, *, lecture_id, session_id, writer_version):
        context = self.lectures.get(str(lecture_id), {})
        dates = {str(d) for d in context.get("dates", ())}
        own = {canonical_key(t) for t in context.get("transcripts", ())}
        own.add(canonical_key(session_id))
        candidates = []
        for sid, row in self.legacy.sessions.items():
            if sid == session_id:
                continue
            owner = self.ownership.owned.get(sid, {}).get("lecture_id")
            on_date = str(row.get("date")) in dates
            if on_date or (owner is not None and str(owner) == str(lecture_id)):
                candidates.append(Candidate(sid, row.get("meeting_id"),
                                            row.get("date"),
                                            str(owner) if owner else None))
        return {"meeting_id": context.get("meeting_id"),
                "own_transcript_keys": own, "candidates": candidates}


class FakePayloads:
    def __init__(self, session, checklist):
        self.session = session
        self.checklist = checklist

    def load_sessions(self, connection, target_date, renderer_version):
        return [dict(self.session)]

    def load_items(self, connection, rendered_session_id):
        return [dict(row) for row in self.checklist]


def writer_for(*, legacy, ownership, payload=None, mode=PRODUCTION_NEW_ONLY,
               allow_update_existing=False, lectures=None):
    payload = payload or rendered()
    context = lectures or {str(payload["lecture_id"]): {
        "meeting_id": payload["meeting_id"],
        "dates": [payload["legacy_date"], payload["canonical_session_date"]],
        "transcripts": [payload["session_id"]]}}
    return LegacyQaWriter(
        payload_repository=FakePayloads(payload, items(payload["session_id"])),
        legacy_repository=legacy, ownership_repository=ownership, mode=mode,
        lecture_ids=[str(payload["lecture_id"])], confirmed=True,
        allow_update_existing=allow_update_existing,
        # The derived Perfect target has its own lifecycle and its own tests.
        perfect_planner=None,
        occurrence_guard=LegacyOccurrenceGuard(
            FakeOccurrenceRepository(legacy, ownership, context)))


def sync_one(writer):
    """One lecture through the writer, the way the runner calls it."""
    return writer.plan_day(FakeConnection(), TARGET)["lectures"][0]


# --- 1. discovered from nothing -> canonical AND compatibility row ------------

def test_a_lecture_discovered_from_nothing_reaches_the_legacy_sync_in_one_run():
    """
    The regression that made RC3 necessary.

    A September lecture in neither table has to walk the whole executable
    chain. If the run ends before `SYNC_LEGACY_QA`, the canonical platform has
    the lecture and `qa_doctors_sessions` does not - and because backfill
    visits each business date exactly once, nothing comes back for it.
    """
    lecture = ScriptedLecture("new-september-lecture", FRESH_LECTURE_CHAIN)
    runner = RecordingRunner([lecture], day_actions=DAY_SCOPED)
    summary = orchestrator_for(
        [lecture], runner=runner,
        max_passes=DEFAULT_MAX_PASSES).run_window(None, TARGET)

    performed = [action for action, _ in runner.calls]
    assert SYNC_LEGACY_QA in performed, (
        "the compatibility projection never ran; qa_doctors_sessions would "
        f"have no row for this lecture. Performed: {performed}")
    assert performed.index(RENDER_QA) < performed.index(SYNC_LEGACY_QA)
    assert summary["errors"] == []
    assert summary["lectures"][0]["final_action"] == NOTHING_TO_DO


def test_the_pass_cap_can_never_be_shorter_than_the_stage_chain_again():
    """
    A lecture advances by at most one stage per pass, so a cap below the chain
    length is a silent budget rather than a defect detector. Derived from the
    stage list, so a new stage cannot reintroduce the gap.
    """
    assert DEFAULT_MAX_PASSES > len(EXECUTABLE_STAGES)


def test_a_fresh_lecture_with_no_legacy_row_is_an_insert_the_scheduler_may_do():
    decision = plan_decision(
        mode=PRODUCTION_NEW_ONLY, render_status="RENDERED", qa_status="COMPLETED",
        payload_valid=True, target_exists=False, coded_owned=False,
        fingerprint_matches=False)
    assert decision == WOULD_INSERT
    assert classify_qa(decision)["action"] == "WRITE"


def test_the_backfill_reaches_the_legacy_tables_only_through_the_orchestrator():
    """
    No second writer. Backfill is a loop over `run_window`, so the only way it
    can touch `qa_doctors_sessions` is the stage every other caller uses.
    """
    import inspect

    from app.orchestration import backfill

    source = inspect.getsource(backfill)
    assert "qa_doctors_sessions" not in source
    assert "qa_doctors_checklist_items" not in source
    assert "run_window" in source


# --- 2. canonical exists, legacy row missing -> safe sync creates it ----------

def test_a_canonical_lecture_whose_legacy_row_is_missing_gets_one_created():
    legacy, ownership = FakeLegacyTarget(), FakeOwnership()
    result = sync_one(writer_for(legacy=legacy, ownership=ownership))

    assert result["decision"] == WOULD_INSERT
    assert result["write_status"] == "WRITTEN"
    assert list(legacy.sessions) == ["TRANSCRIPT-1"]
    assert ownership.owned["TRANSCRIPT-1"]["source_fingerprint"] == "a" * 64


def test_the_created_row_carries_the_fields_the_legacy_consumers_read():
    """
    The positive-clips producer and the dashboard both key on these. A row that
    exists but cannot be joined or dated is not compatibility.
    """
    legacy = FakeLegacyTarget()
    sync_one(writer_for(legacy=legacy, ownership=FakeOwnership()))
    row = legacy.sessions["TRANSCRIPT-1"]

    assert row["session_id"] == "TRANSCRIPT-1"
    assert row["date"] == "2026-09-04"
    assert row["subject"] == "Test Lecture"
    assert row["trainer"] == "Morgan Trainerfield"
    assert row["meeting_id"] == "MEETING-1"
    assert row["overall_judgement"] == "Solid."
    assert row["Engagement"] is not None
    # TEXT column holding the JavaScript booleans n8n wrote.
    assert row["cancelled_session"] in ("true", "false")


def test_the_write_never_touches_a_column_another_workflow_owns():
    """
    `clips_*`, `recording_*`, `transcript_*` and `cancellation_reason` belong to
    the positive-clips and recording workflows. A compatibility write updates
    the 22 mapped columns; it does not get to clear somebody else's fields.
    """
    foreign = {"positive_clips": [{"start": "0:01:00.000"}],
               "clips_analysis_completeness": "positive_clips_v5_final",
               "clips_media_origin": "history",
               "recording_drive_id": "DRIVE-1", "recording_item_id": "ITEM-1",
               "recording_url": "https://example.invalid/recording",
               "cancellation_reason": "set by another workflow"}
    legacy = FakeLegacyTarget(sessions={"TRANSCRIPT-1": dict(foreign)})
    ownership = FakeOwnership(owned={"TRANSCRIPT-1": {"source_fingerprint": "b" * 64}})

    result = sync_one(writer_for(legacy=legacy, ownership=ownership))
    assert result["decision"] == WOULD_UPDATE
    row = legacy.sessions["TRANSCRIPT-1"]
    for column, value in foreign.items():
        assert column not in SESSION_COLUMNS
        assert row[column] == value, f"{column} was overwritten or cleared"


# --- 3. an owned row -> safe update, no duplicate ----------------------------

def test_an_owned_row_with_an_unchanged_source_is_a_noop_not_a_second_row():
    legacy = FakeLegacyTarget(sessions={"TRANSCRIPT-1": {"session_id": "TRANSCRIPT-1"}})
    ownership = FakeOwnership(owned={"TRANSCRIPT-1": {"source_fingerprint": "a" * 64}})
    result = sync_one(writer_for(legacy=legacy, ownership=ownership))

    assert result["decision"] == WOULD_SKIP_IDENTICAL
    assert legacy.writes == []
    assert len(legacy.sessions) == 1


def test_an_owned_row_whose_source_changed_updates_in_place_on_the_same_key():
    legacy = FakeLegacyTarget(sessions={"TRANSCRIPT-1": {"session_id": "TRANSCRIPT-1",
                                                         "subject": "Stale Subject"}})
    ownership = FakeOwnership(owned={"TRANSCRIPT-1": {"source_fingerprint": "b" * 64}})
    result = sync_one(writer_for(legacy=legacy, ownership=ownership))

    assert result["decision"] == WOULD_UPDATE
    assert list(legacy.sessions) == ["TRANSCRIPT-1"]
    assert legacy.sessions["TRANSCRIPT-1"]["subject"] == "Test Lecture"


def test_an_update_is_never_something_the_scheduler_decides_alone():
    """
    The safe set is insert-or-nothing. Rewriting a published row is a judgement,
    and the backfill inherits that refusal unchanged.
    """
    verdict = classify_plan({"decision": WOULD_UPDATE})
    assert verdict["may_write"] is False
    assert verdict["requires_manual_review"] is True
    assert verdict["reason_codes"] == ["LEGACY_ROW_WOULD_BE_UPDATED"]


# --- 4. a foreign / unowned row is protected ---------------------------------

@pytest.mark.parametrize("mode", [DRY_RUN, CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY,
                                  EXPLICIT_BACKFILL])
def test_a_legacy_row_the_platform_did_not_write_is_protected_in_every_mode(mode):
    assert plan_decision(
        mode=mode, render_status="RENDERED", qa_status="COMPLETED",
        payload_valid=True, target_exists=True, coded_owned=False,
        fingerprint_matches=False,
        allow_update_existing=True) == PROTECTED_EXISTING_LEGACY_ROW


def test_a_foreign_row_reached_from_a_backfill_is_left_exactly_as_it_was():
    original = {"session_id": "TRANSCRIPT-1", "subject": "Written by n8n",
                "trainer": "Somebody Else", "overall_judgement": "not ours"}
    legacy = FakeLegacyTarget(sessions={"TRANSCRIPT-1": dict(original)})
    # No ownership record: this row is not ours.
    result = sync_one(writer_for(legacy=legacy, ownership=FakeOwnership(),
                                 mode=EXPLICIT_BACKFILL,
                                 allow_update_existing=True))

    assert result["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert legacy.writes == []
    assert legacy.sessions["TRANSCRIPT-1"] == original
    assert classify_qa(result["decision"])["reason_code"] == "LEGACY_ROW_NOT_CODED_OWNED"


# --- 5. rerunning the backfill ------------------------------------------------

def test_running_the_same_backfill_twice_creates_no_second_legacy_row():
    legacy, ownership = FakeLegacyTarget(), FakeOwnership()

    first = sync_one(writer_for(legacy=legacy, ownership=ownership))
    assert first["decision"] == WOULD_INSERT
    assert first["write_status"] == "WRITTEN"

    second = sync_one(writer_for(legacy=legacy, ownership=ownership))
    assert second["decision"] == WOULD_SKIP_IDENTICAL
    assert classify_plan({"decision": second["decision"]})["may_write"] is False

    assert list(legacy.sessions) == ["TRANSCRIPT-1"]
    assert len(legacy.checklists["TRANSCRIPT-1"]) == 11
    assert legacy.writes == ["TRANSCRIPT-1"], "the second run wrote again"
    assert len(ownership.owned) == 1


def test_a_settled_lecture_is_offered_no_work_on_a_second_backfill_pass():
    lecture = ScriptedLecture("already-recovered", [])
    runner = RecordingRunner([lecture], day_actions=DAY_SCOPED)
    summary = orchestrator_for(
        [lecture], runner=runner,
        max_passes=DEFAULT_MAX_PASSES).run_window(None, TARGET)

    assert runner.calls == []
    assert summary["idempotent"] is True
    assert summary["lectures"][0]["final_action"] == NOTHING_TO_DO


# --- 6. attendance WAIT -------------------------------------------------------

def test_a_lecture_waiting_on_attendance_never_reaches_the_compatibility_sync():
    """
    Scripted: the resolver has NOTHING left for this lecture but the attendance
    wait. The orchestrator must not invent work from it. (Since
    attendance-optional QA the real resolver only reaches this state once QA,
    render and the legacy sync are already settled - see
    test_attendance_optional_qa.py - so this is "no extra work", not "no QA".)
    """
    lecture = ScriptedLecture("waiting", [WAIT_FOR_ATTENDANCE_SOURCE], waiting=True,
                              attendance_state="WAITING")
    runner = RecordingRunner([lecture], day_actions=DAY_SCOPED)
    orchestrator_for([lecture], runner=runner,
                     max_passes=DEFAULT_MAX_PASSES).run_window(None, TARGET)

    performed = [action for action, _ in runner.calls]
    assert SYNC_LEGACY_QA not in performed
    assert SYNC_PERFECT not in performed
    assert RUN_QA not in performed


def test_a_pending_attendance_source_never_becomes_a_perfect_lecture():
    """
    `PERFECT_PENDING_ATTENDANCE_DATA` is non-final on purpose: it supersedes
    nothing, deletes nothing, and writes nothing while the source is silent.
    """
    decision = plan_perfect_decision(
        mode=PRODUCTION_NEW_ONLY, render_status="RENDERED", qa_status="COMPLETED",
        payload_valid=True, is_perfect=True, target_exists=False,
        coded_owned=False, foreign_session_on_key=False, fingerprint_matches=False,
        attendance_pending=True)
    assert decision == PERFECT_PENDING_ATTENDANCE_DATA

    verdict = classify_plan({"decision": WOULD_INSERT,
                             "perfect_lecture": {"perfect_decision": decision}})
    assert verdict["may_write_perfect"] is False
    assert verdict["perfect"]["action"] == "WAITING"
    # The QA compatibility row is still correct and still allowed: attendance
    # gates the DERIVED Perfect output, not the session projection.
    assert verdict["may_write_qa"] is True


def test_a_lecture_that_is_not_perfect_still_gets_its_compatibility_row():
    verdict = classify_plan({"decision": WOULD_INSERT,
                             "perfect_lecture": {
                                 "perfect_decision": PERFECT_NOT_ELIGIBLE}})
    assert verdict["may_write_qa"] is True
    assert verdict["may_write_perfect"] is False
    assert verdict["requires_manual_review"] is False


# --- 7. a suppressed duplicate ------------------------------------------------

def test_a_suppressed_duplicate_never_produces_a_legacy_row():
    from app.orchestration.state import PipelineStateResolver

    state = PipelineStateResolver()._suppressed_state({
        "lecture_id": "loser", "subject": "Duplicate Booking",
        "session_date": TARGET.isoformat(),
        "duplicate_suppression": {"winner_lecture_id": "winner"}})

    assert state["stages"][LEGACY_QA_SYNC]["state"] == NOT_APPLICABLE
    assert state["stages"][PERFECT_SYNC]["state"] == NOT_APPLICABLE
    assert state["next_executable_action"] == NOTHING_TO_DO
    assert state["is_complete"] is True
    # Every stage, not just the two legacy ones: the occurrence is retired.
    assert {item["state"] for item in state["stages"].values()} == {NOT_APPLICABLE}
    assert set(state["stages"]) == set(STAGE_ORDER)


def test_a_suppressed_duplicate_is_offered_no_action_by_the_orchestrator():
    lecture = ScriptedLecture("suppressed", [])
    runner = RecordingRunner([lecture], day_actions=DAY_SCOPED)
    orchestrator_for([lecture], runner=runner,
                     max_passes=DEFAULT_MAX_PASSES).run_window(None, TARGET)
    assert runner.calls == []


# --- 8. multipart identity ----------------------------------------------------

def _candidate(transcript_id, start, minutes, call_id="CALL-1"):
    from app.transcripts.selection import CandidateArtifact

    return CandidateArtifact(
        artifact_id=uuid.uuid5(uuid.NAMESPACE_URL, transcript_id),
        provider_transcript_id=transcript_id, provider_created_at=start,
        provider_end_at=start + timedelta(minutes=minutes),
        provider_call_id=call_id, meeting_id="MEETING-1")


def test_a_multipart_lecture_keeps_the_primary_part_as_its_legacy_identity():
    """
    Legacy `session_id` is the PRIMARY provider transcript id, not a synthetic
    key and not the last part. Every downstream consumer - clip keys, media job
    keys, clip filenames - is derived from it, so a multipart lecture that
    picked a different part would silently orphan its own clips.
    """
    from app.transcripts.selection import SELECTED, select_transcript_parts

    scheduled_start = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    scheduled_end = scheduled_start + timedelta(hours=2)
    parts = [_candidate("PART-A-PRIMARY", scheduled_start, 100),
             _candidate("PART-B-TAIL", scheduled_start + timedelta(minutes=105), 10)]

    result = select_transcript_parts(
        parts, scheduled_start=scheduled_start, scheduled_end=scheduled_end,
        target_date=date(2026, 9, 17), meeting_id="MEETING-1")

    assert result.status == SELECTED
    assert result.diagnostics["selected_parts_count"] == 2
    assert result.primary.candidate.provider_transcript_id == "PART-A-PRIMARY"
    assert result.diagnostics["primary_transcript_id"] == "PART-A-PRIMARY"


def test_the_renderer_takes_the_legacy_session_id_from_the_primary_part_only():
    import inspect

    from app.rendering import service

    source = inspect.getsource(service)
    assert '"session_id": evaluation["primary_provider_transcript_id"]' in source


def test_the_checklist_key_is_derived_from_that_same_session_identity():
    from app.rendering.compatibility import session_id_match

    rows = items("PART-A-PRIMARY")
    assert session_id_match("PART-A-PRIMARY", 1) == "PART-A-PRIMARY_1"
    assert {row["session_id_match"] for row in rows} == {
        f"PART-A-PRIMARY_{order}" for order in range(1, 12)}


def test_the_writer_keys_the_compatibility_row_on_that_session_identity():
    payload = rendered(session_id="PART-A-PRIMARY")
    legacy, ownership = FakeLegacyTarget(), FakeOwnership()
    result = sync_one(writer_for(legacy=legacy, ownership=ownership, payload=payload))

    assert LEGACY_SESSION_KEY == "session_id"
    assert result["legacy_session_id"] == "PART-A-PRIMARY"
    assert list(legacy.sessions) == ["PART-A-PRIMARY"]
    assert ownership.owned["PART-A-PRIMARY"]["source_fingerprint"] == payload[
        "source_fingerprint"]


# --- REVIEW_REQUIRED ----------------------------------------------------------

@pytest.mark.parametrize("qa_status", ["REVIEW_REQUIRED", "INVALID_EVIDENCE",
                                       "FAILED", "PENDING"])
def test_a_lecture_that_needs_review_never_projects_a_compatibility_row(qa_status):
    """
    Only a finalized evaluation reaches the legacy table. `READY_QA_STATUSES`
    is `COMPLETED` and `NON_DELIVERED` and nothing else, so a lecture a human
    still has to look at cannot publish itself to the downstream consumers.
    """
    decision = plan_decision(
        mode=PRODUCTION_NEW_ONLY, render_status="RENDERED", qa_status=qa_status,
        payload_valid=True, target_exists=False, coded_owned=False,
        fingerprint_matches=False)
    assert decision == "BLOCKED_NOT_READY"
    verdict = classify_qa(decision)
    assert verdict["auto_approved"] is False
    assert verdict["reason_code"] == "SOURCE_NOT_READY"


def test_a_review_required_lecture_never_writes_even_from_the_writer_itself():
    legacy, ownership = FakeLegacyTarget(), FakeOwnership()
    payload = rendered(qa_status="REVIEW_REQUIRED")
    result = sync_one(writer_for(legacy=legacy, ownership=ownership, payload=payload))

    assert result["decision"] == "BLOCKED_NOT_READY"
    assert legacy.sessions == {}
    assert ownership.owned == {}


def test_a_cancelled_but_finalized_lecture_is_still_projected():
    """
    `NON_DELIVERED` is a real legacy outcome with a real legacy row - it is how
    a cancelled session appears to the downstream consumers, not an absence.
    """
    assert plan_decision(
        mode=PRODUCTION_NEW_ONLY, render_status="RENDERED_NON_DELIVERED",
        qa_status="NON_DELIVERED", payload_valid=True, target_exists=False,
        coded_owned=False, fingerprint_matches=False) == WOULD_INSERT
