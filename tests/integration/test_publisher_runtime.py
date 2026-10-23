"""
P0-D: the continuous publisher runtime, end to end, against the isolated test database.

The daemon runs its REAL planner, queue, transcript worker (P0-B), recording
worker (P0-C) and Bunny workers; Microsoft Graph, the LMS, the Teams calendar
and Bunny are fakes (no socket is opened). Proposed migrations 024-028 are
applied COMMITTED for the module and removed with their rollback scripts;
every lecture is purged by committed_cleanup.

Time: the workers and planner run on an injected clock (2030). Queue delays are
stamped with the DATABASE clock, so `settle()` makes every waiting job due -
it stands in for "the retry delay elapsed" - and releases queued follow-ups
whose not-before time the injected clock has passed.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.lms.sources import LmsSnapshot
from app.orchestration.locks import SchedulerCycleLock
from app.publisher import operations, pilot_migration
from app.publisher.bunny_jobs import (
    DONE, REVIEW, BunnyRuntime, CaptionWorker, EncodingWorker, ImportWorker,
)
from app.publisher.config import SHADOW, STAGING_WRITE, PublisherConfig
from app.publisher.daemon import DISCOVERY, RECONCILE_FULL, PublisherDaemon
from app.publisher.handlers import (
    BUNNY_PRIORITY, LIVE_PRIORITY, MAX_ATTEMPTS, BunnyJobHandler, RecordingJobHandler, TranscriptJobHandler, part_subject_key,
)
from app.publisher.planner import CalendarDiscovery, ConflictTolerantLectureRepository, Planner
from app.publisher.reconcile import AlertDispatcher, Reconciler
from app.publisher.recordings import RecordingPolicy, RecordingWorker
from app.publisher.transcripts import TranscriptPolicy, TranscriptWorker
from tests.integration.committed_cleanup import purge_lectures
from tests.integration.publisher_db import (
    MIGRATIONS, RECORDING_MIGRATION, RUNTIME_MIGRATION, RUNTIME_ROLLBACK, applied, recording_applied,
    reset_proposed_migrations, runtime_applied,
)
from tests.integration.seeding import graph_id, insert
from tests.unit.lms_fakes import occurrence, private_url
from tests.unit.publisher_fakes import LIBRARY_ID, FakeBunnyLibrary, FakeMedia, transient_source_error
from tests.unit.recording_fakes import LOOKUP_USER, SUBJECT, FakeRecordingGraph
from tests.unit.transcript_fakes import FakeGraph as FakeTranscriptGraph, vtt
from tools.integration_db_guard import assert_isolated

MARKER = "p0d-runtime@example.invalid"
START = datetime(2030, 7, 16, 9, 0, tzinfo=timezone.utc)       # Cairo 12:00
END = START + timedelta(hours=2)
URLISH = re.compile(r"(https?://|tempauth|[?&]sig=)", re.IGNORECASE)


class _Url(str):
    def __repr__(self):
        return "'<isolated test database>'"


_DSN = [""]


def _cleanup(admin):
    if "bunny_publisher_jobs" in applied(admin):
        admin.execute("DELETE FROM public.bunny_publisher_events")
        admin.execute("DELETE FROM public.bunny_publisher_jobs")
        admin.execute("DELETE FROM public.bunny_publisher_heartbeats")
    if runtime_applied(admin):
        for table in ("bunny_publisher_part_decisions", "bunny_publisher_alerts", "bunny_publisher_schedule",
                      "bunny_publisher_reconciliation_runs"):
            admin.execute(f"DELETE FROM public.{table}")
    if "bunny_videos" in applied(admin):
        admin.execute("DELETE FROM public.bunny_caption_current")
        admin.execute("DELETE FROM public.bunny_caption_publications")
        admin.execute("DELETE FROM public.bunny_videos")
    if "bunny_lms_occurrence_coverage" in applied(admin):
        admin.execute("DELETE FROM public.bunny_lms_occurrence_coverage")
    ids = [r[0] for r in admin.execute("SELECT lecture_id FROM public.lecture_sessions WHERE calendar_user_upn = %s",
                                       (MARKER,)).fetchall()]
    purge_lectures(admin, ids, with_artifacts=True)


@pytest.fixture(scope="module")
def url():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    _DSN[0] = url
    reset_proposed_migrations(url)
    admin = psycopg.connect(url, autocommit=True)
    try:
        assert_isolated(admin)
        assert applied(admin) == [] and recording_applied(admin) == [] and runtime_applied(admin) == []
        for path in MIGRATIONS + [RECORDING_MIGRATION, RUNTIME_MIGRATION]:
            admin.execute(path.read_text(encoding="utf-8"))
        yield _Url(url)
    finally:
        try:
            _cleanup(admin)
        finally:
            admin.close()
            reset_proposed_migrations(url)


@pytest.fixture(autouse=True)
def _isolated(url):
    yield
    with psycopg.connect(url, autocommit=True) as admin:
        _cleanup(admin)


# =========================================================================== the world
class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta


class FakeLms:
    def __init__(self):
        self.occurrences = []
        self.fail = False

    def load(self, window_start, window_end):
        if self.fail:
            raise RuntimeError("synthetic LMS outage")
        return LmsSnapshot(occurrences=tuple(o for o in self.occurrences
                                             if window_start <= o.scheduled_start < window_end),
                           shares={}, window_start=window_start, window_end=window_end)


class FakeCalendar:
    def __init__(self):
        self.days = []

    def discover_day(self, connection, day):
        self.days.append(day)
        return {"registry_rows_created": 0, "registry_rows_updated": 0}


class World:
    """One lecture's fakes plus the real runtime."""

    def __init__(self, url, *, mode=STAGING_WRITE, start=START, end=END, lms=True, **config):
        self.url, self.clock = url, Clock(end + timedelta(hours=1))
        self.start, self.end = start, end
        self.rgraph, self.tgraph = FakeRecordingGraph(), FakeTranscriptGraph()
        self.media = FakeMedia(self.rgraph.items)
        self.bunny = FakeBunnyLibrary(self.media)
        self.lms, self.calendar = FakeLms(), FakeCalendar()
        values = dict(
            mode=mode, database_url=url, graph_tenant_id="t", graph_client_id="c", graph_client_secret="s",
            start_date=(start - timedelta(days=10)).date(), lms_database_url="fake", aptem_database_url="fake",
            calendar_user_upn="calendar@example.invalid", staging_library_id=LIBRARY_ID, staging_api_key="k",
            staging_account_api_key="a", workers=1, poll_seconds=0.01)
        self.config = PublisherConfig(**{**values, **config})
        self.lecture_id = self.lecture(start=start, end=end)
        if lms:
            self.add_occurrence()
        self.daemon = self._daemon()

    # -- seeding ---------------------------------------------------------------------------
    def lecture(self, *, start, end, subject=SUBJECT) -> str:
        lecture_id = uuid.uuid4()
        self.thread = f"19:meeting_P0D{lecture_id.hex[:20]}@thread.v2"
        with psycopg.connect(self.url, autocommit=True) as c:
            insert(c, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn=MARKER,
                   calendar_event_id=f"evt-{lecture_id}", join_url=private_url(thread=self.thread),
                   subject=subject, normalized_subject=subject.lower(), module=subject, scheduled_start=start,
                   scheduled_end=end, session_date=start.date(), calendar_mapping_status="RESOLVED",
                   group_match_status="MATCHED", discovery_status="READY", meeting_id=f"MTG-{lecture_id.hex[:12]}",
                   downstream_ready=True, meeting_lookup_user_id=LOOKUP_USER,
                   meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
        return str(lecture_id)

    def add_occurrence(self, **kwargs):
        self.occurrence = occurrence(url=private_url(thread=self.thread), start=self.start,
                                     minutes=int((self.end - self.start).total_seconds() // 60), **kwargs)
        self.lms.occurrences.append(self.occurrence)
        return self.occurrence

    def part(self, start, end, *, transcript=True, minutes=None, call_id="call-1"):
        """One Graph recording + its SharePoint MP4 (+ the transcript of the same stretch)."""
        correlation = f"corr-{uuid.uuid4().hex[:10]}"
        recording = self.rgraph.recording(start, end, call_id=call_id, correlation=correlation)
        self.rgraph.recorded_file(recording, lead=35)
        if transcript:
            self.transcript(start, end, correlation, minutes=minutes, call_id=call_id)
        return recording

    def transcript(self, start, end, correlation, *, minutes=None, call_id="call-1", label="a"):
        tid = graph_id(uuid.uuid4().int % 90000 + 10000)
        minutes = minutes if minutes is not None else max(int((end - start).total_seconds() // 60) - 5, 1)
        self.tgraph.add(tid, start=start, end=end, call_id=call_id, content=vtt(minutes, label=label),
                        correlation=correlation)
        return tid

    def _daemon(self):
        connect = lambda: psycopg.connect(self.url, prepare_threshold=None)  # noqa: E731
        transcript = TranscriptWorker(connect=connect, gateway=self.tgraph.gateway(), graph_client=self.tgraph,
                                      clock=self.clock, policy=TranscriptPolicy(lock_timeout_ms=500))
        recording = RecordingWorker(connect=connect, graph=self.rgraph, discovery_factory=self.rgraph.discovery,
                                    evidence_reader=self.rgraph.reader(), clock=self.clock,
                                    policy=RecordingPolicy(lock_timeout_ms=500))
        self.runtime = BunnyRuntime(config=self.config, connect=connect, bunny=self.bunny, media=self.media,
                                    encoding_tier="Free", clock=self.clock)
        handlers = {"ACQUIRE_TRANSCRIPT": TranscriptJobHandler(worker=transcript, connect=connect),
                    "RESOLVE_RECORDING": RecordingJobHandler(worker=recording, connect=connect)}
        for kind, worker in (("IMPORT_VIDEO", ImportWorker(self.runtime)), ("POLL_ENCODING", EncodingWorker(self.runtime)),
                             ("PUBLISH_CAPTION", CaptionWorker(self.runtime))):
            handlers[kind] = BunnyJobHandler(kind, worker=worker, connect=connect)
        self.planner = Planner(config=self.config, connect=connect, lms_source=self.lms, calendar=self.calendar,
                               clock=self.clock)
        self.dispatcher = AlertDispatcher(self.config)
        self.reconciler = Reconciler(config=self.config, connect=connect, bunny=self.bunny, planner=self.planner,
                                     dispatcher=self.dispatcher, clock=self.clock)
        return PublisherDaemon(config=self.config, connect=connect, handlers=handlers, planner=self.planner,
                               reconciler=self.reconciler, clock=self.clock, identity="p0d-test")

    # -- driving ---------------------------------------------------------------------------
    def settle(self):
        with psycopg.connect(self.url, autocommit=True) as c:
            c.execute("UPDATE public.bunny_publisher_jobs SET next_attempt_at = now() "
                      "WHERE status = 'RETRY_WAIT' OR (status = 'QUEUED' AND next_attempt_at <= %s)", (self.clock(),))

    def run(self, rounds=12, step=timedelta(minutes=5)):
        for _ in range(rounds):
            self.clock.advance(step)
            self.settle()
            self.daemon.run_once()

    def plan(self):
        return self.planner.tick()

    # -- reading ---------------------------------------------------------------------------
    def q(self, sql, *args):
        with psycopg.connect(self.url) as c:
            return c.execute(sql, args).fetchall()

    def events(self):
        return [r[0] for r in self.q("SELECT event FROM public.bunny_publisher_events WHERE lecture_id = %s ORDER BY event_id",
                                     self.lecture_id)]

    def videos(self, *, live=None):
        rows = self.q("SELECT recording_part_index, import_status, encoding_status, bunny_video_id, video_title, "
                      "retired_at IS NOT NULL, metadata FROM public.bunny_videos WHERE lecture_id = %s "
                      "ORDER BY recording_part_index, created_at", self.lecture_id)
        return [r for r in rows if live is None or (not r[5]) == live]

    def captions(self):
        return self.q("SELECT p.publication_status, p.source_content_version, p.applied_offset_ms, "
                      "c.publication_id IS NOT NULL FROM public.bunny_caption_publications p "
                      "LEFT JOIN public.bunny_caption_current c ON c.publication_id = p.publication_id "
                      "WHERE p.lecture_id = %s ORDER BY p.created_at", self.lecture_id)

    def jobs(self, kind=None):
        return self.q("SELECT job_kind, subject_key, status, last_error_code FROM public.bunny_publisher_jobs "
                      "WHERE lecture_id = %s AND (%s::text IS NULL OR job_kind = %s) ORDER BY created_at",
                      self.lecture_id, kind, kind)

    def ready(self):
        return "LECTURE_FULLY_READY" in self.events()


def _no_urls(url):
    with psycopg.connect(url) as c:
        for table in ("bunny_videos", "bunny_caption_publications", "bunny_publisher_jobs",
                      "bunny_publisher_events", "bunny_publisher_alerts", "bunny_publisher_schedule", "bunny_recording_sources"):
            for (text,) in c.execute(f"SELECT row_to_json(t)::text FROM public.{table} t").fetchall():
                assert not URLISH.search(text), f"URL-like text persisted in {table}"


def _one_part_world(url, **kwargs):
    world = World(url, **kwargs)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    return world


# =========================================================================== lifecycle
def test_complete_new_lecture_lifecycle_single_part(url, monkeypatch):
    for key in ("OPENAI_API_KEY", "QA_MODEL_API_KEY", "N8N_PRECHECK_URL"):
        monkeypatch.delenv(key, raising=False)
    world = _one_part_world(url)
    world.run(rounds=10)
    assert world.ready()
    [(part, imported, encoding, video_id, title, retired, _)] = world.videos()
    assert (part, imported, encoding, retired) == (1, "IDENTIFIED", "VERIFIED", False)
    assert f"lms {world.occurrence.occurrence_id}" in title and "part" not in title
    assert world.captions() == [("VERIFIED", 1, 0, True)]
    text = world.bunny.caption_text(video_id)
    assert text.startswith("WEBVTT") and "<v " not in text and "Trainer Synthetic" not in text
    assert len(world.bunny.fetch_calls) == 1 and world.bunny.delete_calls == []
    assert {k: s for k, _, s, _ in world.jobs()} == {"ACQUIRE_TRANSCRIPT": "SUCCEEDED", "RESOLVE_RECORDING": "SUCCEEDED",
                                                       "IMPORT_VIDEO": "SUCCEEDED", "POLL_ENCODING": "SUCCEEDED",
                                                       "PUBLISH_CAPTION": "SUCCEEDED"} | {
        k: s for k, _, s, _ in world.jobs() if s == "QUEUED"}
    order = world.events()
    assert order.index("VIDEO_INTENT_RECORDED") < order.index("FETCH_SUBMITTING") < order.index("FETCH_SUBMITTED") \
        < order.index("VIDEO_IDENTIFIED") < order.index("MEDIA_VERIFIED") < order.index("CAPTION_VERIFIED") \
        < order.index("LECTURE_FULLY_READY")
    _no_urls(url)


def test_recording_before_transcript(url):
    world = World(url)
    recording = world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)
    world.run(rounds=8)
    assert world.videos()[0][2] == "VERIFIED" and not world.ready()
    assert world.captions() == []
    # The transcript appears later; the transcript track catches up at its refresh point.
    world.transcript(START + timedelta(minutes=1), END - timedelta(minutes=2), recording["contentCorrelationId"])
    world.clock.advance(timedelta(hours=6))
    world.run(rounds=8)
    assert world.ready() and world.captions()[0][0] == "VERIFIED"


def test_transcript_before_recording(url):
    world = World(url)
    correlation = "corr-late-recording"
    world.transcript(START + timedelta(minutes=1), END - timedelta(minutes=2), correlation)
    world.run(rounds=4)
    assert world.q("SELECT selection_status FROM public.lecture_transcript_selections WHERE lecture_id = %s",
                   world.lecture_id) == [("SELECTED",)]
    assert world.videos() == []
    recording = world.rgraph.recording(START + timedelta(minutes=1), END - timedelta(minutes=2), call_id="call-1",
                                       correlation=correlation)
    world.rgraph.recorded_file(recording, lead=35)
    world.run(rounds=10)
    assert world.ready()


def test_shadow_mode_plans_without_any_bunny_call(url):
    world = _one_part_world(url, mode=SHADOW)
    world.run(rounds=6)
    plan = world.plan()
    assert plan["would_import_parts"] == 1 and "IMPORT_VIDEO" not in plan["jobs_created"]
    assert world.bunny.fetch_calls == [] and world.media.calls == 0 and world.videos() == []
    assert {k for k, _, _, _ in world.jobs()} == {"ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING"}


def test_a_running_qa_cycle_pauses_nothing(url):
    # P0-F: with QA's global cycle lock held for the WHOLE lifecycle, the lecture
    # is discovered, its recording and transcript acquired, imported, captioned
    # and fully ready - and no QA row ever exists for it.
    world = _one_part_world(url)
    qa = psycopg.connect(url, autocommit=True)
    try:
        assert SchedulerCycleLock().try_acquire(qa)
        summary = world.plan()
        assert [c["status"] for c in summary["calendar"]] == ["COMPLETED"] and world.calendar.days
        world.run(rounds=10)
        assert world.q("SELECT status FROM public.bunny_recording_discoveries WHERE lecture_id = %s",
                       world.lecture_id) == [("LOCATED",)]
        assert world.q("SELECT selection_status FROM public.lecture_transcript_selections WHERE lecture_id = %s",
                       world.lecture_id) == [("SELECTED",)]
        assert world.ready() and "TRANSCRIPT_DEFERRED" not in world.events()
        if world.q("SELECT to_regclass('public.qa_doctors_sessions') IS NOT NULL")[0][0]:
            assert world.q("SELECT count(*) FROM public.qa_doctors_sessions WHERE meeting_id = "
                           "(SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s)",
                           world.lecture_id) == [(0,)]
    finally:
        qa.close()


def test_delayed_lms_match_holds_only_the_bunny_import(url):
    world = World(url, lms=False)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=6)
    status = world.q("SELECT match_status FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s "
                     "AND is_current", world.lecture_id)
    assert status == [("NO_LMS_SESSION",)]
    assert world.videos() == [] and world.bunny.fetch_calls == []
    assert world.q("SELECT status FROM public.bunny_recording_discoveries WHERE lecture_id = %s",
                   world.lecture_id) == [("LOCATED",)]
    world.add_occurrence()                                     # the LMS links it later
    world.run(rounds=10)
    assert world.ready()


def test_lms_only_week_additional_session_is_detected_and_never_creates_a_lecture(url):
    world = _one_part_world(url)
    orphan = occurrence(url=private_url(thread="19:meeting_P0DLMSONLY@thread.v2"),
                        start=START + timedelta(hours=3), meeting_type="week-additional")
    world.lms.occurrences.append(orphan)
    world.clock.advance(timedelta(hours=30))
    summary = world.plan()
    assert summary["lms"]["lms_only_week_additional"] == 1
    assert world.q("SELECT coverage_status FROM public.bunny_lms_occurrence_coverage WHERE lms_occurrence_id = %s",
                   orphan.occurrence_id) == [("LMS_SESSION_NOT_DISCOVERED",)]
    assert world.q("SELECT count(*) FROM public.lecture_sessions WHERE calendar_user_upn = %s", MARKER) == [(1,)]
    world.reconciler.run("LIGHT")
    alert = world.q("SELECT severity, detail ->> 'meeting_type' FROM public.bunny_publisher_alerts "
                    "WHERE kind = 'LMS_ONLY_SESSION' AND status = 'OPEN'")
    assert alert == [("WARNING", "week-additional")]


# =========================================================================== multipart and fragments
def test_two_recording_parts_each_get_their_own_video_and_caption(url):
    world = World(url)
    middle = START + timedelta(minutes=60)
    world.part(START + timedelta(minutes=1), middle + timedelta(minutes=1))
    world.part(middle, END - timedelta(minutes=2))
    world.run(rounds=12)
    assert world.ready()
    videos = world.videos()
    assert [v[0] for v in videos] == [1, 2] and {v[2] for v in videos} == {"VERIFIED"}
    assert "part 1/2" in videos[0][4] and "part 2/2" in videos[1][4]
    assert len({v[3] for v in videos}) == 2 and [c[0] for c in world.captions()] == ["VERIFIED", "VERIFIED"]
    first, second = (world.bunny.caption_text(v[3]) for v in videos)
    assert first != second                                     # each part's own raw WebVTT


def test_unpaired_short_fragment_waits_for_an_operator_decision(url):
    world = World(url)
    world.part(START + timedelta(minutes=1), START + timedelta(minutes=3), transcript=False)   # false start
    world.part(START + timedelta(minutes=5), END - timedelta(minutes=2))
    world.run(rounds=10)
    assert not world.ready()
    assert [v[0] for v in world.videos()] == [2]               # the fragment is never auto-published
    world.clock.advance(timedelta(hours=24))                   # overdue: the reconciler reports it
    world.reconciler.run("LIGHT")
    assert world.q("SELECT count(*) FROM public.bunny_publisher_alerts WHERE kind = 'FRAGMENT_DECISION_NEEDED' "
                   "AND status = 'OPEN'") == [(1,)]
    with psycopg.connect(url, autocommit=True) as c:
        operations.decide_part(c, world.lecture_id, 1, decision="EXCLUDE", actor="ops-test",
                               reason="false start before the lecture")
    world.run(rounds=3)
    assert world.ready() and "OPERATOR_PART_DECISION" in world.events()


def test_a_fragment_carrying_the_lectures_own_speech_is_published(url):
    world = World(url)
    world.part(START + timedelta(minutes=1), START + timedelta(minutes=12), minutes=10)
    world.part(START + timedelta(minutes=14), END - timedelta(minutes=2))
    world.run(rounds=12)
    assert world.ready() and [v[0] for v in world.videos()] == [1, 2]


# =========================================================================== failures and recovery
def test_expired_graph_url_confirmed_failure_is_cleaned_and_reimported(url):
    world = _one_part_world(url)
    world.media.expire_next = 1
    world.run(rounds=12)
    assert world.ready()
    videos = world.videos()
    assert len(videos) == 2 and videos[0][5] and not videos[1][5]          # gen 1 retired, gen 2 live
    assert videos[0][3] is not None and videos[0][6]["cleanup"] == "DELETED"
    assert world.bunny.delete_calls == [str(videos[0][3])] and len(world.bunny.fetch_calls) == 2
    order = world.events()
    assert order.index("VIDEO_IMPORT_FAILED_CONFIRMED") < order.index("VIDEO_DELETE_INTENDED") \
        < order.index("VIDEO_DELETED")
    assert videos[1][6]["generation"] == 2


def test_failed_cleanup_preserves_the_video_and_never_creates_another(url):
    world = _one_part_world(url)
    world.media.expire_next = 1
    world.bunny.delete_plan.append("fail")
    world.run(rounds=10)
    [row] = world.videos()
    assert (row[2], row[6]["cleanup"], row[5]) == ("FAILED", "DELETE_FAILED", False) and row[3] is not None
    assert len(world.bunny.fetch_calls) == 1 and not world.ready()
    job = world.q("SELECT job_id, status, last_error_code FROM public.bunny_publisher_jobs WHERE lecture_id = %s "
                  "AND job_kind = 'POLL_ENCODING'", world.lecture_id)[-1]
    assert job[1:] == ("REVIEW_REQUIRED", "CLEANUP_FAILED")
    with psycopg.connect(url, autocommit=True) as c:                         # an operator retries the cleanup
        operations.operator_retry(c, job[0], actor="ops-test", reason="Bunny delete fixed")
    world.run(rounds=12)
    assert world.ready() and len(world.bunny.fetch_calls) == 2


def test_graph_and_bunny_outages_are_retried(url):
    world = _one_part_world(url)
    world.media.errors.extend([transient_source_error(), transient_source_error()])
    world.bunny.outage = 3
    world.run(rounds=14)
    assert world.ready() and len(world.bunny.fetch_calls) == 1
    assert "FETCH_SOURCE_UNAVAILABLE" in world.events()


def test_unknown_fetch_outcome_is_only_reconciled_never_resubmitted(url):
    world = _one_part_world(url)
    world.bunny.fetch_plan.append("uncertain_absent")
    world.run(rounds=6)
    assert world.videos()[0][1] == "SUBMISSION_UNCERTAIN" and len(world.bunny.fetch_calls) == 1
    world.clock.advance(timedelta(hours=3))                   # the reconcile window passes
    world.run(rounds=4)
    assert world.videos()[0][1] == "REVIEW_REQUIRED" and len(world.bunny.fetch_calls) == 1
    job = [j for j in world.q("SELECT job_id, status FROM public.bunny_publisher_jobs WHERE lecture_id = %s "
                              "AND job_kind = 'IMPORT_VIDEO'", world.lecture_id) if j[1] == "REVIEW_REQUIRED"][0]
    with psycopg.connect(url, autocommit=True) as c:
        operations.operator_retry(c, job[0], actor="ops-test", reason="confirmed nothing reached Bunny")
    world.run(rounds=10)
    assert world.ready() and len(world.bunny.fetch_calls) == 2 and "OPERATOR_REIMPORT_APPROVED" in world.events()


def test_uncertain_fetch_that_did_create_the_video_is_identified(url):
    world = _one_part_world(url)
    world.bunny.fetch_plan.append("uncertain_created")
    world.run(rounds=10)
    assert world.ready() and len(world.bunny.fetch_calls) == 1


def test_duplicate_submission_protection(url):
    world = _one_part_world(url)
    world.run(rounds=3)                                        # recording located, LMS verified
    worker = ImportWorker(world.runtime)
    results, barrier = [], threading.Barrier(3)

    def go():
        barrier.wait()
        results.append(worker.run(world.lecture_id, 1))

    threads = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(world.bunny.fetch_calls) == 1 and len(world.videos(live=True)) == 1


def test_at_most_two_imports_are_in_flight(url):
    world = World(url)
    third = (END - START) / 3
    for n in range(3):
        world.part(START + third * n, START + third * (n + 1) + timedelta(minutes=1 if n < 2 else -1))
    world.bunny.polls_to_finish = 10_000                          # encodes stay in progress: slots stay taken
    world.run(rounds=8)
    assert len(world.bunny.fetch_calls) == 2
    statuses = sorted((v[0], v[1]) for v in world.videos())
    assert statuses == [(1, "IDENTIFIED"), (2, "IDENTIFIED"), (3, "PENDING")]
    assert "IMPORT_SLOTS_FULL" in [r[0] for r in world.q(
        "SELECT detail ->> 'reason' FROM public.bunny_publisher_events WHERE lecture_id = %s", world.lecture_id)]
    world.bunny.polls_to_finish = 1                               # encodes finish; a slot frees up
    world.run(rounds=14)
    assert world.ready() and len(world.bunny.fetch_calls) == 3


def test_worker_crash_after_the_fetch_is_reconciled_without_a_second_fetch(url):
    world = _one_part_world(url)
    original = world.bunny.fetch_video

    def crash_after_side_effect(source_url, *, title):
        original(source_url, title=title)
        raise RuntimeError("process killed after Bunny accepted the fetch")

    world.bunny.fetch_video = crash_after_side_effect
    world.run(rounds=12)
    assert world.ready() and len(world.bunny.fetch_calls) == 1
    order = world.events()
    # Submitted, never recorded as submitted (the process died), then reconciled from SUBMITTING by token.
    assert "FETCH_SUBMITTING" in order and "FETCH_SUBMITTED" not in order
    assert order.index("FETCH_SUBMITTING") < order.index("VIDEO_IDENTIFIED")
    assert "JOB_WORKER_EXCEPTION" in [r[0] for r in world.q("SELECT event FROM public.bunny_publisher_events")]


def test_expired_lease_is_taken_over_and_the_late_worker_loses(url):
    world = _one_part_world(url)
    queue = PublisherQueueRepository()
    with psycopg.connect(url, autocommit=True) as c:
        queue.enqueue_once(c, job_kind="RESOLVE_RECORDING", subject_key=f"lecture:{world.lecture_id}",
                           lecture_id=world.lecture_id, max_attempts=5)
        crashed = queue.claim(c, "worker-a", ["RESOLVE_RECORDING"])
        c.execute("UPDATE public.bunny_publisher_jobs SET locked_at = now() - interval '1 hour' WHERE job_id = %s",
                  (crashed["job_id"],))
        taken = queue.claim(c, "worker-b", ["RESOLVE_RECORDING"])
    assert taken["job_id"] == crashed["job_id"] and taken["attempt_count"] == 2
    handler = world.daemon.handlers["RESOLVE_RECORDING"]
    handler.handle(taken, "worker-b")
    handler.handle(crashed, "worker-a")                       # the crashed worker wakes up late
    events = [r[0] for r in world.q("SELECT event FROM public.bunny_publisher_events WHERE job_id = %s",
                                    crashed["job_id"])]
    assert "RECORDING_LEASE_LOST" in events
    assert world.q("SELECT status FROM public.bunny_publisher_jobs WHERE job_id = %s", crashed["job_id"]) == [("SUCCEEDED",)]


def test_a_job_stuck_on_its_final_attempt_is_released_for_review(url):
    world = _one_part_world(url)
    queue = PublisherQueueRepository()
    with psycopg.connect(url, autocommit=True) as c:
        queue.enqueue_once(c, job_kind="ACQUIRE_TRANSCRIPT", subject_key=f"lecture:{world.lecture_id}",
                           lecture_id=world.lecture_id, max_attempts=1)
        job = queue.claim(c, "worker-dead", ["ACQUIRE_TRANSCRIPT"])
        c.execute("UPDATE public.bunny_publisher_jobs SET locked_at = now() - interval '2 hours' WHERE job_id = %s",
                  (job["job_id"],))
        assert queue.claim(c, "worker-b", ["ACQUIRE_TRANSCRIPT"]) is None    # unclaimable: the old edge case
    world.daemon.tick()
    assert world.q("SELECT status, last_error_code FROM public.bunny_publisher_jobs WHERE job_id = %s", job["job_id"]) == [
        ("REVIEW_REQUIRED", "LEASE_EXPIRED_ON_FINAL_ATTEMPT")]
    with psycopg.connect(url, autocommit=True) as c:
        reopened = operations.operator_retry(c, job["job_id"], actor="ops-test", reason="worker host rebooted")
    assert reopened["status"] == "QUEUED" and reopened["attempt_count"] == 0
    assert "OPERATOR_RETRY" in world.events()


# =========================================================================== captions
def test_a_revised_transcript_revises_the_caption(url):
    world = World(url)
    recording = world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    assert world.ready() and world.captions() == [("VERIFIED", 1, 0, True)]
    tid = next(iter(world.tgraph.transcripts))
    world.tgraph.content[tid] = [vtt(100, label="revised")]                 # Teams revised the transcript
    world.clock.advance(timedelta(hours=6))
    world.run(rounds=8)
    captions = world.captions()
    assert [c[:2] for c in captions] == [("SUPERSEDED", 1), ("VERIFIED", 2)] and captions[1][3]
    assert recording


def test_an_invalid_caption_is_never_published(url):
    world = World(url)
    correlation = "corr-too-long"
    recording = world.rgraph.recording(START + timedelta(minutes=1), START + timedelta(minutes=61), call_id="call-1",
                                       correlation=correlation)
    world.rgraph.recorded_file(recording, lead=35)
    world.transcript(START + timedelta(minutes=1), START + timedelta(minutes=61), correlation, minutes=80)
    world.run(rounds=10)
    assert world.bunny.caption_calls == [] and not world.ready()
    assert ("PUBLISH_CAPTION", f"lecture:{world.lecture_id}:part:1", "REVIEW_REQUIRED",
            "CAPTION_VALIDATION_FAILED") in world.jobs("PUBLISH_CAPTION")


# =========================================================================== reconciliation, recovery, alerts
def test_daily_reconciliation_scans_bunny_and_writes_an_undelivered_digest(url):
    world = _one_part_world(url, alert_destination="ops-mailbox")
    world.run(rounds=10)
    world.bunny.add_video("Stray upload | rec-0000beef")                    # orphan
    world.bunny.add_video("copy one | rec-0000cafe")
    world.bunny.add_video("copy two | rec-0000cafe")                        # duplicate token
    summary = world.reconciler.run("FULL")
    assert summary["orphan_bunny_video"] == 3 and summary["duplicate_bunny_video"] == 1
    assert summary["video_missing_in_bunny"] == 0 and summary["digest"]["notification_status"] == "DISABLED"
    alerts = dict(world.q("SELECT kind, notification_status FROM public.bunny_publisher_alerts WHERE status = 'OPEN'"))
    assert alerts["DUPLICATE_BUNNY_VIDEO"] == "DISABLED" and alerts["DAILY_DIGEST"] == "DISABLED"
    assert world.dispatcher.rendered and not any(URLISH.search(t) for t in world.dispatcher.rendered)
    # the same condition seen again is one alert, not two
    world.reconciler.run("FULL")
    assert world.q("SELECT occurrences FROM public.bunny_publisher_alerts WHERE kind = 'DUPLICATE_BUNNY_VIDEO' "
                   "AND status = 'OPEN'") == [(2,)]
    _no_urls(url)


def test_recovery_after_a_day_offline(url):
    world = _one_part_world(url)
    world.run(rounds=2)
    world.daemon.tick()
    world.calendar.days.clear()
    world.clock.advance(timedelta(hours=26))                                  # the service was down
    ran = world.daemon.tick()
    assert set(ran) >= {RECONCILE_FULL, DISCOVERY}
    assert len(set(world.calendar.days)) >= 2                                 # missed days re-discovered
    world.run(rounds=10)
    assert world.ready()


def test_stale_heartbeat_raises_a_critical_alert_without_sending_it(url):
    world = _one_part_world(url, alert_destination="ops-mailbox")
    with psycopg.connect(url, autocommit=True) as c:
        PublisherQueueRepository().heartbeat(c, "vanished-worker", version="v")
        c.execute("UPDATE public.bunny_publisher_heartbeats SET started_at = now() - interval '2 hours', "
                  "seen_at = now() - interval '1 hour' "
                  "WHERE worker_id = 'vanished-worker'")
    world.reconciler.run("LIGHT")
    assert world.q("SELECT severity, notification_status FROM public.bunny_publisher_alerts WHERE kind = 'HEARTBEAT_STALE' "
                   "AND status = 'OPEN'") == [("CRITICAL", "DISABLED")]
    assert any("vanished-worker" in t for t in world.dispatcher.rendered)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DELETE FROM public.bunny_publisher_heartbeats WHERE worker_id = 'vanished-worker'")
    world.reconciler.run("LIGHT")
    assert world.q("SELECT status FROM public.bunny_publisher_alerts WHERE kind = 'HEARTBEAT_STALE'") == [("RESOLVED",)]


def test_lectures_before_the_start_date_are_backfill_scope_never_planned_or_failed(url):
    world = _one_part_world(url, mode=STAGING_WRITE)
    world.config = world.config.__class__(**{**world.config.__dict__, "start_date": (END + timedelta(days=1)).date()})
    world.daemon = world._daemon()
    world.run(rounds=4)
    world.clock.advance(timedelta(days=2))
    world.reconciler.run("LIGHT")
    assert world.jobs() == [] and world.videos() == [] and world.bunny.fetch_calls == []
    assert world.q("SELECT count(*) FROM public.bunny_publisher_alerts WHERE lecture_id = %s", world.lecture_id) == [(0,)]


def test_the_supervised_loop_runs_workers_and_stops_gracefully(url):
    world = _one_part_world(url, workers=2)
    world.config = world.config.__class__(**{**world.config.__dict__, "poll_seconds": 0.05})
    world.daemon = world._daemon()
    finished = {}
    runner = threading.Thread(target=lambda: finished.update(world.daemon.run_forever()))
    runner.start()
    try:
        for _ in range(200):
            if world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s AND status = 'SUCCEEDED'",
                       world.lecture_id)[0][0] >= 2:
                break
            threading.Event().wait(0.1)
    finally:
        world.daemon.request_stop()                           # what SIGTERM does
        runner.join(timeout=60)
    assert not runner.is_alive() and finished["iterations"] >= 1
    beats = dict(world.q("SELECT worker_id, (detail ->> 'stopped')::boolean FROM public.bunny_publisher_heartbeats "
                         "WHERE worker_id LIKE 'p0d-test%%'"))
    assert set(beats) == {"p0d-test:scheduler", "p0d-test:w0", "p0d-test:w1"} and all(beats.values())
    assert world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE status = 'RUNNING'") == [(0,)]


# =========================================================================== operations, pilot, schema
def test_operations_reads_are_complete_and_safe(url):
    world = _one_part_world(url)
    world.run(rounds=10)
    with psycopg.connect(url) as c:
        c.read_only = True
        summary = operations.status_summary(c, library_id=LIBRARY_ID)
        detail = operations.lecture_detail(c, world.lecture_id, library_id=LIBRARY_ID)
        pending = operations.pending_work(c)
        queue = operations.exceptions(c)
    assert summary["lectures_fully_ready"] == 1 and summary["jobs"]["IMPORT_VIDEO"]["SUCCEEDED"] == 1
    assert detail["readiness"]["fully_ready"] and detail["lms"]["occurrence_id"] == world.occurrence.occurrence_id
    assert [p["caption"] for p in detail["readiness"]["parts"]] == ["VERIFIED"]
    assert isinstance(pending, list) and isinstance(queue, list)
    assert not URLISH.search(repr(summary) + repr(detail))


def test_operator_actions_refuse_without_actor_or_on_live_jobs(url):
    world = _one_part_world(url)
    world.plan()
    job = world.q("SELECT job_id FROM public.bunny_publisher_jobs WHERE lecture_id = %s LIMIT 1", world.lecture_id)[0][0]
    with psycopg.connect(url, autocommit=True) as c:
        with pytest.raises(operations.OperatorActionRefused, match="ACTOR_AND_REASON_REQUIRED"):
            operations.operator_retry(c, job, actor="", reason="x")
        with pytest.raises(operations.OperatorActionRefused, match="JOB_NOT_RETRYABLE"):
            operations.operator_retry(c, job, actor="ops", reason="x")
        with pytest.raises(operations.OperatorActionRefused, match="INVALID_DECISION"):
            operations.decide_part(c, world.lecture_id, 1, decision="DELETE", actor="ops", reason="x")


def test_pilot_manifest_promotion_is_planned_applied_once_and_idempotent(url, tmp_path):
    from app.bunny.identity import build_title, recording_identity, video_record_id
    from app.bunny.manifest import PilotManifest
    world = _one_part_world(url)
    identity = recording_identity("b!pilot-drive", "PILOTITEM")
    record_id = video_record_id(LIBRARY_ID, "b!pilot-drive", "PILOTITEM")
    manifest = PilotManifest(tmp_path / "manifest.json")
    manifest.put_video({"bunny_video_record_id": str(record_id), "lecture_id": world.lecture_id,
                        "recording_part_index": 1, "recording_drive_id": "b!pilot-drive",
                        "recording_item_id": "PILOTITEM", "recording_identity": identity,
                        "bunny_library_id": LIBRARY_ID, "bunny_video_id": str(uuid.uuid4()),
                        "video_title": build_title(SUBJECT, START.date(), world.lecture_id, identity),
                        "import_method": "URL_FETCH", "import_status": "IDENTIFIED", "encoding_status": "VERIFIED",
                        "source_duration_seconds": 7000.0, "attempt_count": 1, "metadata": {}},
                       event="VIDEO_RECORD_CREATED")
    with psycopg.connect(url) as c:
        planned = pilot_migration.plan(c, tmp_path / "manifest.json", library_id=LIBRARY_ID)
    assert planned["applicable"] and planned["inserts"] == 2
    with psycopg.connect(url, autocommit=True) as c:
        pilot_migration.apply(c, tmp_path / "manifest.json", library_id=LIBRARY_ID, actor="ops-test")
        again = pilot_migration.plan(c, tmp_path / "manifest.json", library_id=LIBRARY_ID)
        assert again["inserts"] == 0 and again["applicable"]
        pilot_migration.apply(c, tmp_path / "manifest.json", library_id=LIBRARY_ID, actor="ops-test")
        with pytest.raises(ValueError):
            pilot_migration.apply(c, tmp_path / "manifest.json", library_id=785080, actor="ops-test")
    rows = world.q("SELECT count(*) FROM public.bunny_videos WHERE bunny_video_record_id = %s", record_id)
    assert rows == [(1,)]
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE event = 'PILOT_MANIFEST_MIGRATED'") == [(1,)]
    # the publisher now sees a live row for part 1 and never imports that part again
    world.run(rounds=4)
    assert world.bunny.fetch_calls == []


def test_runtime_migration_rollback_refuses_with_rows(url):
    with psycopg.connect(url) as c:
        PublisherQueueRepository()  # noqa: B018
        c.execute("INSERT INTO public.bunny_publisher_alerts (alert_id, severity, kind, dedupe_key, summary) "
                  "VALUES (gen_random_uuid(), 'WARNING', 'TEST_ALERT', 'k', 's')")
        with pytest.raises(psycopg.errors.RaiseException):
            with c.transaction():
                c.execute(RUNTIME_ROLLBACK.read_text(encoding="utf-8").replace("BEGIN;", "").replace("COMMIT;", ""))
        c.rollback()



# =========================================================================== P0-F: no QA runtime dependency
import contextlib  # noqa: E402
import time  # noqa: E402

from app.db.repositories.lecture_sessions import LectureSessionRepository  # noqa: E402
from app.db.repositories.qa_validation import QaValidationRepository  # noqa: E402
from app.graph.meetings import (  # noqa: E402
    DISCOVERY_MAILBOX_CONTEXT, JOIN_URL_OID_MATCHES_CONTEXT, ORGANIZER_ID_CONFIRMED, RESOLVED,
)
from app.lectures.duplicates import CASE_MULTIPLE_VALID_MEETINGS, CASE_SUPPRESSED  # noqa: E402
from app.lectures.models import CalendarDiscoveryBatch, CalendarEvent, MeetingResolution  # noqa: E402
from app.lectures.service import LectureDiscoveryService  # noqa: E402
from app.publisher.progress import DUPLICATE_AMBIGUOUS, DUPLICATE_LOSER, load_facts, readiness  # noqa: E402
from tests.unit.test_service import Runs  # noqa: E402

DAY_START = datetime(2030, 7, 17, 7, 0, tzinfo=timezone.utc)
DAY = DAY_START.date()


def _event(key, subject, hour):
    start = DAY_START.replace(hour=hour)
    return CalendarEvent(f"evt-{key}", f"ical-{key}", subject, start, start + timedelta(hours=2), "UTC",
                         private_url(thread=f"19:meeting_P0F{key}@thread.v2"),
                         calendar_organizer_address="Cohort@example.invalid")


class _Calendar:
    calendar_user_upn = MARKER                     # committed_cleanup purges every row it creates

    def __init__(self, events):
        self.events = list(events)

    def discover_calendar_events(self, target_date):
        return CalendarDiscoveryBatch(len(self.events), list(self.events))


class _Meetings:
    def resolve(self, url, calendar_organizer_address=None):
        return MeetingResolution(RESOLVED, meeting_id=f"MTG-{uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:16]}",
                                 meeting_organizer_user_id="mailbox-oid", organizer_validation=ORGANIZER_ID_CONFIRMED,
                                 meeting_lookup_user_id="mailbox-oid",
                                 meeting_lookup_context_source=DISCOVERY_MAILBOX_CONTEXT,
                                 join_url_oid_hint_status=JOIN_URL_OID_MATCHES_CONTEXT)


class _Groups:
    def __init__(self, events):
        self.groups = sorted({e.subject for e in events})

    def load_active_groups(self, connection):
        return self.groups


def _service(events, repository, *, qa=None):
    return LectureDiscoveryService(calendar=_Calendar(events), meetings=_Meetings(), aptem_repository=_Groups(events),
                                   lecture_repository=repository, run_repository=Runs(), qa_validation_repository=qa)


def _discovery_config(url):
    return PublisherConfig(mode=SHADOW, database_url=url, graph_tenant_id="t", graph_client_id="c",
                           graph_client_secret="s", start_date=DAY - timedelta(days=5), lms_database_url="fake",
                           aptem_database_url="fake", calendar_user_upn="calendar@example.invalid")


def _publisher_discovery(url, events):
    return CalendarDiscovery(_discovery_config(url), service=_service(events, ConflictTolerantLectureRepository()),
                             aptem_connect=lambda: contextlib.nullcontext(object()))


def _registry(url):
    with psycopg.connect(url) as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT subject, count(*) FROM public.lecture_sessions WHERE calendar_user_upn = %s AND session_date = %s "
            "GROUP BY 1", (MARKER, DAY)).fetchall()}


class _SqlProbe:
    """A connection that records every statement executed through it."""

    def __init__(self, connection):
        self._connection, self.sql = connection, []

    def execute(self, query, *args, **kwargs):
        self.sql.append(str(query))
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_a_new_lecture_is_discovered_while_the_qa_cycle_lock_is_held(url):
    events = [_event("new", "P0F Discovery New", 7)]
    planner = Planner(config=_discovery_config(url), connect=lambda: psycopg.connect(url, prepare_threshold=None),
                      calendar=_publisher_discovery(url, events), clock=lambda: DAY_START + timedelta(hours=4))
    qa = psycopg.connect(url, autocommit=True)
    try:
        assert SchedulerCycleLock().try_acquire(qa)            # QA's 21:00 / 23:00 cycle is running
        summary = planner.tick(calendar_days=[DAY])
    finally:
        qa.close()
    assert summary["calendar"] == [{"session_date": DAY.isoformat(), "status": "COMPLETED", "created": 1,
                                    "updated": 0, "locked": 0}]
    assert _registry(url) == {"P0F Discovery New": 1}
    with psycopg.connect(url) as c:
        row = c.execute("SELECT downstream_ready, meeting_id IS NOT NULL FROM public.lecture_sessions "
                        "WHERE calendar_user_upn = %s", (MARKER,)).fetchone()
    assert row == (True, True)                                 # persisted ready for the tracks, no QA involved
    assert summary["jobs_created"] == {"ACQUIRE_TRANSCRIPT": 1, "RESOLVE_RECORDING": 1}   # it has ended


def test_rows_qa_is_writing_are_skipped_without_stopping_the_batch(url):
    a, b, c = _event("a", "P0F Discovery A", 7), _event("b", "P0F Discovery B", 9), _event("c", "P0F Discovery C", 11)
    with psycopg.connect(url, autocommit=True) as pub:
        assert _publisher_discovery(url, [a]).discover_day(pub, DAY)["registry_rows_created"] == 1
    qa = psycopg.connect(url)                                  # QA's cycle transaction (not autocommit)
    publisher = _publisher_discovery(url, [a, b, c])
    try:
        # QA's own discovery inside its open cycle: updates A, inserts C, commits nothing yet.
        qa_run = _service([a, c], LectureSessionRepository()).discover_day(qa, object(), DAY)
        assert (qa_run["registry_rows_created"], qa_run["registry_rows_updated"]) == (1, 1)
        with psycopg.connect(url, autocommit=True) as pub:
            started = time.monotonic()
            outcome = publisher.discover_day(pub, DAY)
            elapsed = time.monotonic() - started
        assert (outcome["registry_rows_created"], outcome["registry_rows_locked"]) == (1, 2)
        assert elapsed < 5                                     # never waits behind QA's open transaction
        assert _registry(url) == {"P0F Discovery A": 1, "P0F Discovery B": 1}   # B discovered regardless
        qa.commit()                                            # QA's writes are unharmed
    finally:
        qa.close()
    with psycopg.connect(url, autocommit=True) as pub:
        again = publisher.discover_day(pub, DAY)
    assert (again["registry_rows_created"], again["registry_rows_updated"], again["registry_rows_locked"]) == (0, 3, 0)
    assert _registry(url) == {"P0F Discovery A": 1, "P0F Discovery B": 1, "P0F Discovery C": 1}   # one identity each


def test_publisher_discovery_never_queries_qa_doctors_sessions(url):
    built = CalendarDiscovery(_discovery_config(url)).service  # the real assembly (no network at build)
    assert built.qa_validation_repository is None
    assert isinstance(built.lecture_repository, ConflictTolerantLectureRepository)
    events = [_event("probe", "P0F Discovery Probe", 13)]
    with psycopg.connect(url, autocommit=True) as raw:
        probe = _SqlProbe(raw)
        _publisher_discovery(url, events).discover_day(probe, DAY)
        assert probe.sql and not any("qa_doctors_sessions" in q for q in probe.sql)
        if raw.execute("SELECT to_regclass('public.qa_doctors_sessions') IS NOT NULL").fetchone()[0]:
            control = _SqlProbe(raw)                           # QA's assembly does read it: the probe works
            _service(events, LectureSessionRepository(), qa=QaValidationRepository()).discover_day(
                control, object(), DAY)
            assert any("qa_doctors_sessions" in q for q in control.sql)


# -- D3: duplicate calendar events, decided by the publisher itself --------------------------------
def _confirm_organizer(world):
    with psycopg.connect(world.url, autocommit=True) as c:
        c.execute("UPDATE public.lecture_sessions SET organizer_validation_status = 'ORGANIZER_ID_CONFIRMED' "
                  "WHERE lecture_id = %s", (world.lecture_id,))


def _sibling(world, *, with_meeting: bool) -> str:
    """A second calendar event for the SAME occurrence (exact duplicate-group identity)."""
    sid = uuid.uuid4()
    with psycopg.connect(world.url, autocommit=True) as c:
        subject, normalized, module, start, end, day = c.execute(
            "SELECT subject, normalized_subject, module, scheduled_start, scheduled_end, session_date "
            "FROM public.lecture_sessions WHERE lecture_id = %s", (world.lecture_id,)).fetchone()
        # A second CONFIDENTLY RESOLVED event (its own meeting, confirmed organizer) makes the group
        # ambiguous. downstream_ready=false keeps its own tracks idle: the fake recording graph serves
        # one recording set for every meeting, so a running sibling could claim the test lecture's
        # file first (027's one-owner rule) - correct, but it would make this test order-dependent.
        state = (dict(calendar_mapping_status="RESOLVED", discovery_status="READY", meeting_id=f"MTG-{sid.hex[:12]}",
                      downstream_ready=False, organizer_validation_status="ORGANIZER_ID_CONFIRMED",
                      meeting_lookup_user_id=LOOKUP_USER, meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
                 if with_meeting else
                 dict(calendar_mapping_status="ONLINE_MEETING_NOT_FOUND", discovery_status="REVIEW",
                      downstream_ready=False))
        insert(c, "lecture_sessions", lecture_id=sid, calendar_user_upn=MARKER, calendar_event_id=f"evt-{sid}",
               join_url=private_url(thread=f"19:meeting_P0FDUP{sid.hex[:20]}@thread.v2"), subject=subject,
               normalized_subject=normalized, module=module, scheduled_start=start, scheduled_end=end,
               session_date=day, group_match_status="MATCHED", **state)
    return str(sid)


def _force_import_job(world, lecture_id):
    with psycopg.connect(world.url, autocommit=True) as c:
        PublisherQueueRepository().enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(lecture_id, 1),
                                           lecture_id=lecture_id, priority=BUNNY_PRIORITY["IMPORT_VIDEO"],
                                           max_attempts=MAX_ATTEMPTS)
    world.daemon.run_once()
    return world.q("SELECT status, last_error_code FROM public.bunny_publisher_jobs WHERE lecture_id = %s "
                   "AND job_kind = 'IMPORT_VIDEO'", lecture_id)


def test_duplicate_winner_is_imported_without_waiting_for_qa_suppression(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    loser = _sibling(world, with_meeting=False)                # not suppressed: QA has not run
    world.run(rounds=10)
    assert world.ready() and len(world.bunny.fetch_calls) == 1
    with psycopg.connect(url) as c:
        winner, beaten = load_facts(c, world.lecture_id), load_facts(c, loser)
    assert (winner.duplicate_block, winner.duplicate_case, winner.duplicate_role) == (None, CASE_SUPPRESSED, "WINNER")
    assert beaten.duplicate_block == DUPLICATE_LOSER
    # The publisher only READ the decision: writing the suppression stays QA's / the operator's job.
    assert world.q("SELECT metadata -> 'duplicate_suppression' FROM public.lecture_sessions WHERE lecture_id = %s",
                   loser) == [(None,)]
    # P1: a conclusively blocked duplicate with no Teams meeting gets no recording/transcript track at all.
    assert world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s", loser) == [(0,)]
    assert world.plan()["duplicates_skipped"] == 1
    # Even a hand-made import job for the loser is refused at the gate.
    assert _force_import_job(world, loser) == [("REVIEW_REQUIRED", DUPLICATE_LOSER)]
    assert len(world.bunny.fetch_calls) == 1                   # never two recordings for one occurrence


def test_ambiguous_duplicate_group_never_reaches_bunny(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    _sibling(world, with_meeting=True)                         # two valid meetings: a human decides
    world.run(rounds=10)
    assert world.videos() == [] and world.bunny.fetch_calls == [] and not world.ready()
    assert world.plan()["duplicates_blocked"] == 1
    with psycopg.connect(url) as c:
        facts = load_facts(c, world.lecture_id)
        state = readiness(c, facts, LIBRARY_ID)
    assert (facts.duplicate_block, facts.duplicate_case) == (DUPLICATE_AMBIGUOUS, CASE_MULTIPLE_VALID_MEETINGS)
    assert DUPLICATE_AMBIGUOUS in state.blocking and state.state == "REVIEW_REQUIRED"
    assert _force_import_job(world, world.lecture_id) == [("REVIEW_REQUIRED", DUPLICATE_AMBIGUOUS)]
    assert world.bunny.fetch_calls == []
    world.clock.advance(timedelta(hours=25))                   # visible for review in the exception queue
    world.reconciler.run("LIGHT")
    assert world.q("SELECT status FROM public.bunny_publisher_alerts WHERE kind = 'DUPLICATE_BLOCKED' "
                   "AND lecture_id = %s", world.lecture_id) == [("OPEN",)]


# =========================================================================== P1: production fixes
def test_a_lecture_without_an_lms_match_still_acquires_recording_and_transcript(url):
    """AI in Project Control 2026 (2026-10-23): a real Teams lecture the LMS does not know."""
    world = _one_part_world(url, lms=False)
    world.run(rounds=8)
    assert world.q("SELECT match_status FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s AND is_current",
                   world.lecture_id) == [("NO_LMS_SESSION",)]
    tracks = {kind: status for kind, _, status, _ in world.jobs() if kind in ("ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING")
              and status == "SUCCEEDED"}
    assert tracks == {"ACQUIRE_TRANSCRIPT": "SUCCEEDED", "RESOLVE_RECORDING": "SUCCEEDED"}
    assert world.q("SELECT status FROM public.bunny_recording_discoveries WHERE lecture_id = %s",
                   world.lecture_id) == [("LOCATED",)]
    assert world.q("SELECT selection_status FROM public.lecture_transcript_selections WHERE lecture_id = %s",
                   world.lecture_id) == [("SELECTED",)]
    assert world.videos() == [] and world.bunny.fetch_calls == []          # Bunny still needs a VERIFIED match
    assert world.plan()["duplicates_skipped"] == 0


def test_a_succeeded_job_drops_its_stale_error_and_the_event_log_keeps_it(url):
    world = World(url)
    recording = world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)
    world.run(rounds=4)
    [(_, _, waiting, code)] = [j for j in world.jobs("ACQUIRE_TRANSCRIPT") if j[2] != "QUEUED"]
    assert waiting == "RETRY_WAIT" and code                                  # no transcript yet: an error code
    world.transcript(START + timedelta(minutes=1), END - timedelta(minutes=2), recording["contentCorrelationId"])
    world.clock.advance(timedelta(hours=6))
    world.run(rounds=8)
    done = [j for j in world.jobs("ACQUIRE_TRANSCRIPT") if j[2] == "SUCCEEDED"]
    assert done and all(last_error is None for _, _, _, last_error in done)
    history = world.q("SELECT e.detail::text FROM public.bunny_publisher_events e JOIN public.bunny_publisher_jobs j "
                      "USING (job_id) WHERE j.lecture_id = %s AND j.job_kind = 'ACQUIRE_TRANSCRIPT'", world.lecture_id)
    assert any(code in text for (text,) in history)                         # the earlier reason is still on record


# =========================================================================== P1: audited operator cancel
CANCEL_ACTOR, CANCEL_REASON = "Test Operator", "blocked duplicate invitation; no Teams meeting"


def _stale_job(world, lecture_id, kind="ACQUIRE_TRANSCRIPT", *, retry_wait=True):
    """A track job of the kind created before the planner skipped blocked duplicates."""
    with psycopg.connect(world.url, autocommit=True) as c:
        job, _ = PublisherQueueRepository().enqueue(c, job_kind=kind, subject_key=f"lecture:{lecture_id}",
                                                    lecture_id=lecture_id, priority=LIVE_PRIORITY,
                                                    max_attempts=MAX_ATTEMPTS)
        if retry_wait:
            c.execute("UPDATE public.bunny_publisher_jobs SET status = 'RETRY_WAIT', attempt_count = 5, "
                      "next_attempt_at = now() + interval '3 minutes', last_error_code = 'MEETING_NOT_RESOLVED' "
                      "WHERE job_id = %s", (job["job_id"],))
    return str(job["job_id"])


def _cancel(world, job_id, actor=CANCEL_ACTOR, reason=CANCEL_REASON):
    with psycopg.connect(world.url, autocommit=True) as c:
        return operations.operator_cancel(c, job_id, actor=actor, reason=reason)


def _refused(world, job_id, **kw):
    with pytest.raises(operations.OperatorActionRefused) as refusal:
        _cancel(world, job_id, **kw)
    return refusal.value.code


def _job_row(world, job_id):
    return world.q("SELECT status, finished_at IS NOT NULL, next_attempt_at, locked_by FROM public.bunny_publisher_jobs "
                   "WHERE job_id = %s", job_id)[0]


def test_operator_cancel_closes_a_blocked_duplicates_waiting_jobs_audited(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    loser = _sibling(world, with_meeting=False)
    transcript, recording = _stale_job(world, loser), _stale_job(world, loser, "RESOLVE_RECORDING", retry_wait=False)
    for job_id, previous in ((transcript, "RETRY_WAIT"), (recording, "QUEUED")):
        done = _cancel(world, job_id)
        assert done["status"] == "CANCELLED" and done["lecture_id"] == loser
        assert _job_row(world, job_id) == ("CANCELLED", True, None, None)
        [(actor, detail)] = world.q("SELECT actor, detail FROM public.bunny_publisher_events WHERE job_id = %s "
                                    "AND event = 'OPERATOR_CANCEL'", job_id)
        assert actor == CANCEL_ACTOR and detail["reason"] == CANCEL_REASON and detail["previous_status"] == previous
        assert (detail["duplicate_case"], detail["duplicate_role"]) == (CASE_SUPPRESSED, "SUPPRESSIBLE_DUPLICATE")
    # Never reclaimed, never recreated; the lecture, its job history and the winner are untouched.
    world.clock.advance(timedelta(hours=2))
    world.run(rounds=4)
    assert world.q("SELECT job_kind, status FROM public.bunny_publisher_jobs WHERE lecture_id = %s ORDER BY job_kind",
                   loser) == [("ACQUIRE_TRANSCRIPT", "CANCELLED"), ("RESOLVE_RECORDING", "CANCELLED")]
    assert world.q("SELECT count(*) FROM public.lecture_sessions WHERE lecture_id = %s", loser) == [(1,)]
    assert world.ready() and len(world.bunny.fetch_calls) == 1
    assert _refused(world, transcript) == "JOB_NOT_CANCELLABLE"                     # already terminal


def test_operator_cancel_refuses_running_leased_and_finished_jobs(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    loser = _sibling(world, with_meeting=False)
    job_id = _stale_job(world, loser, retry_wait=False)
    with psycopg.connect(url, autocommit=True) as c:
        claimed = PublisherQueueRepository().claim(c, "worker-x", ["ACQUIRE_TRANSCRIPT"])
    assert str(claimed["job_id"]) == job_id
    assert _refused(world, job_id) == "JOB_NOT_CANCELLABLE" and _job_row(world, job_id)[0] == "RUNNING"
    with psycopg.connect(url, autocommit=True) as c:
        PublisherQueueRepository().succeed(c, job_id, "worker-x")
    assert _refused(world, job_id) == "JOB_NOT_CANCELLABLE" and _job_row(world, job_id)[0] == "SUCCEEDED"
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE event = 'OPERATOR_CANCEL'") == [(0,)]


def test_operator_cancel_refuses_legitimate_and_ambiguous_lectures_and_needs_actor_and_reason(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    world.run(rounds=1)
    legit = _stale_job(world, world.lecture_id, "RESOLVE_RECORDING")             # resolved meeting
    assert _refused(world, legit) == "MEETING_RESOLVED"
    _sibling(world, with_meeting=True)                                           # a second valid meeting
    unresolved_member = _sibling(world, with_meeting=False)                       # ... makes the group ambiguous
    ambiguous = _stale_job(world, unresolved_member)
    assert _refused(world, ambiguous) == "NOT_A_BLOCKED_DUPLICATE"
    loner = World(url, lms=False, start=START + timedelta(days=2), end=END + timedelta(days=2))   # no group at all
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.lecture_sessions SET meeting_id = NULL, downstream_ready = false, "
                  "calendar_mapping_status = 'ONLINE_MEETING_NOT_FOUND', discovery_status = 'REVIEW' "
                  "WHERE lecture_id = %s", (loner.lecture_id,))
    assert _refused(world, _stale_job(world, loner.lecture_id)) == "NOT_A_BLOCKED_DUPLICATE"
    loser_world = _one_part_world(url)
    _confirm_organizer(loser_world)
    blocked = _stale_job(loser_world, _sibling(loser_world, with_meeting=False))
    assert _refused(loser_world, blocked, actor=" ") == "ACTOR_AND_REASON_REQUIRED"
    assert _refused(loser_world, blocked, reason="") == "ACTOR_AND_REASON_REQUIRED"
    assert _job_row(loser_world, blocked)[0] == "RETRY_WAIT"
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE event = 'OPERATOR_CANCEL'") == [(0,)]


def test_operator_cancel_and_a_worker_claim_never_both_win(url):
    world = _one_part_world(url)
    _confirm_organizer(world)
    loser = _sibling(world, with_meeting=False)
    queue = PublisherQueueRepository()
    # 1. A worker's claim holds the row: the cancel is refused at once, never waits, changes nothing.
    first = _stale_job(world, loser, retry_wait=False)
    worker = psycopg.connect(url)
    try:
        claimed = queue.claim(worker, "worker-a", ["ACQUIRE_TRANSCRIPT"])        # transaction still open
        assert str(claimed["job_id"]) == first
        assert _refused(world, first) == "JOB_BUSY"
    finally:
        worker.rollback()
        worker.close()
    assert _job_row(world, first)[0] == "QUEUED"
    # 2. The cancel holds the row: a claim skips it, and after the cancel it is never claimable.
    operator = psycopg.connect(url)
    try:
        assert str(queue.lock_job(operator, first)["job_id"]) == first           # operator's lock, txn open
        with psycopg.connect(url, autocommit=True) as c:
            assert queue.claim(c, "worker-b", ["ACQUIRE_TRANSCRIPT"]) is None    # SKIP LOCKED
    finally:
        operator.rollback()
        operator.close()
    assert _cancel(world, first)["status"] == "CANCELLED"
    with psycopg.connect(url, autocommit=True) as c:
        assert queue.claim(c, "worker-c", ["ACQUIRE_TRANSCRIPT"]) is None
        assert not queue.enqueue_once(c, job_kind="ACQUIRE_TRANSCRIPT", subject_key=f"lecture:{loser}",
                                      lecture_id=loser, priority=LIVE_PRIORITY, max_attempts=MAX_ATTEMPTS)


# =========================================================================== P0-G: db-smoke
def test_db_smoke_passes_on_a_direct_session_and_writes_nothing(url):
    from app.publisher.smoke import SMOKE_KEY, run_db_smoke
    report = run_db_smoke(str(url))
    assert report["ok"] is True, report
    assert report["distinct_backends_over_statements"]["backends"] == 1
    assert report["checks"]["write_probe_refused"] and report["checks"]["b_is_refused_while_a_holds"]
    assert report["migrations_024_028_applied"] is True                    # the module applies them
    with psycopg.connect(url) as c:                                         # nothing left behind
        assert c.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s::bigint::oid",
                         (SMOKE_KEY,)).fetchone()[0] == 0
        assert c.execute("SELECT to_regclass('pg_temp._publisher_smoke_write_probe')").fetchone()[0] is None
    assert "127.0.0.1" not in json.dumps(report) and "postgresql://" not in json.dumps(report)
