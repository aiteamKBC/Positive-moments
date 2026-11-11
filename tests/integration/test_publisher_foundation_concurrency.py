"""
P0-A concurrency: real parallel connections against COMMITTED schema.

Two connections cannot see DDL inside one rolled-back transaction, so this
module applies the proposed migrations 024-026 for real on the isolated test
database and ALWAYS removes them again afterwards with their own rollback
scripts (after deleting the rows it made) - leaving the database exactly as
tools/bootstrap_integration_db.py built it. A leftover from a killed run is
cleaned up the same way before the module starts.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.lms_matches import LmsMatchRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.lms.matching import CONFLICT, VERIFIED, MatchConfig, match_lecture
from tests.integration.publisher_db import MARKER_UPN, MIGRATIONS, NEW_TABLES, ROLLBACKS, applied, lecture_row
from tests.unit.lms_fakes import START, lecture, occurrence, snapshot
from tools.integration_db_guard import assert_isolated

CONFIG = MatchConfig()
DELETE_ORDER = ("bunny_publisher_events", "bunny_publisher_jobs", "bunny_publisher_heartbeats", "bunny_lms_occurrence_coverage",
                "bunny_lms_occurrence_links", "bunny_lms_match_evaluations", "bunny_caption_current",
                "bunny_caption_publications", "bunny_videos")


def _teardown(connection):
    present = set(applied(connection))
    for table in DELETE_ORDER:
        if table in present:
            connection.execute(f"DELETE FROM public.{table}")
    connection.execute("DELETE FROM public.lecture_sessions WHERE calendar_user_upn = %s", (MARKER_UPN,))
    for path in ROLLBACKS:
        connection.execute(path.read_text(encoding="utf-8"))
    assert applied(connection) == []


@pytest.fixture(scope="module")
def url():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    admin = psycopg.connect(url, autocommit=True)
    try:
        assert_isolated(admin)
        assert "test" in admin.execute("SELECT current_database()").fetchone()[0]
        _teardown(admin)                                   # a killed earlier run
        for path in MIGRATIONS:
            admin.execute(path.read_text(encoding="utf-8"))
        assert applied(admin) == list(NEW_TABLES)
        yield url
    finally:
        try:
            _teardown(admin)
        finally:
            admin.close()


def _committed_lecture(url, start=START) -> str:
    with psycopg.connect(url) as connection:
        assert_isolated(connection)
        lecture_id = lecture_row(connection, start)
        connection.commit()
    return lecture_id


def _parallel(n, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait(timeout=30)
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_two_lectures_racing_for_one_occurrence_yield_one_owner(url):
    lectures = [_committed_lecture(url) for _ in range(2)]
    occ = occurrence()

    def record(i):
        facts = lecture(lecture_id=lectures[i])
        with psycopg.connect(url) as connection:
            outcome = LmsMatchRepository().record(connection, match_lecture(facts, snapshot(occ), CONFIG),
                                                  lecture_scheduled_start=facts.scheduled_start)
            connection.commit()
        return outcome.status
    assert sorted(_parallel(2, record)) == sorted([VERIFIED, CONFLICT])
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM public.bunny_lms_occurrence_links "
                                  "WHERE is_current AND lms_occurrence_id = %s", (occ.occurrence_id,)).fetchone() == (1,)


def test_same_lecture_evaluated_concurrently_keeps_one_current_decision(url):
    lecture_id = _committed_lecture(url)
    occ = occurrence()
    facts = lecture(lecture_id=lecture_id)

    def record(_):
        with psycopg.connect(url) as connection:
            outcome = LmsMatchRepository().record(connection, match_lecture(facts, snapshot(occ), CONFIG),
                                                  lecture_scheduled_start=facts.scheduled_start)
            connection.commit()
        return outcome.changed
    assert sorted(_parallel(4, record)) == [False, False, False, True]
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*), sum(evaluation_count) FROM public.bunny_lms_match_evaluations "
                                  "WHERE lecture_id = %s", (lecture_id,)).fetchone() == (1, 4)


def test_parallel_workers_claim_each_job_exactly_once(url):
    queue = PublisherQueueRepository()
    tag = uuid.uuid4().hex[:8]
    with psycopg.connect(url) as connection:
        for n in range(12):
            queue.enqueue(connection, job_kind="POLL_ENCODING", subject_key=f"claim-{tag}-{n}")
        connection.commit()

    def drain(i):
        claimed = []
        with psycopg.connect(url, autocommit=True) as connection:
            while (job := queue.claim(connection, f"worker-{i}", ["POLL_ENCODING"])) is not None:
                claimed.append(job["job_id"])
                assert queue.succeed(connection, job["job_id"], f"worker-{i}")
        return claimed
    results = _parallel(4, drain)
    everything = [job for claimed in results for job in claimed]
    assert len(everything) == 12 and len(set(everything)) == 12


def test_concurrent_enqueue_of_one_subject_creates_one_live_job(url):
    queue = PublisherQueueRepository()
    subject = f"enqueue-{uuid.uuid4().hex[:8]}"

    def enqueue(_):
        with psycopg.connect(url, autocommit=True) as connection:
            job, created = queue.enqueue(connection, job_kind="MATCH_LMS", subject_key=subject)
        return job["job_id"], created
    results = _parallel(6, enqueue)
    assert len({job for job, _ in results}) == 1 and sum(created for _, created in results) == 1
