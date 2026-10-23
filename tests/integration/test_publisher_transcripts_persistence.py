"""
P0-B: the publisher transcript worker against the isolated test database.

The worker COMMITS by design (independent of QA), so these tests use committed
data: every lecture and artifact they create is removed by
committed_cleanup.purge_lectures, and proposed migration 026 (needed by the
queue handler) is applied for the module and removed with its rollback script.
Microsoft Graph is the fake client under the real TranscriptGateway.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.db.repositories.ready_lectures import ReadyLectureRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_selections import TranscriptSelectionRepository, TranscriptSelectionRunRepository
from app.common.hashing import content_sha256, provider_transcript_identity
from app.orchestration.locks import LectureLockManager, SchedulerCycleLock
from app.publisher.handlers import TranscriptJobHandler
from app.publisher.transcripts import (
    DEFERRED, NOT_APPLICABLE, PERMANENT, READY, RETRY, REVIEW, WAITING, TranscriptPolicy, TranscriptWorker,
)
from app.transcripts.selection_service import TranscriptSelectionService
from tests.integration.committed_cleanup import purge_lectures
from tests.integration.publisher_db import PROPOSED, applied, reset_proposed_migrations
from tests.integration.seeding import graph_id, insert
from tests.unit.transcript_fakes import END, START, FakeGraph, http, network, vtt
from tools.integration_db_guard import assert_isolated

MARKER = "p0b-transcripts@example.invalid"
QUEUE_MIGRATION = PROPOSED / "026_create_publisher_queue_tables.sql"
QUEUE_ROLLBACK = PROPOSED / "026_create_publisher_queue_tables.rollback.sql"
AFTER = END + timedelta(hours=1)
FAST = TranscriptPolicy(lock_timeout_ms=500)


def _cleanup(admin):
    if "bunny_publisher_jobs" in applied(admin):
        admin.execute("DELETE FROM public.bunny_publisher_events")
        admin.execute("DELETE FROM public.bunny_publisher_jobs")
        admin.execute("DELETE FROM public.bunny_publisher_heartbeats")
        admin.execute(QUEUE_ROLLBACK.read_text(encoding="utf-8"))
    ids = [r[0] for r in admin.execute("SELECT lecture_id FROM public.lecture_sessions WHERE calendar_user_upn = %s",
                                       (MARKER,)).fetchall()]
    purge_lectures(admin, ids, with_artifacts=True)


@pytest.fixture(scope="module")
def url():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    reset_proposed_migrations(url)                         # a killed earlier run
    admin = psycopg.connect(url, autocommit=True)
    try:
        assert_isolated(admin)
        _cleanup(admin)
        assert "bunny_publisher_jobs" not in applied(admin)
        admin.execute(QUEUE_MIGRATION.read_text(encoding="utf-8"))
        yield url
    finally:
        try:
            _cleanup(admin)
        finally:
            admin.close()


def _connect(url):
    return lambda: psycopg.connect(url, prepare_threshold=None)


def _lecture(url, *, cancelled=False, suppressed=False, ready=True, start=START, end=END) -> str:
    lecture_id = uuid.uuid4()
    with psycopg.connect(url, autocommit=True) as c:
        assert_isolated(c)
        insert(c, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn=MARKER,
               calendar_event_id=f"evt-{lecture_id}", join_url=f"https://example.invalid/{lecture_id}",
               subject="Synthetic P0-B Lecture", normalized_subject="synthetic p0-b lecture",
               module="Synthetic P0-B Lecture", scheduled_start=start, scheduled_end=end,
               session_date=start.date(), calendar_mapping_status="RESOLVED", group_match_status="MATCHED",
               discovery_status="READY", meeting_id=f"MTG-{lecture_id.hex[:12]}" if ready else None,
               downstream_ready=ready, is_cancelled=cancelled, meeting_lookup_user_id="organizer-oid",
               meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID",
               metadata={"duplicate_suppression": {"winner_lecture_id": "x"}} if suppressed else {})
    return str(lecture_id)


def _tid():
    return graph_id(uuid.uuid4().int % 90000 + 10000)


def _worker(url, graph, *, clock=AFTER, policy=FAST):
    return TranscriptWorker(connect=_connect(url), gateway=graph.gateway(), graph_client=graph,
                            clock=lambda: clock, policy=policy)


def _versions(url, transcript_id):
    artifact = provider_transcript_identity(provider="MICROSOFT_GRAPH", provider_transcript_id=transcript_id)
    with psycopg.connect(url) as c:
        rows = c.execute("SELECT version_number, content_sha256 FROM public.lecture_transcript_artifact_contents "
                         "WHERE artifact_id = %s ORDER BY version_number", (artifact,)).fetchall()
        status = c.execute("SELECT artifact_status, content_version FROM public.lecture_transcript_artifacts "
                           "WHERE artifact_id = %s", (artifact,)).fetchone()
    return rows, status


def _selection(url, lecture_id):
    with psycopg.connect(url) as c:
        row = c.execute("SELECT selection_status, selected_part_count, primary_provider_transcript_id "
                        "FROM public.lecture_transcript_selections WHERE lecture_id = %s", (lecture_id,)).fetchone()
        parts = c.execute("SELECT p.provider_transcript_id, p.part_offset_ms FROM public.lecture_transcript_selection_parts p "
                          "JOIN public.lecture_transcript_selections s ON s.selection_id = p.selection_id "
                          "WHERE s.lecture_id = %s ORDER BY p.part_index", (lecture_id,)).fetchall()
    return row, parts


def _one_part(graph, *, content=None):
    tid = _tid()
    graph.add(tid, start=START + timedelta(minutes=2), end=END - timedelta(minutes=3),
              content=content if content is not None else vtt(110))
    return tid


# -- acquisition, independence, idempotency ---------------------------------------------------

def test_acquires_and_commits_without_qa_or_openai(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason, outcome.graph_calls) == (READY, "TRANSCRIPT_STORED", 2)
    versions, status = _versions(url, tid)                     # read back from a NEW connection
    assert [v[0] for v in versions] == [1] and versions[0][1] == content_sha256(vtt(110))
    assert status == ("CONTENT_STORED", 1)
    selection, parts = _selection(url, lecture_id)
    assert selection == ("SELECTED", 1, tid) and [p[0] for p in parts] == [tid]
    assert outcome.detail["parts"][0]["content_version"] == 1 and outcome.detail["parts"][0]["speaker_attribution"]
    assert outcome.next_refresh_at == END + timedelta(hours=6)


def test_committed_raw_vtt_survives_a_failing_qa_transaction(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == READY
    # A QA cycle runs its day-scoped selection in its long transaction and then fails.
    qa = psycopg.connect(url)
    try:
        TranscriptSelectionService(lecture_repository=ReadyLectureRepository(),
                                   selection_repository=TranscriptSelectionRepository(),
                                   run_repository=TranscriptSelectionRunRepository()).select_day(qa, START.date())
        qa.execute("UPDATE public.lecture_transcript_artifacts SET artifact_status = 'CONTENT_FETCH_ERROR' "
                   "WHERE provider_transcript_id = %s", (tid,))
        with pytest.raises(psycopg.errors.DivisionByZero):
            qa.execute("SELECT 1 / 0")                         # the QA transaction dies
        qa.rollback()
    finally:
        qa.close()
    versions, status = _versions(url, tid)
    assert len(versions) == 1 and status == ("CONTENT_STORED", 1)
    assert _selection(url, lecture_id)[0][0] == "SELECTED"


def test_rerun_reuses_stored_content_without_graph(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == READY
    graph.calls.clear()
    again = worker.run(lecture_id)
    assert (again.kind, again.reason, again.graph_calls) == (READY, "STORED_TRANSCRIPT_REUSED", 0)
    assert graph.calls == [] and len(_versions(url, tid)[0]) == 1


# -- occurrence correctness --------------------------------------------------------------------

def test_recurring_series_fetches_only_this_occurrence(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    last_week = _tid()
    graph.add(last_week, start=START - timedelta(days=7), end=END - timedelta(days=7), call_id="call-old",
              content=vtt(110, label="old"))
    this_week = _one_part(graph)
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == READY and outcome.detail["artifacts_listed"] == 2
    assert graph.content_calls() == [this_week]                 # last week's bytes are never downloaded
    assert _selection(url, lecture_id)[0][2] == this_week


def test_another_occurrences_transcript_is_not_proof_this_one_exists(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    graph.add(_tid(), start=START - timedelta(days=7), end=END - timedelta(days=7), content=vtt(110))
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == WAITING and outcome.reason.startswith("NO_TRANSCRIPT_FOR_OCCURRENCE")
    assert graph.content_calls() == [] and _selection(url, lecture_id)[0][0] != "SELECTED"


def test_multipart_transcript_is_selected_as_parts(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    first, second = _tid(), _tid()
    graph.add(first, start=START, end=START + timedelta(minutes=55), call_id="call-mp", content=vtt(55, label="p1"))
    graph.add(second, start=START + timedelta(minutes=60), end=END, call_id="call-mp", content=vtt(55, label="p2"))
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == READY and [p["part_index"] for p in outcome.detail["parts"]] == [1, 2]
    selection, parts = _selection(url, lecture_id)
    assert selection[0:2] == ("SELECTED", 2)
    assert [p[0] for p in parts] == [first, second] and parts[0][1] == 0 and parts[1][1] > 0


# -- not ready, Graph failures ------------------------------------------------------------------

def test_not_yet_generated_is_waiting(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    empty = _worker(url, graph).run(lecture_id)
    assert (empty.kind, empty.reason) == (WAITING, "NO_TRANSCRIPTS_AVAILABLE")
    tid = _one_part(graph, content=[http(404, "NotFound")])
    pending = _worker(url, graph).run(lecture_id)
    assert pending.kind == WAITING and _versions(url, tid)[1][0] == "TRANSCRIPT_NOT_FOUND"
    graph.content[tid] = [b"   "]                                # Graph answers, body still empty
    assert _worker(url, graph).run(lecture_id).kind == WAITING
    graph.content[tid] = [vtt(110)]
    assert _worker(url, graph).run(lecture_id).kind == READY


def test_lecture_not_ended_makes_no_graph_call(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    outcome = _worker(url, graph, clock=END - timedelta(minutes=30)).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "LECTURE_NOT_ENDED") and graph.calls == []


@pytest.mark.parametrize("error, kind", [(http(403, "Forbidden"), REVIEW), (http(429), RETRY),
                                         (http(500), RETRY), (http(503), RETRY), (network(), RETRY)])
def test_listing_failures(url, error, kind):
    lecture_id, graph = _lecture(url), FakeGraph()
    graph.listing_errors = [error]
    assert _worker(url, graph).run(lecture_id).kind == kind


def test_content_403_is_review_and_429_is_retry(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph, content=[http(403, "Forbidden")])
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == REVIEW and outcome.reason == "GRAPH_ACCESS_BLOCKED:403"
    graph.content[tid] = [http(429)]
    assert _worker(url, graph).run(lecture_id).kind == RETRY


def test_expired_token_is_refreshed_once_and_succeeds(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    _one_part(graph)
    graph.listing_errors = [http(401, "InvalidAuthenticationToken"), None]
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == READY and graph.invalidations == 1


def test_deadline_turns_waiting_into_review(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    outcome = _worker(url, graph, clock=END + timedelta(hours=73)).run(lecture_id)
    assert outcome.kind == REVIEW and outcome.reason == "DEADLINE_PASSED:NO_TRANSCRIPTS_AVAILABLE"


@pytest.mark.parametrize("kwargs, kind, reason", [
    ({"cancelled": True}, NOT_APPLICABLE, "LECTURE_CANCELLED"),
    ({"suppressed": True}, NOT_APPLICABLE, "LI_DUPLICATE"),
    ({"ready": False}, WAITING, "MEETING_NOT_RESOLVED"),
])
def test_lectures_with_nothing_to_acquire(url, kwargs, kind, reason):
    graph = FakeGraph()
    outcome = _worker(url, graph).run(_lecture(url, **kwargs))
    assert (outcome.kind, outcome.reason) == (kind, reason) and graph.calls == []


def test_unknown_lecture_is_permanent(url):
    assert _worker(url, FakeGraph()).run(str(uuid.uuid4())).kind == PERMANENT


# -- failure after storage, interruption, revisions -------------------------------------------------

def test_failure_after_one_part_is_stored_keeps_it_and_retry_completes(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    first, second = _tid(), _tid()
    graph.add(first, start=START, end=START + timedelta(minutes=55), call_id="c", content=vtt(55, label="p1"))
    graph.add(second, start=START + timedelta(minutes=60), end=END, call_id="c", content=[http(500)])
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == RETRY and outcome.reason == "GRAPH_SERVER_ERROR:500"
    assert len(_versions(url, first)[0]) == 1                   # part 1 already committed
    assert _versions(url, second)[1][0] == "CONTENT_FETCH_ERROR"   # part 2 marked, nothing invented
    graph.content[second] = [vtt(55, label="p2")]
    graph.calls.clear()
    assert _worker(url, graph).run(lecture_id).kind == READY
    assert graph.content_calls() == [second]                    # part 1 is not downloaded again


def test_interruption_mid_run_releases_the_lock_and_retry_is_safe(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    first, second = _tid(), _tid()
    graph.add(first, start=START, end=START + timedelta(minutes=55), call_id="c", content=vtt(55, label="p1"))
    graph.add(second, start=START + timedelta(minutes=60), end=END, call_id="c", content=vtt(55, label="p2"))
    worker = _worker(url, graph)
    real = worker.gateway.fetch_content
    calls = []

    def die_on_second(*args):
        calls.append(args)
        if len(calls) == 2:
            raise SystemExit("service stopped")                  # SIGTERM-style interruption mid-job
        return real(*args)
    worker.gateway.fetch_content = die_on_second
    with pytest.raises(SystemExit):
        worker.run(lecture_id)
    with psycopg.connect(url) as c:                              # the session lock went with the job
        assert LectureLockManager().try_acquire(c, lecture_id)
        LectureLockManager().release(c, lecture_id)
    worker.gateway.fetch_content = real
    assert worker.run(lecture_id).kind == READY
    assert len(_versions(url, first)[0]) == 1 and len(_versions(url, second)[0]) == 1


def test_bounded_refresh_stores_a_revision_and_a_failed_refresh_keeps_content(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)

    def at(hours, artifact_time=None):
        outcome = _worker(url, graph, clock=END + timedelta(hours=hours)).run(lecture_id)
        with psycopg.connect(url, autocommit=True) as c:          # the DB clock follows the test clock
            c.execute("UPDATE public.lecture_transcript_artifacts SET content_fetched_at = %s "
                      "WHERE provider_transcript_id = %s AND content_fetched_at > now() - interval '5 minutes'",
                      (END + timedelta(hours=hours), tid))
        return outcome
    assert at(1).reason == "TRANSCRIPT_STORED"
    assert at(3).graph_calls == 0                                 # before the 6 h point: reuse
    graph.content[tid] = [vtt(110, label="revised")]
    revised = at(7)                                                # 6 h point: one refresh, revised text
    assert revised.kind == READY and revised.detail["content_versions_created"] == 1
    versions, status = _versions(url, tid)
    assert [v[0] for v in versions] == [1, 2] and status == ("CONTENT_STORED", 2)
    assert at(10).graph_calls == 0
    graph.content[tid] = [http(500)]
    failed = at(25)                                                # 24 h point: refresh fails
    assert failed.kind == READY and failed.detail["refresh_failures"] == 1
    assert _versions(url, tid)[1] == ("CONTENT_STORED", 2)        # nothing overwritten or re-labelled
    assert at(30).graph_calls == 0 and at(24 * 30).graph_calls == 0   # bounded: never again


# -- concurrency with QA and with other workers ---------------------------------------------------

def test_progresses_while_a_qa_cycle_runs(url):
    # P0-F: QA's global cycle lock is not a reason to wait. Only a row QA is
    # actually writing defers the worker (see the uncommitted-write tests).
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    qa = psycopg.connect(url, autocommit=True)
    try:
        assert SchedulerCycleLock().try_acquire(qa)
        outcome = _worker(url, graph).run(lecture_id)
        assert outcome.kind == READY and graph.content_calls()
        assert [v[0] for v in _versions(url, tid)[0]] == [1]
    finally:
        SchedulerCycleLock().release(qa)
        qa.close()


def test_defers_while_qa_holds_the_lecture_lock(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    _one_part(graph)
    qa = psycopg.connect(url, autocommit=True)
    try:
        assert LectureLockManager().try_acquire(qa, lecture_id)
        assert _worker(url, graph).run(lecture_id).reason == "LECTURE_LOCKED"
    finally:
        LectureLockManager().release(qa, lecture_id)
        qa.close()


def test_uncommitted_qa_write_of_the_same_transcript_defers_then_is_reused(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    artifact = provider_transcript_identity(provider="MICROSOFT_GRAPH", provider_transcript_id=tid)
    raw = vtt(110)
    with psycopg.connect(url) as c:
        meeting_id = c.execute("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s",
                               (lecture_id,)).fetchone()[0]
    qa = psycopg.connect(url)                     # QA's long cycle transaction, not committed yet
    try:
        repo = TranscriptArtifactRepository()
        repo.upsert(qa, artifact, lecture_id, provider="MICROSOFT_GRAPH", provider_transcript_id=tid,
                    meeting_id=meeting_id, meeting_lookup_user_id="organizer-oid", provider_call_id="call-1",
                    content_correlation_id=None, provider_created_at=START, provider_end_at=END,
                    artifact_status="DISCOVERED", metadata={})
        repo.store_content(qa, artifact, raw_text=raw.decode(), content_sha256=content_sha256(raw),
                           content_bytes=len(raw), content_format="text/vtt", speaker_attribution=True)
        blocked = _worker(url, graph).run(lecture_id)
        # QA owns these rows until its cycle commits: the worker writes nothing.
        assert (blocked.kind, blocked.reason) == (DEFERRED, "ROW_LOCKED")
        qa.commit()                                # QA's transaction is unharmed
    finally:
        qa.close()
    graph.calls.clear()
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == READY and graph.content_calls() == []   # QA's committed bytes are reused
    assert len(_versions(url, tid)[0]) == 1


def test_qa_storing_after_the_worker_reuses_the_committed_version(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == READY
    raw = vtt(110)
    with psycopg.connect(url) as qa:
        stored = TranscriptArtifactRepository().store_content(
            qa, provider_transcript_identity(provider="MICROSOFT_GRAPH", provider_transcript_id=tid),
            raw_text=raw.decode(), content_sha256=content_sha256(raw), content_bytes=len(raw),
            content_format="text/vtt", speaker_attribution=True)
        qa.commit()
    assert stored == {"version_number": 1, "version_created": False}


def _qa_cycle_store(url, artifact, lecture_id, tid, raw, result):
    """QA's transcript write order in its cycle transaction: upsert the artifact, store, commit."""
    try:
        with psycopg.connect(url) as qa:                      # not autocommit: QA's cycle transaction
            meeting_id = qa.execute("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s",
                                    (lecture_id,)).fetchone()[0]
            repo = TranscriptArtifactRepository()
            repo.upsert(qa, artifact, lecture_id, provider="MICROSOFT_GRAPH", provider_transcript_id=tid,
                        meeting_id=meeting_id, meeting_lookup_user_id="organizer-oid", provider_call_id="call-1",
                        content_correlation_id=None, provider_created_at=START, provider_end_at=END,
                        artifact_status="DISCOVERED", metadata={})
            result["stored"] = repo.store_content(qa, artifact, raw_text=raw.decode(),
                                                  content_sha256=content_sha256(raw), content_bytes=len(raw),
                                                  content_format="text/vtt", speaker_attribution=True)
            qa.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
        result["error"] = repr(exc)


def _guarded_worker_store(url, graph, lecture_id, tid, raw, qa_raw):
    """
    The worker's guarded content transaction held OPEN while QA's cycle writes
    the same artifact: QA must wait (never fail), then see the committed result.
    """
    artifact = provider_transcript_identity(provider="MICROSOFT_GRAPH", provider_transcript_id=tid)
    worker = _worker(url, graph)
    result: dict = {}
    with psycopg.connect(url, autocommit=True) as pub:
        meeting_id = pub.execute("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s",
                                 (lecture_id,)).fetchone()[0]
        with worker._write(pub, artifacts=[artifact]):
            worker.artifacts.upsert(pub, artifact, lecture_id, provider="MICROSOFT_GRAPH", provider_transcript_id=tid,
                                    meeting_id=meeting_id, meeting_lookup_user_id="organizer-oid",
                                    provider_call_id="call-1", content_correlation_id=None, provider_created_at=START,
                                    provider_end_at=END, artifact_status="DISCOVERED", metadata={})
        with worker._write(pub, artifacts=[artifact]):
            worker.artifacts.store_content(pub, artifact, raw_text=raw.decode(), content_sha256=content_sha256(raw),
                                           content_bytes=len(raw), content_format="text/vtt",
                                           speaker_attribution=True)
            qa = threading.Thread(target=_qa_cycle_store, args=(url, artifact, lecture_id, tid, qa_raw, result))
            qa.start()
            qa.join(0.5)
            assert qa.is_alive(), "QA should be waiting for the worker's short guarded transaction"
        qa.join(15)
    assert not qa.is_alive() and "error" not in result, result
    return artifact, result["stored"]


def test_concurrent_qa_save_of_identical_content_waits_then_reuses_the_version(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid, raw = _tid(), vtt(110)
    _, stored = _guarded_worker_store(url, graph, lecture_id, tid, raw, raw)
    assert stored == {"version_number": 1, "version_created": False}       # no duplicate version
    versions, status = _versions(url, tid)
    assert [v[0] for v in versions] == [1] and status[1] == 1


def test_concurrent_qa_save_of_new_content_appends_and_keeps_the_committed_version(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid, mine, qas = _tid(), vtt(110), vtt(111)
    artifact, stored = _guarded_worker_store(url, graph, lecture_id, tid, mine, qas)
    assert stored == {"version_number": 2, "version_created": True}
    versions, status = _versions(url, tid)
    assert versions == [(1, content_sha256(mine)), (2, content_sha256(qas))] and status[1] == 2
    with psycopg.connect(url) as c:                                          # the first raw WebVTT is intact
        raw = c.execute("SELECT raw_content FROM public.lecture_transcript_artifact_contents "
                        "WHERE artifact_id = %s AND version_number = 1", (artifact,)).fetchone()[0]
    assert raw == mine.decode()


def test_a_selection_qa_is_writing_is_never_touched(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == READY
    qa = psycopg.connect(url)                       # QA's cycle has upserted this lecture's selection
    try:
        qa.execute("UPDATE public.lecture_transcript_selections SET updated_at = now() WHERE lecture_id = %s",
                   (lecture_id,))
        worker = _worker(url, graph)
        with psycopg.connect(url, autocommit=True) as pub:
            ready = worker._load(pub, lecture_id).ready
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                worker._persist_selection(pub, ready)
            assert time.monotonic() - started < 2                    # NOWAIT: no waiting behind QA
        qa.commit()
    finally:
        qa.close()


def test_parallel_workers_on_one_lecture_never_duplicate(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    tid = _one_part(graph)
    barrier, results = threading.Barrier(3), []

    def run():
        worker = _worker(url, graph)
        barrier.wait(timeout=30)
        results.append(worker.run(lecture_id).kind)
    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert READY in results and set(results) <= {READY, DEFERRED}
    assert len(_versions(url, tid)[0]) == 1
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM public.lecture_transcript_selections WHERE lecture_id = %s",
                         (lecture_id,)).fetchone()[0] == 1


# -- the queue handler, end to end -------------------------------------------------------------------

def _claim_and_handle(url, handler, worker_id="worker-1"):
    with psycopg.connect(url, autocommit=True) as c:
        job = PublisherQueueRepository().claim(c, worker_id, ["ACQUIRE_TRANSCRIPT"])
    assert job is not None
    return job, handler.handle(job, worker_id)


def _job(url, job_id):
    with psycopg.connect(url) as c:
        job = PublisherQueueRepository().job(c, job_id)
        events = c.execute("SELECT event, detail FROM public.bunny_publisher_events WHERE job_id = %s ORDER BY event_id",
                           (job_id,)).fetchall()
    return job, events


def test_handler_waiting_then_ready_then_schedules_one_bounded_refresh(url):
    lecture_id, graph = _lecture(url), FakeGraph()
    handler = TranscriptJobHandler(worker=_worker(url, graph), connect=_connect(url))
    with psycopg.connect(url, autocommit=True) as c:
        handler.enqueue(c, lecture_id)
        assert handler.enqueue(c, lecture_id)[1] is False           # one live job per lecture
    job, outcome = _claim_and_handle(url, handler)
    state, events = _job(url, job["job_id"])
    assert outcome.kind == WAITING and state["status"] == "RETRY_WAIT" and events[-1][0] == "TRANSCRIPT_WAITING"
    with psycopg.connect(url, autocommit=True) as c:                 # due now
        c.execute("UPDATE public.bunny_publisher_jobs SET next_attempt_at = now() WHERE job_id = %s", (job["job_id"],))
    _one_part(graph)
    job, outcome = _claim_and_handle(url, handler)
    state, events = _job(url, job["job_id"])
    assert outcome.kind == READY and state["status"] == "SUCCEEDED"
    assert events[-1][0] == "TRANSCRIPT_READY" and events[-1][1]["parts"][0]["content_version"] == 1
    assert "http" not in str(events)
    with psycopg.connect(url) as c:
        refresh = c.execute("SELECT status, next_attempt_at, payload, priority FROM public.bunny_publisher_jobs "
                            "WHERE subject_key = %s AND status = 'QUEUED'", (f"lecture:{lecture_id}",)).fetchall()
    assert refresh == [("QUEUED", END + timedelta(hours=6), {"refresh": True}, 20)]


@pytest.mark.parametrize("setup, status, event", [
    ("forbidden", "REVIEW_REQUIRED", "TRANSCRIPT_REVIEW_REQUIRED"),
    ("missing", "FAILED", "TRANSCRIPT_FAILED"),
    ("cancelled", "SUCCEEDED", "TRANSCRIPT_NOT_APPLICABLE"),
])
def test_handler_terminal_transitions(url, setup, status, event):
    graph = FakeGraph()
    if setup == "forbidden":
        lecture_id = _lecture(url)
        graph.listing_errors = [http(403, "Forbidden")]
    elif setup == "cancelled":
        lecture_id = _lecture(url, cancelled=True)
    else:
        lecture_id = str(uuid.uuid4())
    handler = TranscriptJobHandler(worker=_worker(url, graph), connect=_connect(url))
    with psycopg.connect(url, autocommit=True) as c:
        PublisherQueueRepository().enqueue(c, job_kind="ACQUIRE_TRANSCRIPT", subject_key=f"lecture:{lecture_id}",
                                           max_attempts=100)
    job, _ = _claim_and_handle(url, handler)
    state, events = _job(url, job["job_id"])
    assert state["status"] == status and events[-1][0] == event
