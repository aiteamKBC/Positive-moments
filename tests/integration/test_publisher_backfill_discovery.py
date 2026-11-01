"""
Historical Backfill from Teams source discovery, against the isolated test database (2026-10-29).

Ranges here have ZERO publisher or Lecture Intelligence records for their Teams lectures: the preview must find
them in Teams (read-only), and the run must register them through the daily publisher's own discovery
(LectureDiscoveryService + CalendarDiscovery, the same identity and conflict-tolerant upsert) and take them
through the same LMS matching, recording selection, Bunny import, encoding and captions as daily work.
Graph (calendar, meetings, recordings, transcripts), Aptem, the LMS and Bunny are fakes; no socket is opened.

The fake calendar answers like calendarView: every event OVERLAPPING the asked Cairo day, so an event that
crosses midnight is returned for two days and must be counted once, on its own Cairo date.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

from app.common.time import cairo_day_utc_window
from app.graph.meetings import DISCOVERY_MAILBOX_CONTEXT, JOIN_URL_OID_MATCHES_CONTEXT, ORGANIZER_ID_CONFIRMED, RESOLVED
from app.lectures.models import CalendarDiscoveryBatch, CalendarEvent, MeetingResolution
from app.lectures.service import LectureDiscoveryService
from app.db.repositories.publisher_backfill import EXECUTE, PREVIEW, BackfillRepository
from app.publisher import backfill
from app.publisher.backfill import HistoricalBackfill
from app.publisher.planner import CalendarDiscovery, ConflictTolerantLectureRepository
from tests.integration.test_publisher_backfill import (  # noqa: F401 - fixtures
    _runs_removed_first, backfill_db, historical, preview as stored_preview,
)
from tests.integration.test_publisher_delivery import act
from tests.integration.test_publisher_runtime import END, MARKER, START, _isolated, url  # noqa: F401
from tests.integration.seeding import graph_id
from tests.unit.lms_fakes import occurrence, private_url
from tests.unit.publisher_fakes import LIBRARY_ID
from tests.unit.recording_fakes import LOOKUP_USER
from tests.unit.test_service import Runs
from tests.unit.transcript_fakes import vtt

D1, D2 = START.date() + timedelta(days=1), START.date() + timedelta(days=2)      # 2030-07-17 / 18: no records
PRODUCTION = ("lecture_sessions", "bunny_publisher_jobs", "bunny_videos", "bunny_lms_match_evaluations",
              "bunny_recording_discoveries", "lecture_discovery_runs")


# =========================================================================== Teams, as Graph would answer
class Calendar:
    calendar_user_upn = MARKER                     # committed_cleanup purges every lecture it creates

    def __init__(self):
        self.events, self.fail, self.asked = [], set(), []

    def discover_calendar_events(self, target_date):
        self.asked.append(target_date)
        if target_date in self.fail:
            from app.common.errors import PlatformError
            raise PlatformError("CALENDAR_QUERY_ERROR", "synthetic calendar outage")
        lo, hi = cairo_day_utc_window(target_date)
        overlapping = [e for e in self.events if e.meeting_start < hi and e.meeting_end > lo]
        return CalendarDiscoveryBatch(len(overlapping), overlapping)


class Meetings:
    def resolve(self, url, calendar_organizer_address=None):
        return MeetingResolution(RESOLVED, meeting_id=meeting_id(url), meeting_organizer_user_id=LOOKUP_USER,
                                 organizer_validation=ORGANIZER_ID_CONFIRMED, meeting_lookup_user_id=LOOKUP_USER,
                                 meeting_lookup_context_source=DISCOVERY_MAILBOX_CONTEXT,
                                 join_url_oid_hint_status=JOIN_URL_OID_MATCHES_CONTEXT)


class Groups:
    def __init__(self, calendar):
        self.calendar = calendar

    def load_active_groups(self, connection):
        return sorted({e.subject for e in self.calendar.events} | {"Backfill Other Active Group"})   # never empty


def meeting_id(url):
    return f"MTG-{uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:16]}"


class Teams:
    """The Teams side of a world: calendar events, their meetings, recordings and transcripts, and LMS sessions."""

    def __init__(self, world):
        self.world, self.calendar = world, Calendar()
        service = LectureDiscoveryService(calendar=self.calendar, meetings=Meetings(), aptem_repository=Groups(self.calendar),
                                          lecture_repository=ConflictTolerantLectureRepository(), run_repository=Runs(),
                                          qa_validation_repository=None)
        world.planner.calendar = CalendarDiscovery(world.config, service=service,
                                                   aptem_connect=lambda: contextlib.nullcontext(object()))

    def lecture(self, key, start, minutes=120, *, lms=True, recordings=((1, -2),), transcript=True, leads=None):
        """
        One Teams lecture. `recordings`: (start offset, end offset) minutes - the longest is the lecture.
        `leads`: seconds each MP4 is named before its recording (default 35). Every MP4 carries the Teams
        source facet of its call and the meeting thread.
        """
        subject = f"Backfill Teams {key}"
        end = start + timedelta(minutes=minutes)
        thread = f"19:meeting_BF{key}{uuid.uuid4().hex[:6]}@thread.v2"
        url = private_url(thread=thread)
        self.calendar.events.append(CalendarEvent(f"evt-bf-{key}-{uuid.uuid4().hex[:6]}", f"ical-bf-{key}", subject,
                                                  start, end, "UTC", url, calendar_organizer_address="Cohort@example.invalid"))
        if lms:
            self.world.lms.occurrences.append(occurrence(url=url, start=start, minutes=minutes))
        for n, (a, b) in enumerate(recordings):
            r_start, r_end = start + timedelta(minutes=a), end + timedelta(minutes=b)
            correlation = f"corr-{key}-{n}"
            recording = self.world.rgraph.recording(r_start, r_end, call_id=f"call-{key}-{n}", correlation=correlation,
                                                    meeting_id=meeting_id(url))
            self.world.rgraph.recorded_file(recording, lead=(leads or {}).get(n, 35), subject=subject, thread=thread)
            if transcript:
                tid = graph_id(uuid.uuid4().int % 90000 + 10000)
                self.world.tgraph.add(tid, start=r_start, end=r_end, call_id=f"call-{key}-{n}", correlation=correlation,
                                      content=vtt(max(int((r_end - r_start).total_seconds() // 60) - 5, 1), label=key),
                                      meeting_id=meeting_id(url))
        return subject


def world_with_teams(url, **config):
    # Its own stored lecture is on 2030-07-16; the publisher starts after D2, so the DAILY publisher never plans
    # or discovers any day used here - only the historical backfill does.
    world = historical(url, start_date=D2 + timedelta(days=3), **config)
    world.clock.advance(timedelta(days=3))               # D1 / D2 have ended
    return world, Teams(world)


def task_for(world, **overrides):
    if overrides:
        world.config = world.config.__class__(**{**world.config.__dict__, **overrides})
        calendar = world.planner.calendar
        world.daemon = world._daemon()
        world.planner.calendar = calendar
    world.backfill = HistoricalBackfill(config=world.config, connect=lambda: psycopg.connect(world.url, prepare_threshold=None),
                                        planner=world.planner, clock=world.clock)
    world.daemon.backfill = world.backfill
    return world.backfill


def counts(world):
    return {t: world.q(f"SELECT count(*) FROM public.{t}")[0][0] for t in PRODUCTION}


def request_preview(world, date_from, date_to):
    with psycopg.connect(world.url, autocommit=True) as c:
        with c.transaction():
            return BackfillRepository().create(c, date_from=date_from, date_to=date_to, library_id=LIBRARY_ID,
                                               requested_by="ops-test", mode=PREVIEW)


def finish_preview(world, task, run, ticks=20):
    for _ in range(ticks):
        out = task.run()
        if out.get("preview", {}).get("status") in ("COMPLETED", "INCOMPLETE"):
            break
    with psycopg.connect(world.url) as c:
        repository = BackfillRepository()
        return repository.get(c, run["run_id"]), [i["detail"] for i in repository.items(c, run["run_id"])]


def confirm(world, preview_run, rows):
    """What the operations API's start does once its checks pass."""
    with psycopg.connect(world.url, autocommit=True) as c:
        with c.transaction():
            return BackfillRepository().create(
                c, date_from=preview_run["date_from"], date_to=preview_run["date_to"], library_id=LIBRARY_ID,
                requested_by="ops-test", fingerprint=preview_run["preview_fingerprint"],
                summary=preview_run["preview_summary"], items=backfill.run_items({"lectures": rows}), mode=EXECUTE,
                preview_run_id=preview_run["run_id"])


def items(world, run_id):
    with psycopg.connect(world.url) as c:
        return {str(i["lecture_id"]): i for i in BackfillRepository().items(c, run_id)}


def registered(world, subject):
    return world.q("SELECT lecture_id::text, session_date FROM public.lecture_sessions WHERE subject = %s", subject)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


# =========================================================================== A: zero existing records
def test_a_preview_finds_teams_lectures_with_zero_records_and_writes_nothing_to_production(url):
    world, teams = world_with_teams(url)
    teams.lecture("A1", at(D1, 7))
    teams.lecture("A2", at(D1, 11))
    teams.lecture("A3", at(D2, 8), lms=False)
    task = task_for(world)
    before = counts(world)
    run = request_preview(world, D1, D2)
    preview_run, rows = finish_preview(world, task, run)
    assert counts(world) == before                                  # no lecture, job, match, discovery-run row
    assert world.bunny.fetch_calls == [] and world.rgraph.calls == []   # recordings are the run's job
    assert preview_run["status"] == "COMPLETED"
    summary = preview_run["preview_summary"]
    assert (summary["teams_discovered"], summary["new"], summary["existing"], summary["eligible"]) == (3, 3, 0, 2)
    by_subject = {r["subject"]: r for r in rows}
    assert {r["origin"] for r in rows} == {backfill.TEAMS_NEW}
    assert by_subject["Backfill Teams A1"]["category"] == backfill.READY_TO_QUEUE
    assert by_subject["Backfill Teams A1"]["verification"] == {"teams": "LIVE", "lms": "LIVE_DRY_RUN",
                                                              "recording": "TO_BE_VERIFIED_AT_RUN"}
    assert by_subject["Backfill Teams A3"]["category"] == backfill.MISSING_LMS_MATCH
    assert {d["status"] for d in preview_run["discovery"].values()} == {"COMPLETE"}
    assert not any("https://" in str(r) for r in rows)              # no join URL, no meeting id kept


def test_a_and_f_a_run_registers_the_teams_lectures_and_publishes_them_through_the_daily_pipeline(url):
    world, teams = world_with_teams(url)
    teams.lecture("F1", at(D1, 7))
    teams.lecture("F2", at(D1, 11))
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)
    for subject in ("Backfill Teams F1", "Backfill Teams F2"):
        [(lid, day)] = registered(world, subject)                   # exactly one canonical lecture each
        assert day == D1 and lid in {r["lecture_id"] for r in rows}  # the identity the preview showed
        assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
        assert world.q("SELECT match_status FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s "
                       "AND is_current", lid) == [("VERIFIED",)]
        assert world.q("SELECT import_status, encoding_status FROM public.bunny_videos WHERE lecture_id = %s", lid) \
            == [("IDENTIFIED", "VERIFIED")]
        assert world.q("SELECT count(*) FROM public.bunny_caption_publications WHERE lecture_id = %s "
                       "AND publication_status = 'VERIFIED'", lid) == [(1,)]
        act(world, "release", lecture_id=lid)                       # LMS-ready delivery, the audited release
        assert world.q("SELECT count(*) FROM public.bunny_videos WHERE lecture_id = %s AND visibility_state = "
                       "'READY_FOR_LMS'", lid) == [(1,)]
    assert len(world.bunny.fetch_calls) == 2
    assert world.q("SELECT status FROM public.bunny_backfill_runs WHERE run_id = %s", run["run_id"]) == [("COMPLETED",)]


# =========================================================================== B: a mixed range
def test_b_a_mixed_range_skips_resumes_discovers_and_blocks(url):
    world, teams = world_with_teams(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))      # the stored lecture of 07-16
    teams.lecture("NEW", at(D1, 7))                                           # only in Teams
    teams.lecture("MULTI", at(D1, 11), recordings=((0, -117), (5, 3)))        # 3-min false start + the lecture
    teams.lecture("NOLMS", at(D2, 8), lms=False)                             # no LMS session
    task = task_for(world)
    # First, the stored lecture is published and released by an earlier run.
    first = confirm(world, *(lambda p: (p, [r for r in p_rows(world, p)]))(
        finish_preview(world, task, request_preview(world, START.date(), START.date()))[0]))
    world.run(rounds=16)
    act(world, "release")
    assert world.q("SELECT status FROM public.bunny_backfill_runs WHERE run_id = %s", first["run_id"]) == [("COMPLETED",)]
    fetched = len(world.bunny.fetch_calls)
    # The mixed range: published (07-16), Teams-only with one / several recordings, and no LMS session.
    preview_run, rows = finish_preview(world, task, request_preview(world, START.date(), D2))
    by_subject = {r["subject"]: r for r in rows}
    assert by_subject[world.q("SELECT subject FROM public.lecture_sessions WHERE lecture_id = %s",
                              world.lecture_id)[0][0]]["category"] == backfill.ALREADY_PUBLISHED
    assert by_subject["Backfill Teams NEW"]["origin"] == backfill.TEAMS_NEW
    assert by_subject["Backfill Teams NOLMS"]["category"] == backfill.MISSING_LMS_MATCH
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)
    states = {r["subject"]: items(world, run["run_id"])[r["lecture_id"]]["status"] for r in rows}
    assert states["Backfill Teams NEW"] == "COMPLETED" and states["Backfill Teams MULTI"] == "COMPLETED"
    assert states["Backfill Teams NOLMS"] == "BLOCKED"
    [(multi, _)] = registered(world, "Backfill Teams MULTI")
    assert world.q("SELECT recording_part_index FROM public.bunny_videos WHERE lecture_id = %s", multi) == [(2,)]
    assert len(world.bunny.fetch_calls) == fetched + 2               # the published one was not re-imported
    assert world.q("SELECT count(*) FROM public.bunny_videos WHERE lecture_id = %s", world.lecture_id) == [(1,)]


def p_rows(world, preview_run):
    with psycopg.connect(world.url) as c:
        return [i["detail"] for i in BackfillRepository().items(c, preview_run["run_id"])]


def test_b_an_incomplete_lecture_resumes_at_its_persisted_stage_without_a_second_upload(url):
    world, _ = world_with_teams(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    task = task_for(world)
    first = confirm(world, *finish_preview(world, task, request_preview(world, START.date(), START.date())))
    task.run()                                            # discovery, LMS, tracks
    for _ in range(3):
        world.clock.advance(timedelta(minutes=5))
        world.settle()
        while world.daemon.work_once("w") != "IDLE":
            pass
    task.run()                                            # import queued
    world.clock.advance(timedelta(minutes=5))
    world.settle()
    world.daemon.work_once("w")                           # the import only: the video exists in Bunny
    assert world.videos()[0][1] in ("SUBMITTED", "IDENTIFIED")
    with psycopg.connect(url, autocommit=True) as c:      # the old run is cancelled
        BackfillRepository().request_cancel(c, first["run_id"], requested_by="ops-test")
    task.run()
    for _ in range(2):                                    # the submitted import is identified (in-flight work finishes)
        world.clock.advance(timedelta(minutes=5))
        world.settle()
        while world.daemon.work_once("w") != "IDLE" and world.videos()[0][1] != "IDENTIFIED":
            pass
    with psycopg.connect(url, autocommit=True) as c:      # ... and the encoding / caption work is lost
        c.execute("UPDATE public.bunny_publisher_jobs SET status = 'CANCELLED', finished_at = now(), "
                  "next_attempt_at = NULL WHERE lecture_id = %s AND status IN ('QUEUED', 'RETRY_WAIT')",
                  (world.lecture_id,))
    assert world.videos()[0][1:3] == ("IDENTIFIED", "NOT_STARTED") or world.videos()[0][2] != "VERIFIED"
    preview_run, rows = finish_preview(world, task, request_preview(world, START.date(), START.date()))
    [row] = rows
    assert row["category"] == backfill.RESUME_REQUIRED and row["origin"] == backfill.STORED_ONLY
    confirm(world, preview_run, rows)
    world.run(rounds=14)
    assert world.videos()[0][1:3] == ("IDENTIFIED", "VERIFIED")
    assert len(world.bunny.fetch_calls) == 1                         # resumed, never re-uploaded


# =========================================================================== C: idempotency
def test_c_the_same_and_overlapping_ranges_twice_create_nothing_twice(url):
    world, teams = world_with_teams(url)
    teams.lecture("C1", at(D1, 7))
    teams.lecture("C2", at(D2, 7))
    task = task_for(world)
    confirm(world, *finish_preview(world, task, request_preview(world, D1, D1)))
    world.run(rounds=16)
    confirm(world, *finish_preview(world, task, request_preview(world, D1, D2)))     # overlaps D1
    world.run(rounds=16)
    confirm(world, *finish_preview(world, task, request_preview(world, D1, D2)))     # the same range again
    world.run(rounds=6)
    for subject in ("Backfill Teams C1", "Backfill Teams C2"):
        [(lid, _)] = registered(world, subject)
        assert world.q("SELECT count(*) FROM public.bunny_videos WHERE lecture_id = %s", lid) == [(1,)]
        assert world.q("SELECT count(*) FROM public.bunny_caption_publications WHERE lecture_id = %s", lid) == [(1,)]
    assert len(world.bunny.fetch_calls) == 2
    assert world.q("SELECT count(*) FROM public.bunny_backfill_runs WHERE mode = 'EXECUTE' AND status = 'COMPLETED'") == [(3,)]


def test_c_one_preview_and_one_run_at_a_time(url):
    world, teams = world_with_teams(url)
    teams.lecture("P1", at(D1, 7))
    assert request_preview(world, D1, D1) is not None and request_preview(world, D1, D2) is None


# =========================================================================== D: recovery
def test_d_a_restart_during_discovery_continues_where_it_stopped(url):
    world, teams = world_with_teams(url)
    teams.lecture("R1", at(D1, 7))
    teams.lecture("R2", at(D2, 7))
    task = task_for(world, backfill_discovery_days=1)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D2))
    run = confirm(world, preview_run, rows)
    assert task.run()["status"] == "DISCOVERING"                # one day per tick
    assert registered(world, "Backfill Teams R1") and not registered(world, "Backfill Teams R2")
    task = task_for(world)                                     # a restarted service
    world.run(rounds=16)
    assert all(i["status"] == "COMPLETED" for i in items(world, run["run_id"]).values())
    discovered = world.q("SELECT discovery FROM public.bunny_backfill_runs WHERE run_id = %s", run["run_id"])[0][0]
    assert {d["status"] for d in discovered.values()} == {"COMPLETE"} and len(world.bunny.fetch_calls) == 2


def test_d_a_day_that_cannot_be_discovered_makes_the_preview_incomplete(url):
    world, teams = world_with_teams(url)
    teams.lecture("X1", at(D1, 7))
    teams.calendar.fail.add(D2)
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D2))
    assert preview_run["status"] == "INCOMPLETE"
    assert preview_run["preview_summary"]["incomplete_days"] == [D2.isoformat()]
    assert preview_run["discovery"][D2.isoformat()]["attempts"] == backfill.DAY_ATTEMPTS
    assert teams.calendar.asked.count(D2) == backfill.DAY_ATTEMPTS


def test_d_a_failed_day_at_run_blocks_its_lectures_instead_of_skipping_them_silently(url):
    world, teams = world_with_teams(url)
    teams.lecture("Y1", at(D1, 7))
    teams.lecture("Y2", at(D2, 7))
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D2))
    run = confirm(world, preview_run, rows)
    teams.calendar.fail.add(D2)                                # Teams is unavailable for D2 during the run
    world.run(rounds=16)
    by_subject = {r["subject"]: items(world, run["run_id"])[r["lecture_id"]] for r in rows}
    assert by_subject["Backfill Teams Y1"]["status"] == "COMPLETED"
    assert (by_subject["Backfill Teams Y2"]["status"], by_subject["Backfill Teams Y2"]["reason"]) == (
        "BLOCKED", "TEAMS_DISCOVERY_FAILED")


# =========================================================================== E: dates
def test_e_an_event_crossing_cairo_midnight_belongs_to_one_date_only(url):
    world, teams = world_with_teams(url)
    teams.lecture("LATE", at(D1, 20, 30), minutes=150)          # 23:30 Cairo on D1 until 02:00 on D2
    teams.lecture("EARLY", at(D2, 21, 30))                      # 00:30 Cairo on the day AFTER D2
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D2, D2))
    assert rows == [] and preview_run["preview_summary"]["teams_discovered"] == 0
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    # The business date is the Cairo date of the start, never the UTC date or the asked window.
    assert [(r["subject"], r["session_date"]) for r in rows] == [("Backfill Teams LATE", D1.isoformat())]


# =========================================================================== a false start next to the lecture
def test_a_historical_lecture_with_a_false_start_imports_only_the_lecture_recording(url):
    world, teams = world_with_teams(url)
    subject = teams.lecture("R1", at(D1, 11), recordings=((-5, -124), (-3, 19)))   # ~1 min, then ~2 h 22 min
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)
    [(lid, _)] = registered(world, subject)
    assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
    assert world.q("SELECT status, (metadata -> 'recording_selection' ->> 'part_index')::int FROM "
                   "public.bunny_recording_discoveries WHERE lecture_id = %s", lid) == [("LOCATED", 2)]
    assert world.q("SELECT recording_part_index, import_status, encoding_status FROM public.bunny_videos "
                   "WHERE lecture_id = %s", lid) == [(2, "IDENTIFIED", "VERIFIED")]
    assert len(world.bunny.fetch_calls) == 1
    world.run(rounds=4)
    assert len(world.bunny.fetch_calls) == 1


def test_a_historical_msp_shaped_lecture_imports_only_the_8533_s_recording(url):
    world, teams = world_with_teams(url)
    # a 59 s false start, then the lecture 21 min 36 s later on another call (the 2026-10-30 MSP shape)
    subject = teams.lecture("M1", at(D1, 11), recordings=((-3 - 26 / 60, -122 - 27 / 60), (19 + 9 / 60, 41 + 22 / 60)))
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)
    [(lid, _)] = registered(world, subject)
    assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
    [(index, duration)] = world.q("SELECT recording_part_index, round(source_duration_seconds) FROM "
                                  "public.bunny_recording_sources WHERE lecture_id = %s", lid)
    assert (index, duration) == (1, 8533)
    assert world.q("SELECT recording_part_index, import_status, encoding_status FROM public.bunny_videos "
                   "WHERE lecture_id = %s", lid) == [(1, "IDENTIFIED", "VERIFIED")]
    assert len(world.bunny.fetch_calls) == 1


def test_a_historical_lecture_whose_mp4_is_named_at_the_call_start_is_proven_and_imported(url):
    world, teams = world_with_teams(url)
    # the 2026-10-30 MSP files: the false start named 71.7 s early, the lecture named 1,303.9 s early
    subject = teams.lecture("P1", at(D1, 11), recordings=((-3 - 26 / 60, -122 - 27 / 60), (19 + 9 / 60, 41 + 22 / 60)),
                            leads={0: 71.7, 1: 1303.9})
    task = task_for(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)
    [(lid, _)] = registered(world, subject)
    assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
    assert world.q("SELECT recording_part_index, round(source_duration_seconds), media_origin FROM "
                   "public.bunny_recording_sources WHERE lecture_id = %s", lid) == [(1, 8533, "graph_created_origin")]
    assert world.q("SELECT import_status, encoding_status FROM public.bunny_videos WHERE lecture_id = %s", lid) == [
        ("IDENTIFIED", "VERIFIED")]
    world.run(rounds=4)
    assert len(world.bunny.fetch_calls) == 1
