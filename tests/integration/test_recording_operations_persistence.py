"""
Recording Links in the Operations console, against the isolated test database.

What these prove with real SQL, a real resolver and the real repositories:

  * an earlier unfinished stage keeps the shared orchestrator away from
    LINK_RECORDING, and the console says why;
  * the Historical Backfill live preview reads, and only reads: no statement
    that writes, no sharing link, no change to any row;
  * an EXECUTE snapshot tells "written by this run" from "already linked",
    never lets an ambiguous match through, and its totals reconcile;
  * the per-run snapshot (migration 022) round-trips without carrying a URL;
  * the lecture detail's Recording Link block is safe to show.

Microsoft Graph is always a stub.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.backfill import MODE_PREVIEW, BackfillRepository
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.recording_links import (
    PERFECT_RECORDING_COLUMNS,
    SESSION_RECORDING_COLUMNS,
    RecordingLinkRepository,
)
from app.orchestration.locks import NullCycleLock, NullLockManager
from app.orchestration.operations import OperationsService
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    AUTOMATABLE_ACTIONS,
    LINK_RECORDING,
    RECORDING_LINK,
    RUN_TYPE_BACKFILL,
    TRANSCRIPT,
)
from app.orchestration.state import PipelineStateResolver
from app.recordings import coverage as c
from app.recordings import models as m
from app.recordings.coverage import BackfillRecordingReport, execute_items, summarize
from app.recordings.preview import build_preview
from tests.integration.seeding import DAY
from tests.integration.test_recording_links_persistence import (  # noqa: F401 - fixture
    GRAPH_CREATED,
    ORG_LINK,
    checklist,
    db,
    file_item,
    graph_for,
    row_json,
    runner_with,
    seed_world,
    service_for,
    without,
)
from tests.unit.recording_graph_fakes import FakeGraph  # noqa: F401

SETTINGS = Settings(database_url="", aptem_database_url="", graph_tenant_id="t",
                    graph_client_id="c", graph_client_secret="s", graph_scope="s",
                    graph_base_url="https://graph.microsoft.com/v1.0",
                    calendar_user_upn="calendar@example.invalid",
                    recording_link_mode="write")

WRITES = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP)\b",
                    re.IGNORECASE)


class StubPreflight:
    """The legacy QA path is disabled; a real run would ask n8n (GET only)."""

    def check(self):
        return {"status": "LEGACY_QA_DISABLED", "legacy_qa_node_disabled": True,
                "reason": "LEGACY_QA_DISABLED", "writes_permitted": True,
                "read_only": True, "http_method": "GET", "n8n_modified": False}


def resolver(mode="write"):
    return PipelineStateResolver(legacy_observations=LegacyObservationRepository(),
                                 recording_link_mode=mode)


class ReadOnly:
    """The test connection, refusing any statement that could write."""

    def __init__(self, connection):
        self._connection = connection
        self.statements = 0

    def execute(self, sql, params=None, **kwargs):
        text = sql if isinstance(sql, str) else str(sql)
        assert not WRITES.match(text) and not re.search(
            r"\b(UPDATE|INSERT INTO|DELETE FROM)\b", text, re.IGNORECASE), text[:120]
        self.statements += 1
        return self._connection.execute(sql, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def world_fingerprint(db, session_id, key):
    return (row_json(db, "qa_doctors_sessions", "session_id", session_id),
            row_json(db, "qa_perfect_lectures", "lecture_key", key),
            checklist(db, session_id),
            db.execute("SELECT count(*) FROM public.lecture_recording_links").fetchone()[0],
            db.execute("SELECT count(*) FROM public.lecture_pipeline_runs").fetchone()[0])


def live_graph(lecture, session_id, *, items=None, recordings=None):
    """
    The live routes a preview reads: the organizer's recordings listing, the
    organizer's OneDrive Recordings folder (which holds the file), and the
    meeting channel's Recordings folder (empty).
    """
    from urllib.parse import quote

    from app.transcripts.identity import canonical_transcript_identity

    thread = canonical_transcript_identity(session_id).thread_id
    graph = graph_for(session_id, recordings=recordings)
    graph.collections = {
        "/users/organizer/drive/": items if items is not None else [file_item(lecture)],
        "/users/organizer/joinedTeams": [{"id": "team-1"}],
        "/teams/team-1/allChannels": [{"id": thread}],
        "/drives/channel-drive/": [],
        **graph.collections}
    graph.json = {f"/teams/team-1/channels/{quote(thread, safe='')}/filesFolder": {
        "id": "folder-1", "parentReference": {"driveId": "channel-drive"}}}
    return graph


# ---------------------------------------------------------------------------
# 8. an earlier unfinished stage blocks RECORDING_LINK
# ---------------------------------------------------------------------------

class RefusingRunner:
    """Refuses the earlier stage the way a real run without Graph would."""

    def __init__(self):
        self.calls = []

    def scope_of(self, action):
        return "LECTURE"

    def can_run(self, action):
        return action in AUTOMATABLE_ACTIONS and action != ACQUIRE_TRANSCRIPT

    def refusal_reason(self, action):
        return "GRAPH_CALLS_DISABLED_FOR_THIS_RUN"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append(action)
        raise AssertionError(f"{action} must not run while TRANSCRIPT is unfinished")


def test_an_earlier_unfinished_stage_keeps_the_orchestrator_away_from_the_recording(db):
    lecture, session_id, _ = seed_world(db)
    state = resolver().for_lecture(db, lecture["lecture_id"])
    assert state["stages"][RECORDING_LINK]["action"] == LINK_RECORDING
    assert (state["executable_stage"], state["next_executable_action"]) == (
        TRANSCRIPT, ACQUIRE_TRANSCRIPT)

    runner = RefusingRunner()
    summary = PipelineOrchestrator(
        resolver=resolver(), runner=runner, preflight=StubPreflight(),
        lock_manager=NullLockManager(), cycle_lock=NullCycleLock(), max_passes=4,
    ).run_window(db, DAY, run_type=RUN_TYPE_BACKFILL,
                 lecture_ids=[str(lecture["lecture_id"])])

    assert runner.calls == []
    assert LINK_RECORDING not in summary["lectures"][0]["actions"]
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None

    items = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=datetime.now(timezone.utc))
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert ours["outcome"] == c.BLOCKED_BY_EARLIER_STAGE
    assert (ours["earlier_stage"], ours["earlier_action"]) == (TRANSCRIPT, ACQUIRE_TRANSCRIPT)


# ---------------------------------------------------------------------------
# 9-10. the live preview writes nothing and publishes nothing
# ---------------------------------------------------------------------------

def test_the_backfill_live_preview_performs_zero_writes_and_creates_no_link(db):
    lecture, session_id, key = seed_world(db)
    graph = live_graph(lecture, session_id)
    report = BackfillRecordingReport(
        mode="observe",
        preview_factory=lambda: build_preview(SETTINGS, resolver=resolver(), graph=graph),
        resolver_factory=lambda: resolver("observe"))
    before = world_fingerprint(db, session_id, key)

    guarded = ReadOnly(db)
    items, graph_calls = report.preview_day(guarded, DAY)

    assert guarded.statements > 0
    assert world_fingerprint(db, session_id, key) == before
    assert not [path for verb, path in graph.paths if verb == "POST"]
    assert not [path for _, path in graph.paths if "createLink" in path]
    assert graph_calls == len(graph.paths) > 0

    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    # The live verdict is exact; the pipeline would still act on TRANSCRIPT first.
    assert ours["recording_status"] == m.EXACT_RECORDING_FILE_MATCHED
    assert ours["would_write"] is True and ours["source"] == "onedrive_recordings"
    assert ours["timestamp_difference_seconds"] == 30.0
    assert ours["outcome"] == c.BLOCKED_BY_EARLIER_STAGE
    assert ours["evaluation"] == c.EVALUATION_LIVE_PREVIEW
    assert "https://" not in repr(items) and ".mp4" not in repr(items)


def test_an_existing_link_is_reported_without_a_single_graph_call(db):
    lecture, session_id, _ = seed_world(db, recording_url="https://kbc.sharepoint.com/old")
    graph = live_graph(lecture, session_id)
    items, graph_calls = BackfillRecordingReport(
        mode="write",
        preview_factory=lambda: build_preview(SETTINGS, resolver=resolver(), graph=graph),
        resolver_factory=resolver).preview_day(ReadOnly(db), DAY)
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert ours["outcome"] == c.ALREADY_LINKED
    assert graph.paths == [] and graph_calls == 0


# ---------------------------------------------------------------------------
# 4-7, 11, 13-14. the execute snapshot
# ---------------------------------------------------------------------------

def test_an_exact_write_is_reported_as_written_by_this_run_and_nothing_else_changed(db):
    lecture, session_id, key = seed_world(db)
    before_session = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    before_perfect = row_json(db, "qa_perfect_lectures", "lecture_key", key)
    before_checklist = checklist(db, session_id)
    started = datetime.now(timezone.utc) - timedelta(seconds=1)

    service, _ = service_for(graph_for(session_id), [file_item(lecture)])
    runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                 lecture_id=lecture["lecture_id"])

    after_session = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    after_perfect = row_json(db, "qa_perfect_lectures", "lecture_key", key)
    assert after_session["recording_url"] == ORG_LINK
    # QA, checklist, clips, transcript and attendance fields: byte-identical.
    assert without(after_session, SESSION_RECORDING_COLUMNS) == \
        without(before_session, SESSION_RECORDING_COLUMNS)
    assert after_session["positive_clips"] == before_session["positive_clips"]
    assert after_session["transcript_url"] == before_session["transcript_url"]
    assert checklist(db, session_id) == before_checklist
    # Perfect QA fields unchanged; only the recording-owned columns moved.
    assert without(after_perfect, PERFECT_RECORDING_COLUMNS) == \
        without(before_perfect, PERFECT_RECORDING_COLUMNS)

    items = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=started)
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert ours["outcome"] == c.WRITTEN and ours["perfect_row_updated"] is True
    assert ours["source"] == "fixture_source" and ours["attempt_count"] == 1
    summary = summarize(items)
    assert summary["reconciles"] and summary["written"] == 1
    assert summary["perfect_rows_updated"] == 1
    assert summary["still_missing_after"] == summary["missing_before"] - 1

    # The same lecture seen by a LATER run is already linked, not re-written.
    later = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=datetime.now(timezone.utc)
                          + timedelta(seconds=5))
    assert next(i for i in later if i["lecture_id"] == str(lecture["lecture_id"]))[
        "outcome"] == c.ALREADY_LINKED


def test_an_existing_recording_url_is_never_overwritten_and_reads_as_already_linked(db):
    lecture, session_id, _ = seed_world(db, recording_url="https://kbc.sharepoint.com/old")
    service, _ = service_for(graph_for(session_id), [file_item(lecture)])
    outcome = service.link(db, lecture["lecture_id"], session_id, write=True)
    assert outcome["status"] == m.RECORDING_ALREADY_LINKED and not outcome["written"]
    assert row_json(db, "qa_doctors_sessions", "session_id",
                    session_id)["recording_url"] == "https://kbc.sharepoint.com/old"
    items = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=datetime.now(timezone.utc)
                          - timedelta(minutes=1))
    assert next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))[
        "outcome"] == c.ALREADY_LINKED


def test_two_graph_recordings_for_one_call_are_ambiguous_and_never_written(db):
    lecture, session_id, _ = seed_world(db)
    graph = graph_for(session_id)
    call_id = graph.collections["/users/"][0]["callId"]
    graph.collections["/users/"] = [
        {"id": "rec-1", "callId": call_id, "createdDateTime": GRAPH_CREATED.isoformat()},
        {"id": "rec-2", "callId": call_id, "createdDateTime": GRAPH_CREATED.isoformat()}]
    service, source = service_for(graph, [file_item(lecture)])
    result = runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                          lecture_id=lecture["lecture_id"])
    assert result["legacy_rows_written"] == 0 and source.calls == 0
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None
    assert not [p for v, p in graph.paths if v == "POST"]
    items = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=datetime.now(timezone.utc)
                          - timedelta(minutes=1))
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert (ours["recording_status"], ours["outcome"]) == (m.GRAPH_RECORDING_AMBIGUOUS,
                                                           c.AMBIGUOUS)


def test_two_qualifying_files_are_ambiguous_and_never_written(db):
    lecture, session_id, _ = seed_world(db)
    service, _ = service_for(graph_for(session_id),
                             [file_item(lecture, lead=20, n=0), file_item(lecture, lead=40, n=1)])
    runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                 lecture_id=lecture["lecture_id"])
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None
    items = execute_items(db, DAY, resolver=resolver(), repository=RecordingLinkRepository(),
                          mode="write", written_since=datetime.now(timezone.utc)
                          - timedelta(minutes=1))
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert (ours["recording_status"], ours["outcome"]) == (m.AMBIGUOUS_RECORDING_FILES,
                                                           c.AMBIGUOUS)
    assert ours["exact_candidate_count"] == 2


def test_observe_mode_snapshots_without_reading_the_stage_table(db):
    lecture, _, _ = seed_world(db)

    class Untouchable:
        def details(self, *_args):
            raise AssertionError("observe mode must not read lecture_recording_links")

    items = execute_items(db, DAY, resolver=resolver("observe"), repository=Untouchable(),
                          mode="observe", written_since=datetime.now(timezone.utc))
    ours = next(i for i in items if i["lecture_id"] == str(lecture["lecture_id"]))
    assert ours["recording_link_mode"] == "observe"
    assert ours["outcome"] in (c.BLOCKED_BY_EARLIER_STAGE, c.NOT_EVALUATED)


# ---------------------------------------------------------------------------
# migration 022: the per-run snapshot
# ---------------------------------------------------------------------------

def test_the_run_snapshot_round_trips_and_replaces_a_resumed_day(db):
    repository = BackfillRepository()
    run_id = repository.create(db, requested_from=date(2031, 3, 4),
                               requested_to=date(2031, 3, 4), created_by="t",
                               total_days=1, runner_version="v", mode=MODE_PREVIEW)
    repository.record_day(db, run_id, business_date=date(2031, 3, 4), status="COMPLETED")
    first = c.item_from_preview_row(
        {"lecture_id": None, "date": "2031-03-04", "subject": "S",
         "population": "LEGACY_ROW_ONLY", "recording_match_status": "NO_CODED_LECTURE",
         "legacy_session_ref": "abc", "reason": "r", "verification": "LIVE_VERIFIED"},
        mode="write")
    repository.replace_recording_items(db, run_id, business_date=date(2031, 3, 4),
                                       items=[first, {**first, "item_key": "legacy:def"}])
    repository.replace_recording_items(db, run_id, business_date=date(2031, 3, 4),
                                       items=[first], error_code="GRAPH_AUTHENTICATION_FAILED")

    stored = repository.recording_items(db, run_id)
    assert [row["item_key"] for row in stored] == ["legacy:abc"]
    assert stored[0]["outcome"] == c.NO_CODED_LECTURE
    assert repository.days(db, run_id)[0]["recording_error_code"] == \
        "GRAPH_AUTHENTICATION_FAILED"
    columns = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'backfill_run_recording_items'").fetchall()}
    assert not {"recording_url", "recording_item_id", "recording_drive_id",
                "recording_filename", "web_url"} & columns


def test_the_snapshot_refuses_an_unknown_population(db):
    repository = BackfillRepository()
    run_id = repository.create(db, requested_from=date(2031, 3, 4),
                               requested_to=date(2031, 3, 4), created_by="t",
                               total_days=1, runner_version="v", mode=MODE_PREVIEW)
    bad = {"item_key": "x", "population": "INVENTED", "recording_link_mode": "write",
           "evaluation": "LIVE_PREVIEW", "outcome": "OTHER"}
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            repository.replace_recording_items(db, run_id, business_date=date(2031, 3, 4),
                                               items=[bad])


# ---------------------------------------------------------------------------
# the lecture detail's Recording Link block
# ---------------------------------------------------------------------------

def test_the_lecture_detail_explains_the_stage_without_exposing_the_link(db):
    lecture, session_id, _ = seed_world(db)
    service, _ = service_for(graph_for(session_id), [file_item(lecture)])
    runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                 lecture_id=lecture["lecture_id"])
    operations = OperationsService(resolver=resolver(), recording_links=RecordingLinkRepository())
    block = operations.lecture_stage_matrix(db, lecture["lecture_id"])["recording_link"]

    assert block["recording_link_mode"] == "write"
    assert block["state"] == "COMPLETE" and block["recording_available"] is True
    assert block["written_by_platform"] is True and block["attempt_count"] == 1
    assert block["last_status"] == m.WRITTEN and block["source"] == "fixture_source"
    assert block["refused_to_guess"] is False
    assert "https://" not in repr(block) and "item-0" not in repr(block)


def test_the_lecture_detail_says_when_the_stage_refused_to_guess(db):
    lecture, session_id, _ = seed_world(db)
    service, _ = service_for(graph_for(session_id),
                             [file_item(lecture, lead=20, n=0), file_item(lecture, lead=40, n=1)])
    runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                 lecture_id=lecture["lecture_id"])
    block = OperationsService(resolver=resolver(), recording_links=RecordingLinkRepository(
    )).lecture_stage_matrix(db, lecture["lecture_id"])["recording_link"]
    assert (block["state"], block["refused_to_guess"]) == ("REVIEW_REQUIRED", True)
    assert block["recording_available"] is False and block["next_attempt_after"] is None


def test_observe_mode_detail_reads_no_stage_state(db):
    lecture, _, _ = seed_world(db)

    class Untouchable:
        def details(self, *_args):
            raise AssertionError("observe mode must not read lecture_recording_links")

    block = OperationsService(resolver=resolver("observe"), recording_links=Untouchable(
    )).lecture_stage_matrix(db, lecture["lecture_id"])["recording_link"]
    assert block["recording_link_mode"] == "observe"
    assert block["owner"] == "LEGACY_RECORDING_BRANCH" and block["attempt_count"] is None
