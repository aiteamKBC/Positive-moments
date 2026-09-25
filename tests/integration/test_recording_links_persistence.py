"""
The coded RECORDING_LINK stage against the isolated test database.

Self-contained: every row is seeded here and each test's transaction is rolled
back. Nothing is stubbed between the stage and PostgreSQL - the real resolver,
the real StageRunner action, the real repository SQL. Only Microsoft Graph is
a stub, because a test must never reach it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.recording_links import (
    PERFECT_RECORDING_COLUMNS,
    SESSION_RECORDING_COLUMNS,
    RecordingLinkRepository,
)
from app.orchestration.runner import StageRunner
from app.orchestration.stages import (
    COMPLETE,
    LINK_RECORDING,
    MISSING,
    NOT_APPLICABLE,
    RECORDING_LINK,
    REVIEW_REQUIRED,
    WAIT_FOR_RECORDING,
    WAITING,
)
from app.orchestration.state import PipelineStateResolver
from app.recordings import models as m
from app.recordings.drive_items import DriveItemDiscovery, RecordingLinkPublisher
from app.recordings.graph_lookup import RecordingMetadataGateway
from app.recordings.service import RecordingLinkService
from app.transcripts.identity import teams_call_id
from tests.integration.seeding import (
    DAY,
    insert,
    seed_lecture,
    seed_legacy_row,
    seed_render,
)
from tests.unit.recording_graph_fakes import FakeGraph, graph_error


GRAPH_CREATED = datetime(2031, 3, 4, 9, 0, 30, tzinfo=timezone.utc)
NOW = datetime(2031, 3, 5, 12, 0, tzinfo=timezone.utc)
ORG_LINK = "https://kbc.sharepoint.com/:v:/s/cohort/org-view-link"


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert "test" in name, "refusing: not the isolated test database"
        yield connection
    finally:
        connection.rollback()
        connection.close()


class WriteSettings:
    recording_link_mode = "write"
    recording_links_writable = True


# ---------------------------------------------------------------------------
# a seeded world: one lecture, its render, its legacy row, its Perfect row
# ---------------------------------------------------------------------------

def seed_world(db, *, recording_url=None, cancelled="false", perfect_meeting=None,
               perfect_url=None):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    session_id = lecture["transcript_id"]
    seed_legacy_row(
        db, session_id, meeting_id=lecture["meeting_id"], day=DAY,
        subject=lecture["subject"], recording_url=recording_url,
        cancelled_session=cancelled,
        # QA, clips and transcript fields the recording stage must never touch.
        strengths={"strength_1": {"title": "kept"}},
        areas_for_development={"area_1": {"title": "kept"}},
        overall_judgement="kept", teaching_quality_rating=4,
        teaching_quality_comments="kept", partial_count=0, not_met_count=2,
        clips_status="COMPLETED", recording_id="foreign-recording-id",
        transcript_url="https://foreign/transcript")
    db.execute("UPDATE public.qa_doctors_sessions SET positive_clips = %s::jsonb "
               "WHERE session_id = %s", ('[{"clip": "kept"}]', session_id))
    for order in range(1, 12):
        insert(db, "qa_doctors_checklist_items", session_id=session_id,
               session_id_match=f"{session_id}_{order}", checklist_order=order,
               checklist_item=f"item {order}", status="Met", evidence="kept")
    key = f"{DAY.isoformat()}|{lecture['subject']}"
    insert(db, "qa_perfect_lectures", lecture_key=key, session_date=DAY,
           subject=lecture["subject"], met_count=11, engagement=7, attended_count=5,
           session_id=session_id, meeting_id=perfect_meeting, recording_url=perfect_url)
    return lecture, session_id, key


def file_item(lecture, *, lead=30, n=0, subject=None):
    stamp = GRAPH_CREATED - timedelta(seconds=lead)
    name = f"{subject or lecture['subject']}-{stamp:%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4"
    return {"id": f"item-{n}", "name": name, "file": {},
            "webUrl": f"https://kbc.sharepoint.com/sites/cohort/{n}.mp4",
            "parentReference": {"driveId": f"drive-{n}"}}


class _Source:
    name = "fixture_source"

    def __init__(self, items, error=None):
        self._items, self.error, self.calls = items, error, 0

    def applicable(self, target):
        return True

    def items(self, target):
        self.calls += 1
        if self.error:
            raise self.error
        return self._items


def graph_for(session_id, *, recordings=None, error=None):
    call_id = teams_call_id(session_id)
    rows = recordings if recordings is not None else [
        {"id": "rec-1", "callId": call_id, "createdDateTime": GRAPH_CREATED.isoformat()}]
    return FakeGraph(collections={"/users/": rows},
                     errors={"/users/": error} if error else {},
                     posts={"/drives/drive-0/items/item-0/createLink":
                            {"link": {"webUrl": ORG_LINK}}})


def service_for(graph, items, *, source_error=None):
    source = _Source(items, source_error)
    return RecordingLinkService(
        repository=RecordingLinkRepository(),
        metadata_gateway=RecordingMetadataGateway(graph),
        discovery=DriveItemDiscovery([source]),
        publisher=RecordingLinkPublisher(graph)), source


def runner_with(service):
    runner = StageRunner(settings=WriteSettings(), persist=True)
    runner._recording_link_service = lambda: service
    return runner


def resolve(db, lecture, *, clock=NOW):
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository(),
                                     recording_link_mode="write", clock=lambda: clock)
    return resolver.for_lecture(db, lecture["lecture_id"])["stages"][RECORDING_LINK]


def row_json(db, table, key_column, key):
    return db.execute(f"SELECT to_jsonb(t) FROM public.{table} t WHERE {key_column} = %s",
                      (key,)).fetchone()[0]


def without(row, columns):
    return {k: v for k, v in row.items() if k not in columns}


def checklist(db, session_id):
    return db.execute("SELECT to_jsonb(c) FROM public.qa_doctors_checklist_items c "
                      "WHERE session_id = %s ORDER BY checklist_order",
                      (session_id,)).fetchall()


def link_state(db, lecture):
    return db.execute("SELECT status, stage_state, attempt_count, next_attempt_after, "
                      "recording_url_written FROM public.lecture_recording_links "
                      "WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()


# ---------------------------------------------------------------------------
# the full stage, through the real runner
# ---------------------------------------------------------------------------

def test_an_exact_match_is_written_through_the_real_runner_and_nothing_else_changes(db):
    lecture, session_id, key = seed_world(db)
    stage = resolve(db, lecture)
    assert (stage["state"], stage["action"]) == (MISSING, LINK_RECORDING)
    assert stage["legacy_session_id"] == session_id

    before_session = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    before_perfect = row_json(db, "qa_perfect_lectures", "lecture_key", key)
    before_checklist = checklist(db, session_id)

    graph = graph_for(session_id)
    service, _ = service_for(graph, [file_item(lecture)])
    result = runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                          lecture_id=lecture["lecture_id"])
    assert result["legacy_rows_written"] == 2          # one session row, one Perfect row
    assert result["provider_calls"] == 0

    after_session = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    assert after_session["recording_url"] == ORG_LINK
    assert after_session["recording_item_id"] == "item-0"
    assert after_session["recording_drive_id"] == "drive-0"
    assert after_session["recording_filename"].endswith("-Meeting Recording.mp4")
    assert after_session["recording_link_status"] == m.LINK_ORGANIZATION_VIEW
    assert after_session["recording_link_updated_at"] is not None
    # QA, attendance, clips and transcript fields: byte-identical.
    assert without(after_session, SESSION_RECORDING_COLUMNS) == \
        without(before_session, SESSION_RECORDING_COLUMNS)
    assert checklist(db, session_id) == before_checklist

    after_perfect = row_json(db, "qa_perfect_lectures", "lecture_key", key)
    assert after_perfect["recording_url"] == ORG_LINK
    assert after_perfect["meeting_id"] == lecture["meeting_id"]      # was NULL
    assert after_perfect["session_id"] == session_id                 # already set
    assert without(after_perfect, PERFECT_RECORDING_COLUMNS) == \
        without(before_perfect, PERFECT_RECORDING_COLUMNS)

    assert link_state(db, lecture)[:2] == (m.WRITTEN, COMPLETE)
    assert link_state(db, lecture)[4] is True
    stage = resolve(db, lecture)
    assert (stage["state"], stage["owner"]) == (COMPLETE, "CODED_RECORDING_LINK")


def test_a_repeated_run_is_idempotent(db):
    lecture, session_id, _ = seed_world(db)
    graph = graph_for(session_id)
    service, source = service_for(graph, [file_item(lecture)])
    runner = runner_with(service)
    runner.execute(db, LINK_RECORDING, session_date=DAY, lecture_id=lecture["lecture_id"])
    first = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    calls = len(graph.paths)
    again = runner.execute(db, LINK_RECORDING, session_date=DAY,
                           lecture_id=lecture["lecture_id"])
    assert again["summary"]["status"] == "NOOP"
    assert again["legacy_rows_written"] == 0
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id) == first
    assert len(graph.paths) == calls and source.calls == 1         # no new Graph work
    assert link_state(db, lecture)[2] == 1                          # one attempt recorded


def test_an_existing_recording_url_is_never_overwritten(db):
    lecture, session_id, key = seed_world(db, recording_url="https://kbc.sharepoint.com/old",
                                          perfect_url="https://kbc.sharepoint.com/old-p")
    assert resolve(db, lecture)["state"] == COMPLETE
    before = row_json(db, "qa_doctors_sessions", "session_id", session_id)
    graph = graph_for(session_id)
    service, _ = service_for(graph, [file_item(lecture)])
    outcome = service.link(db, lecture["lecture_id"], session_id, write=True)
    assert outcome["status"] == m.RECORDING_ALREADY_LINKED
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id) == before
    assert row_json(db, "qa_perfect_lectures", "lecture_key", key)["recording_url"] == \
        "https://kbc.sharepoint.com/old-p"
    assert graph.paths == []


def test_the_sql_refuses_to_replace_a_url_that_appears_concurrently(db):
    lecture, session_id, key = seed_world(db)
    db.execute("UPDATE public.qa_doctors_sessions SET recording_url = %s "
               "WHERE session_id = %s", ("https://n8n-won-the-race", session_id))
    from app.recordings.candidates import candidate_from_drive_item
    result = RecordingLinkRepository().write_link(
        db, session_id=session_id, meeting_id=lecture["meeting_id"], lecture_key=key,
        recording_url=ORG_LINK, candidate=candidate_from_drive_item(file_item(lecture), "s"),
        link_status=m.LINK_ORGANIZATION_VIEW)
    assert result["sessions_updated"] == 0 and result["perfect_rows_updated"] == 0
    assert row_json(db, "qa_doctors_sessions", "session_id",
                    session_id)["recording_url"] == "https://n8n-won-the-race"


@pytest.mark.parametrize("overrides", [
    {"link_status": "AMBIGUOUS_RECORDING_FILES"},
    {"meeting_id": "some-other-meeting"},
    {"recording_url": "   "},
])
def test_the_write_sql_keeps_every_guard(db, overrides):
    from app.recordings.candidates import candidate_from_drive_item
    lecture, session_id, key = seed_world(db)
    params = dict(session_id=session_id, meeting_id=lecture["meeting_id"], lecture_key=key,
                  recording_url=ORG_LINK,
                  candidate=candidate_from_drive_item(file_item(lecture), "s"),
                  link_status=m.LINK_ORGANIZATION_VIEW)
    params.update(overrides)
    result = RecordingLinkRepository().write_link(db, **params)
    assert result["sessions_updated"] == 0
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None


def test_a_perfect_row_keeps_its_own_meeting_id(db):
    lecture, session_id, key = seed_world(db, perfect_meeting="perfect-own-meeting")
    service, _ = service_for(graph_for(session_id), [file_item(lecture)])
    service.link(db, lecture["lecture_id"], session_id, write=True)
    perfect = row_json(db, "qa_perfect_lectures", "lecture_key", key)
    assert perfect["meeting_id"] == "perfect-own-meeting"
    assert perfect["recording_url"] == ORG_LINK
    assert (perfect["met_count"], float(perfect["engagement"]), perfect["attended_count"]) == \
        (11, 7.0, 5)


# ---------------------------------------------------------------------------
# terminal and retryable outcomes
# ---------------------------------------------------------------------------

def test_ambiguous_files_never_write_and_stay_in_review(db):
    lecture, session_id, _ = seed_world(db)
    service, _ = service_for(graph_for(session_id),
                             [file_item(lecture, lead=20, n=0), file_item(lecture, lead=40, n=1)])
    result = runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                          lecture_id=lecture["lecture_id"])
    assert result["legacy_rows_written"] == 0
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None
    assert link_state(db, lecture)[:2] == (m.AMBIGUOUS_RECORDING_FILES, REVIEW_REQUIRED)
    stage = resolve(db, lecture)
    assert (stage["state"], stage["reason"]) == (REVIEW_REQUIRED, m.AMBIGUOUS_RECORDING_FILES)


def test_a_graph_refusal_waits_with_backoff_then_retries_and_is_bounded(db):
    lecture, session_id, _ = seed_world(db)
    graph = graph_for(session_id, error=graph_error(403))
    service, source = service_for(graph, [file_item(lecture)])
    runner = runner_with(service)
    runner.execute(db, LINK_RECORDING, session_date=DAY, lecture_id=lecture["lecture_id"])
    status, state, attempts, due, written = link_state(db, lecture)
    assert (status, state, attempts, written) == (m.GRAPH_LOOKUP_FAILED, WAITING, 1, False)
    assert source.calls == 0                               # no file search on a refusal
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None

    waiting = resolve(db, lecture, clock=due - timedelta(minutes=1))
    assert (waiting["state"], waiting["action"]) == (WAITING, WAIT_FOR_RECORDING)
    assert resolve(db, lecture, clock=due + timedelta(minutes=1))["action"] == LINK_RECORDING

    repository = RecordingLinkRepository()
    decision = service.decide(repository.target(db, lecture["lecture_id"], session_id))
    for _ in range(m.MAX_ATTEMPTS):
        repository.record(db, decision)
    status, state, attempts, _, _ = link_state(db, lecture)
    assert (state, attempts) == (REVIEW_REQUIRED, m.MAX_ATTEMPTS + 1)
    assert resolve(db, lecture, clock=NOW + timedelta(days=30))["state"] == REVIEW_REQUIRED


def test_a_cancelled_lecture_is_terminal_and_costs_nothing(db):
    lecture, session_id, _ = seed_world(db, cancelled="true")
    stage = resolve(db, lecture)
    assert (stage["state"], stage["reason"]) == (NOT_APPLICABLE,
                                                 m.NO_RECORDING_EXPECTED_CANCELLED)
    graph = graph_for(session_id)
    service, source = service_for(graph, [file_item(lecture)])
    result = runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                          lecture_id=lecture["lecture_id"])
    assert result["summary"]["status"] == "NOOP"
    assert graph.paths == [] and source.calls == 0
    assert link_state(db, lecture) is None


def test_a_failed_discovery_source_records_a_retry_not_a_write(db):
    lecture, session_id, _ = seed_world(db)
    service, _ = service_for(graph_for(session_id), [file_item(lecture)],
                             source_error=graph_error(403, code="accessDenied"))
    runner_with(service).execute(db, LINK_RECORDING, session_date=DAY,
                                 lecture_id=lecture["lecture_id"])
    assert link_state(db, lecture)[:2] == (m.DRIVE_ITEM_DISCOVERY_FAILED, WAITING)
    assert row_json(db, "qa_doctors_sessions", "session_id", session_id)["recording_url"] is None


def test_observe_mode_never_reads_or_writes_stage_state(db):
    lecture, _, _ = seed_world(db)
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository())
    stage = resolver.for_lecture(db, lecture["lecture_id"])["stages"][RECORDING_LINK]
    assert (stage["state"], stage["action"]) == (MISSING, WAIT_FOR_RECORDING)
    runner = StageRunner(settings=Settings(
        database_url="", aptem_database_url="", graph_tenant_id="", graph_client_id="",
        graph_client_secret="", graph_scope="", graph_base_url="",
        calendar_user_upn=""), persist=True)
    assert runner.can_run(LINK_RECORDING) is False
    assert link_state(db, lecture) is None


def test_the_migration_created_the_stage_table_with_its_state_check(db):
    lecture = seed_lecture(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute("INSERT INTO public.lecture_recording_links "
                       "(lecture_id, link_version, status, stage_state) "
                       "VALUES (%s, 'v', 's', 'INVENTED')", (lecture["lecture_id"],))
