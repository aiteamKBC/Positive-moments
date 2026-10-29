"""
Historical Backfill, end to end, against the isolated test database (2026-10-29).

The REAL daemon, planner, queue, transcript / recording / Bunny workers and delivery rule, with fake Graph,
LMS and Bunny (no socket). Every lecture here is HISTORICAL: before the publisher's start date, so the daily
publisher never plans it (test_lectures_before_the_start_date_are_backfill_scope_never_planned_or_failed) -
only an operator-confirmed run does, through the same pipeline.
"""
from __future__ import annotations

import threading
from datetime import timedelta

import psycopg
import pytest

from app.bunny.identity import build_publisher_title, recording_identity, video_record_id
from app.db.repositories.bunny_publisher import BunnyVideoRepository
from app.db.repositories.publisher_backfill import BackfillRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher import backfill, monitor
from app.publisher.backfill import HistoricalBackfill
from app.publisher.recording_mapping import RULE_LONGEST_RECORDING
from app.publisher.handlers import HISTORICAL_PRIORITY, HISTORICAL_TAG, LIVE_PRIORITY, part_subject_key, subject_key
from tests.integration.publisher_db import BACKFILL_MIGRATION, remove_backfill_migration
from tests.integration.test_publisher_delivery import act
from tests.integration.test_publisher_runtime import (  # noqa: F401 - fixtures
    END, START, World, _isolated, url,
)
from tests.unit.lms_fakes import occurrence, private_url
from tests.unit.publisher_fakes import LIBRARY_ID

DAY = START.date()
COUNTED = ("bunny_publisher_jobs", "bunny_publisher_events", "bunny_videos", "bunny_caption_publications",
           "bunny_lms_match_evaluations", "bunny_recording_discoveries", "bunny_backfill_runs", "bunny_backfill_items")


@pytest.fixture(scope="module")
def backfill_db(url):
    with psycopg.connect(url, autocommit=True) as admin:
        remove_backfill_migration(admin)
        admin.execute(BACKFILL_MIGRATION.read_text(encoding="utf-8"))
    yield url
    with psycopg.connect(url, autocommit=True) as admin:
        remove_backfill_migration(admin)


@pytest.fixture(autouse=True)
def _runs_removed_first(backfill_db, _isolated):
    """Runs and items reference lectures: removed before the runtime module purges them."""
    yield
    with psycopg.connect(backfill_db, autocommit=True) as admin:
        admin.execute("DELETE FROM public.bunny_backfill_items")
        admin.execute("DELETE FROM public.bunny_backfill_runs")


# =========================================================================== helpers
def historical(url, **config):
    """A world whose lecture is before PUBLISHER_START_DATE: daily planning never touches it."""
    return World(url, start_date=(END + timedelta(days=1)).date(), **config)


def full_part(world):
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))


def other_lecture(world, start, end, *, day=DAY, lms=True, recorded=True) -> str:
    """Another lecture of the world with its OWN meeting-scoped recording and LMS session."""
    lid = world.lecture(start=start, end=end)
    with psycopg.connect(world.url, autocommit=True) as c:
        c.execute("UPDATE public.lecture_sessions SET session_date = %s WHERE lecture_id = %s", (day, lid))
        meeting = c.execute("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s", (lid,)).fetchone()[0]
    if lms:
        world.lms.occurrences.append(occurrence(url=private_url(thread=world.thread), start=start,
                                                minutes=int((end - start).total_seconds() // 60)))
    if recorded:
        recording = world.rgraph.recording(start + timedelta(minutes=1), end - timedelta(minutes=2),
                                           call_id=f"call-{lid[:8]}", correlation=f"corr-{lid[:8]}", meeting_id=meeting)
        world.rgraph.recorded_file(recording, lead=35)
    return lid


def scoped_part(world):
    """The world's own lecture's recording, listed for its meeting only."""
    meeting = world.q("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s", world.lecture_id)[0][0]
    recording = world.rgraph.recording(START + timedelta(minutes=1), END - timedelta(minutes=2), call_id="call-1",
                                       correlation="corr-main", meeting_id=meeting)
    world.rgraph.recorded_file(recording, lead=35)


def connect(world):
    return lambda: psycopg.connect(world.url, prepare_threshold=None)


def coordinator(world, **overrides):
    if overrides:
        world.config = world.config.__class__(**{**world.config.__dict__, **overrides})
        world.daemon = world._daemon()
    world.backfill = HistoricalBackfill(config=world.config, connect=connect(world), planner=world.planner,
                                        clock=world.clock)
    world.daemon.backfill = world.backfill
    return world.backfill


def preview(world, date_from=DAY, date_to=DAY):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        facts = monitor.load_facts(c, date_from, date_to, LIBRARY_ID)
        recordings = backfill.load_recordings(c, [f["lecture_id"] for f in facts])
        views = {f["lecture_id"]: monitor.resolve(c, f, library_id=LIBRARY_ID) for f in facts
                 if monitor.needs_resolution(f)}
        return backfill.build_preview(facts, views, recordings, date_from=date_from, date_to=date_to,
                                      library_id=LIBRARY_ID, now=world.clock())


def start(world, shown, *, actor="ops-test"):
    with psycopg.connect(world.url, autocommit=True) as c:
        with c.transaction():
            return BackfillRepository().create(
                c, date_from=shown["date_from"], date_to=shown["date_to"], library_id=LIBRARY_ID, requested_by=actor,
                reason="test", fingerprint=shown["fingerprint"], summary={"eligible": shown["summary"]["eligible"]},
                items=backfill.run_items(shown))


def items(world, run_id):
    with psycopg.connect(world.url) as c:
        return {str(i["lecture_id"]): i for i in BackfillRepository().items(c, run_id)}


def run_row(world, run_id):
    with psycopg.connect(world.url) as c:
        return BackfillRepository().get(c, run_id)


def counts(world):
    return {t: world.q(f"SELECT count(*) FROM public.{t}")[0][0] for t in COUNTED}


def jobs_of(world, lecture_id):
    return world.q("SELECT job_kind, subject_key, status, priority, payload ->> %s FROM public.bunny_publisher_jobs "
                   "WHERE lecture_id = %s ORDER BY created_at", HISTORICAL_TAG, lecture_id)


def drain(world, rounds=1):
    """Workers only (no scheduler tick): every due job until the queue is idle."""
    for _ in range(rounds):
        world.clock.advance(timedelta(minutes=5))
        world.settle()
        for _ in range(100):
            if world.daemon.work_once("backfill-test:w0") == "IDLE":
                break


# =========================================================================== preview
def test_preview_is_read_only_and_cannot_enqueue_import_or_release(url):
    world = historical(url)
    full_part(world)
    before = counts(world)
    shown = preview(world)
    assert counts(world) == before and world.bunny.fetch_calls == []
    [row] = shown["lectures"]
    assert row["category"] == backfill.READY_TO_QUEUE and row["proposed_action"] == "QUEUE"
    assert row["data_source"] == "DATABASE" and shown["summary"]["eligible"] == 1
    with psycopg.connect(world.url) as c:                        # the guarantee is PostgreSQL's, not ours
        c.read_only = True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            PublisherQueueRepository().enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(
                world.lecture_id, 1), lecture_id=world.lecture_id)


# =========================================================================== the run
def test_a_historical_run_publishes_through_the_existing_pipeline_at_historical_priority(url):
    world = historical(url)
    full_part(world)
    world.run(rounds=3)                                          # daily publishing alone: never touched
    assert world.jobs() == [] and world.bunny.fetch_calls == []
    coordinator(world)
    run = start(world, preview(world))
    world.run(rounds=14)
    assert world.ready()
    [(part, imported, encoding, _, _, retired, _)] = world.videos()
    assert (part, imported, encoding, retired) == (1, "IDENTIFIED", "VERIFIED", False)
    assert len(world.bunny.fetch_calls) == 1 and world.bunny.delete_calls == []
    assert world.captions()[0][0] == "VERIFIED"
    jobs = jobs_of(world, world.lecture_id)
    assert {k for k, *_ in jobs} >= {"ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING", "IMPORT_VIDEO", "POLL_ENCODING",
                                     "PUBLISH_CAPTION"}
    for kind, _, _, priority, tag in jobs:
        if kind in ("IMPORT_VIDEO", "POLL_ENCODING", "PUBLISH_CAPTION") or tag:
            assert tag == str(run["run_id"]) and priority == HISTORICAL_PRIORITY[kind], kind
    [item] = items(world, run["run_id"]).values()
    assert (item["status"], item["reason"]) == ("COMPLETED", "VERIFIED_READY_AWAITING_MANUAL_RELEASE")
    assert run_row(world, run["run_id"])["status"] == "COMPLETED"
    events = world.q("SELECT event FROM public.bunny_publisher_events WHERE event LIKE 'HISTORICAL_BACKFILL_%%' "
                     "ORDER BY event_id")
    assert [e for (e,) in events] == ["HISTORICAL_BACKFILL_STARTED", "HISTORICAL_BACKFILL_COMPLETED"]
    # Released by an operator: the next preview says so and a rerun has nothing to queue - no second upload.
    act(world, "release")
    again = preview(world)
    assert again["lectures"][0]["category"] == backfill.ALREADY_PUBLISHED and again["summary"]["eligible"] == 0
    assert again["summary"]["estimated_imports"]["upper_bound"] == 0


def test_the_longest_recording_is_the_only_one_imported(url):
    world = historical(url)
    world.part(START, START + timedelta(minutes=3), call_id="call-a")                  # a false start
    world.part(START + timedelta(minutes=5), END + timedelta(minutes=3), call_id="call-b")
    coordinator(world)
    start(world, preview(world))
    world.run(rounds=14)
    live = world.videos(live=True)
    assert [v[0] for v in live] == [2] and len(world.bunny.fetch_calls) == 1
    shown = preview(world)["lectures"][0]["recording"]
    assert shown["verified_count"] == 2 and shown["selected_part"] == 2
    assert shown["selection_rule"] == RULE_LONGEST_RECORDING and shown["selected_duration_seconds"] > 3600


def test_only_lectures_of_the_range_are_processed_by_their_cairo_date(url):
    world = historical(url)
    full_part(world)
    late_previous = world.lecture(start=START - timedelta(hours=11, minutes=30), end=START - timedelta(hours=10))
    next_day = world.lecture(start=START + timedelta(days=1), end=END + timedelta(days=1))
    after_midnight = world.lecture(start=START + timedelta(hours=13, minutes=30), end=START + timedelta(hours=14, minutes=30))
    with psycopg.connect(url, autocommit=True) as c:
        # 21:30 UTC the day before = 00:30 Cairo (UTC+3): this date's lecture; 22:30 UTC = the next Cairo date.
        c.execute("UPDATE public.lecture_sessions SET session_date = %s WHERE lecture_id = %s", (DAY, late_previous))
        c.execute("UPDATE public.lecture_sessions SET session_date = %s WHERE lecture_id = %s",
                  (DAY + timedelta(days=1), after_midnight))
    coordinator(world)
    shown = preview(world)
    assert {r["lecture_id"] for r in shown["lectures"]} == {world.lecture_id, late_previous}
    run = start(world, shown)
    world.run(rounds=14)
    assert set(items(world, run["run_id"])) == {world.lecture_id, late_previous}
    for outside in (next_day, after_midnight):
        assert jobs_of(world, outside) == []
    assert items(world, run["run_id"])[late_previous]["status"] == "BLOCKED"        # no LMS session of its own
    assert world.ready()


def test_one_active_run_at_a_time_even_for_simultaneous_starts(url):
    world = historical(url)
    full_part(world)
    shown = preview(world)
    barrier, results = threading.Barrier(2), []

    def go():
        barrier.wait()
        results.append(start(world, shown))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is not None for r in results) == [False, True]
    assert world.q("SELECT count(*) FROM public.bunny_backfill_runs")[0][0] == 1
    assert start(world, shown) is None                           # still one while it is active


def test_overlapping_reruns_converge_on_the_same_rows(url):
    world = historical(url)
    full_part(world)
    coordinator(world)
    first = start(world, preview(world))
    world.run(rounds=14)
    assert run_row(world, first["run_id"])["status"] == "COMPLETED"
    rerun = preview(world)
    assert rerun["summary"]["eligible"] == 0 and rerun["lectures"][0]["category"] == backfill.ALREADY_PROCESSING
    second = start(world, rerun)                                  # allowed once the first has finished
    world.run(rounds=3)
    assert run_row(world, second["run_id"])["status"] == "COMPLETED"
    assert len(world.bunny.fetch_calls) == 1
    assert world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s AND job_kind = "
                   "'IMPORT_VIDEO'", world.lecture_id) == [(1,)]


# =========================================================================== daily priority
def test_daily_work_is_claimed_before_historical_work(url):
    world = historical(url)
    daily = other_lecture(world, START + timedelta(days=5), END + timedelta(days=5), day=DAY + timedelta(days=5),
                          lms=False, recorded=False)
    queue = PublisherQueueRepository()
    with psycopg.connect(url, autocommit=True) as c:
        queue.enqueue(c, job_kind="RESOLVE_RECORDING", subject_key=subject_key(world.lecture_id),
                      lecture_id=world.lecture_id, priority=HISTORICAL_PRIORITY["RESOLVE_RECORDING"],
                      payload={HISTORICAL_TAG: "run"})
        queue.enqueue(c, job_kind="ACQUIRE_TRANSCRIPT", subject_key=subject_key(daily), lecture_id=daily,
                      priority=LIVE_PRIORITY)
        first = queue.claim(c, "w", ["RESOLVE_RECORDING", "ACQUIRE_TRANSCRIPT"])
    assert str(first["lecture_id"]) == daily


def test_a_waiting_daily_import_defers_historical_imports(url):
    world = historical(url)
    scoped_part(world)
    daily = other_lecture(world, START + timedelta(days=5), END + timedelta(days=5), day=DAY + timedelta(days=5),
                          lms=False, recorded=False)
    backfill_task = coordinator(world)
    run = start(world, preview(world))
    backfill_task.run()                                          # LMS match + tracks
    drain(world, rounds=3)                                       # recording located
    with psycopg.connect(url, autocommit=True) as c:
        waiting, _ = PublisherQueueRepository().enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(daily, 1),
                                                       lecture_id=daily, priority=80)
    out = backfill_task.run()
    assert out["import_budget"] == 0 and out["imports_deferred"] == 1
    assert [k for k, *_ in jobs_of(world, world.lecture_id) if k == "IMPORT_VIDEO"] == []
    assert items(world, run["run_id"])[world.lecture_id]["reason"] == "IMPORT_WAITING_FOR_SLOT"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.bunny_publisher_jobs SET status = 'CANCELLED', finished_at = now() WHERE job_id = %s",
                  (waiting["job_id"],))
    out = backfill_task.run()
    assert out["jobs_created"] == 1 and [k for k, *_ in jobs_of(world, world.lecture_id) if k == "IMPORT_VIDEO"]


# =========================================================================== restart, cancel, retry
def test_a_restarted_service_resumes_the_active_run_without_repeating_work(url):
    world = historical(url)
    full_part(world)
    coordinator(world)
    run = start(world, preview(world))
    world.run(rounds=3)
    assert run_row(world, run["run_id"])["status"] == "RUNNING"
    world.daemon = world._daemon()                               # a new process: nothing kept in memory
    coordinator(world)
    world.run(rounds=14)
    assert run_row(world, run["run_id"])["status"] == "COMPLETED" and world.ready()
    assert len(world.bunny.fetch_calls) == 1
    assert world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s AND job_kind = "
                   "'IMPORT_VIDEO'", world.lecture_id) == [(1,)]


def test_cancel_stops_pending_work_and_lets_in_flight_work_finish(url):
    world = historical(url)
    scoped_part(world)
    second = other_lecture(world, START - timedelta(hours=5), END - timedelta(hours=5))       # same day, ended
    lectures = {world.lecture_id, second}
    task = coordinator(world, backfill_batch_size=1, backfill_max_imports=1)
    run = start(world, preview(world))
    task.run()                                                   # one lecture started: its tracks
    drain(world, rounds=3)
    task.run()                                                   # its import queued; the other one started
    drain(world, rounds=1)                                       # first import submitted; the other located

    def imported():
        return {lid: [v[1:3] for v in world.q(
            "SELECT recording_part_index, import_status, encoding_status FROM public.bunny_videos "
            "WHERE lecture_id = %s AND retired_at IS NULL", lid)] for lid in lectures}

    in_flight = [lid for lid, rows in imported().items() if rows]
    assert len(in_flight) == 1 and imported()[in_flight[0]][0][0] in ("SUBMITTED", "IDENTIFIED")
    [waiting] = lectures - set(in_flight)
    task.run()                                                   # budget used by the in-flight import, or queued
    with psycopg.connect(url, autocommit=True) as c:
        assert BackfillRepository().request_cancel(c, run["run_id"], requested_by="ops-test") is not None
    out = task.run()
    assert out["status"] == "CANCELLED" and run_row(world, run["run_id"])["status"] == "CANCELLED"
    assert {i["status"] for i in items(world, run["run_id"]).values()} == {"CANCELLED"}
    assert [s for k, _, s, *_ in jobs_of(world, waiting) if k == "IMPORT_VIDEO"] in ([], ["CANCELLED"])
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE event = 'HISTORICAL_BACKFILL_CANCELLED'") == [(1,)]
    drain(world, rounds=8)                                       # the in-flight lecture finishes normally
    assert imported()[in_flight[0]][0] == ("IDENTIFIED", "VERIFIED")
    assert imported()[waiting] == [] and len(world.bunny.fetch_calls) == 1
    assert task.run()["status"] == "IDLE"


def test_retry_reopens_a_blocked_lecture_and_rematches_the_lms(url):
    world = historical(url, lms=False)
    full_part(world)
    coordinator(world)
    run = start(world, preview(world))
    world.run(rounds=3)
    item = items(world, run["run_id"])[world.lecture_id]
    assert (item["status"], item["reason"]) == ("BLOCKED", "LMS_NO_LMS_SESSION")
    assert run_row(world, run["run_id"])["status"] == "COMPLETED" and world.bunny.fetch_calls == []
    world.add_occurrence()                                       # the LMS session is created afterwards
    with psycopg.connect(url, autocommit=True) as c:
        out = backfill.retry_items(c, run["run_id"], [], actor="ops-test", reason="LMS session added")
    assert out["lectures_reopened"] == 1 and out["lms_rematch"] is True
    world.run(rounds=14)
    assert items(world, run["run_id"])[world.lecture_id]["status"] == "COMPLETED" and world.ready()
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE event = 'HISTORICAL_BACKFILL_RETRY' "
                   "AND actor = 'ops-test'") == [(1,)]


def test_cancel_cancels_only_this_runs_imports_that_never_reached_bunny(url):
    world = historical(url)
    scoped_part(world)
    in_bunny = other_lecture(world, START - timedelta(hours=5), END - timedelta(hours=5))
    daily = other_lecture(world, START - timedelta(hours=8), END - timedelta(hours=8), lms=False, recorded=False)
    task = coordinator(world)
    run = start(world, preview(world))
    tag = {HISTORICAL_TAG: str(run["run_id"])}
    queue = PublisherQueueRepository()
    with psycopg.connect(url, autocommit=True) as c:
        unsent, _ = queue.enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(world.lecture_id, 1),
                                  lecture_id=world.lecture_id, priority=8, payload=tag)
        submitted, _ = queue.enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(in_bunny, 1),
                                     lecture_id=in_bunny, priority=8, payload=tag)
        # The import worker's own intent row, already past PENDING (a submission is under way).
        videos = BunnyVideoRepository()
        row = videos.create(c, record_id=video_record_id(LIBRARY_ID, "drive-x", "item-x"), lecture_id=in_bunny, part=1,
                            drive="drive-x", item="item-x", identity=recording_identity("drive-x", "item-x"),
                            library=LIBRARY_ID, file_name="x.mp4",
                            title=build_publisher_title("Synthetic", DAY, "OCC-SYN", recording_identity("drive-x", "item-x")),
                            size=1, duration=60.0, metadata={})
        assert videos.transition(c, row["bunny_video_record_id"], expect={"import_status": "PENDING"},
                                 import_status="SUBMITTING", attempt_count=1, import_submitted_at=world.clock())
        untagged, _ = queue.enqueue(c, job_kind="IMPORT_VIDEO", subject_key=part_subject_key(daily, 1), lecture_id=daily,
                                    priority=80)
        BackfillRepository().request_cancel(c, run["run_id"], requested_by="ops-test")
    out = task.run()
    assert out["status"] == "CANCELLED" and out["imports_cancelled"] == 1
    status = dict(world.q("SELECT job_id::text, status FROM public.bunny_publisher_jobs"))
    assert status[str(unsent["job_id"])] == "CANCELLED"
    assert status[str(submitted["job_id"])] == "QUEUED" and status[str(untagged["job_id"])] == "QUEUED"
    assert world.q("SELECT actor FROM public.bunny_publisher_events WHERE event = 'OPERATOR_CANCEL'") == [("ops-test",)]


def test_the_schema_command_installs_029_once_and_refuses_a_partial_schema(url):
    with psycopg.connect(url, autocommit=True) as c:
        assert backfill.schema_status(c) == {"tables": {"bunny_backfill_runs": True, "bunny_backfill_items": True},
                                             "installed": True, "runs": 0}
        assert backfill.install_schema(c)["status"] == "ALREADY_INSTALLED"
        remove_backfill_migration(c)
        assert backfill.schema_status(c)["installed"] is False
        applied = backfill.install_schema(c)
        assert (applied["status"], applied["installed"]) == ("APPLIED", True)
        c.execute("DROP TABLE public.bunny_backfill_items")
        assert backfill.install_schema(c)["status"] == "REFUSED_PARTIAL_SCHEMA"
        remove_backfill_migration(c)
        c.execute(BACKFILL_MIGRATION.read_text(encoding="utf-8"))
