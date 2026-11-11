"""
Historical Backfill with its second source, the QA database (2026-11-02), against the isolated test database.

The fake Teams world of test_publisher_backfill_discovery plus REAL rows in public.qa_doctors_sessions: every day
reads both; one lecture seen in both is one lecture (one registry row, one Bunny import); a QA lecture Teams no
longer lists is kept and reported with what it lacks, never registered; the QA table is never written. A
historical lecture with no transcript anywhere is imported, encoded and released under the daily policy.
"""
from __future__ import annotations

import base64

import uuid
from datetime import timedelta

import psycopg
import pytest

from app.publisher import backfill
from app.publisher import historical_sources as sources
from app.publisher.auto_release import ACTOR, AutoReleaser
from tests.integration.seeding import legacy_fingerprint, seed_legacy_row
from tests.integration.test_publisher_auto_release import Security
from tests.integration.test_publisher_backfill import _runs_removed_first, backfill_db  # noqa: F401 - fixtures
from tests.integration.test_publisher_backfill_discovery import (
    D1, D2, PRODUCTION, at, confirm, counts, finish_preview, items, meeting_id, registered, request_preview,
    task_for, world_with_teams,
)
from tests.integration.test_publisher_delivery import production
from tests.integration.test_publisher_runtime import _isolated, url  # noqa: F401 - fixtures

QA_PREFIX = "bf-qa-"


@pytest.fixture(autouse=True)
def _qa_rows_removed(url):
    yield
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DELETE FROM public.qa_doctors_sessions WHERE session_id LIKE %s", (QA_PREFIX + "%",))


def seed_qa(world, day, meeting, *, n, subject="QA Lecture", **extra):
    with psycopg.connect(world.url, autocommit=True) as c:
        seed_legacy_row(c, f"{QA_PREFIX}{n}-{uuid.uuid4().hex[:8]}", meeting_id=meeting, day=day, subject=subject,
                        trainer="QA trainer", **extra)


def teams_meeting(teams, subject):
    return meeting_id(next(e.join_url for e in teams.calendar.events if e.subject == subject))


def gone_meeting(n):
    """A Graph onlineMeeting id of a course whose calendar events are gone."""
    return base64.b64encode(f"1*org-gone*0**19:meeting_GONE{n}{uuid.uuid4().hex[:6]}@thread.v2".encode()).decode()


def qa_table(world):
    with psycopg.connect(world.url) as c:
        return legacy_fingerprint(c)


# =========================================================================== preview
def test_the_preview_reads_teams_and_qa_every_day_and_counts_each_lecture_once(url):
    world, teams = world_with_teams(url)
    q1 = teams.lecture("Q1", at(D1, 7))
    seed_qa(world, D1, teams_meeting(teams, q1), n=1, subject=q1)
    seed_qa(world, D1, teams_meeting(teams, q1), n=2, subject=q1)          # a duplicate QA record of Q1
    seed_qa(world, D1, gone_meeting(1), n=3, subject="Finished Course")   # Teams no longer lists it
    task = task_for(world)
    before, qa_before = counts(world), qa_table(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D2))

    assert counts(world) == before and qa_table(world) == qa_before          # read only, both sources
    assert preview_run["status"] == "COMPLETED"
    s = preview_run["preview_summary"]
    assert (s["total"], s["teams_discovered"], s["qa_records"], s["qa_matched"], s["qa_only"]) == (2, 1, 3, 2, 1)
    [lecture] = rows                                                        # items: publisher lectures only
    assert lecture["subject"] == q1 and lecture["sources"] == ["TEAMS", "QA"] and lecture["qa"]["records"] == 2
    [qa_only] = s["qa_only_lectures"]
    assert qa_only["subject"] == "Finished Course" and qa_only["code"] == sources.NO_LMS_SESSION
    day1, day2 = s["per_day"][D1.isoformat()], s["per_day"][D2.isoformat()]
    assert (day1["teams_status"], day1["qa_status"], day1["outcome"]) == ("COMPLETE", "COMPLETE", "DISCOVERED")
    assert day2["outcome"] == backfill.COMPLETED_EMPTY_DAY                  # both sources answered: zero
    assert "MSo" not in str(preview_run["discovery"]) and QA_PREFIX not in str(preview_run["discovery"])


def test_a_qa_outage_makes_the_day_partial_and_the_preview_unconfirmable(url, monkeypatch):
    world, teams = world_with_teams(url)
    teams.lecture("P1", at(D1, 7))
    task = task_for(world)

    def outage(connection, date_from, date_to):
        raise psycopg.OperationalError("qa database unavailable")
    monkeypatch.setattr(sources, "load_records", outage)
    preview_run, _ = finish_preview(world, task, request_preview(world, D1, D1))
    day = preview_run["discovery"][D1.isoformat()]
    assert preview_run["status"] == "INCOMPLETE" and day["status"] == backfill.PARTIAL
    assert day["teams"]["status"] == "COMPLETE" and day["qa"]["status"] == "FAILED"
    assert day["attempts"] == backfill.DAY_ATTEMPTS


# =========================================================================== run
def test_a_run_processes_a_lecture_seen_in_both_sources_once_and_keeps_the_qa_only_one_blocked(url):
    world, teams = world_with_teams(url)
    r1 = teams.lecture("R1", at(D1, 7))
    seed_qa(world, D1, teams_meeting(teams, r1), n=1, subject=r1)
    seed_qa(world, D1, gone_meeting(2), n=2, subject="Finished Course")
    task = task_for(world)
    qa_before = qa_table(world)
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=16)

    [(lid, day)] = registered(world, r1)                                     # one canonical lecture
    assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
    assert world.q("SELECT count(*) FROM public.bunny_videos WHERE lecture_id = %s", lid) == [(1,)]
    assert len(world.bunny.fetch_calls) == 1
    assert registered(world, "Finished Course") == []                       # never registered, never invented
    with psycopg.connect(world.url) as c:
        stored = backfill.BackfillRepository().get(c, run["run_id"])
        [qa_only] = backfill.run_qa_only(c, stored)
    assert qa_only["code"] == sources.NO_LMS_SESSION and qa_only["lecture_id"] is None
    assert stored["discovery"][D1.isoformat()]["qa"]["records"] == 2      # the run read QA itself
    assert qa_table(world) == qa_before                                     # the QA table is never written

    # the same range again: nothing is registered, imported or queued twice
    again_preview, again_rows = finish_preview(world, task, request_preview(world, D1, D1))
    assert {r["lecture_id"]: r["category"] for r in again_rows}[lid] in (backfill.ALREADY_PUBLISHED,
                                                                         backfill.ALREADY_PROCESSING)
    assert again_preview["preview_summary"]["eligible"] == 0
    assert len(registered(world, r1)) == 1 and len(world.bunny.fetch_calls) == 1


# =========================================================================== no transcript anywhere
def test_a_historical_lecture_without_any_transcript_is_imported_and_released_like_a_daily_one(url):
    world, teams = world_with_teams(url, **production(auto_release=True))
    world.security = Security()
    t1 = teams.lecture("T1", at(D1, 7), transcript=False)                  # Graph has no transcript either
    seed_qa(world, D1, teams_meeting(teams, t1), n=1, subject=t1)          # and QA holds no transcript text
    task = task_for(world)
    world.daemon.auto_releaser = AutoReleaser(config=world.config, security=world.security,
                                              connect=lambda: psycopg.connect(world.url, prepare_threshold=None))
    preview_run, rows = finish_preview(world, task, request_preview(world, D1, D1))
    run = confirm(world, preview_run, rows)
    world.run(rounds=20)

    [(lid, _)] = registered(world, t1)
    assert world.q("SELECT import_status, encoding_status, visibility_state FROM public.bunny_videos "
                   "WHERE lecture_id = %s", lid) == [("IDENTIFIED", "VERIFIED", "READY_FOR_LMS")]
    assert world.q("SELECT count(*) FROM public.bunny_caption_publications WHERE lecture_id = %s", lid) == [(0,)]
    assert world.q("SELECT actor FROM public.bunny_publisher_events WHERE lecture_id = %s "
                   "AND event = 'PUBLICATION_RELEASED'", lid) == [(ACTOR,)]                # released once
    assert world.q("SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s "
                   "AND job_kind = 'ACQUIRE_TRANSCRIPT' AND status = 'SUCCEEDED'", lid) == [(0,)]   # never faked
    assert items(world, run["run_id"])[lid]["status"] == "COMPLETED"
    # Past the 72 h window Teams' "no transcript" is the confirmed answer; the released video is untouched.
    world.run(rounds=4, step=timedelta(hours=24))
    [(status, code)] = world.q("SELECT status, last_error_code FROM public.bunny_publisher_jobs WHERE lecture_id = %s "
                               "AND job_kind = 'ACQUIRE_TRANSCRIPT' ORDER BY created_at DESC LIMIT 1", lid)
    assert status == "REVIEW_REQUIRED" and code.startswith("DEADLINE_PASSED:")
    assert world.q("SELECT visibility_state FROM public.bunny_videos WHERE lecture_id = %s", lid) == [("READY_FOR_LMS",)]
    assert world.q("SELECT count(*) FROM public.bunny_publisher_events WHERE lecture_id = %s "
                   "AND event = 'PUBLICATION_RELEASED'", lid) == [(1,)]
