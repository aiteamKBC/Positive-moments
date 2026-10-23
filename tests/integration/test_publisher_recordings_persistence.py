"""
P0-C: the publisher recording worker against the isolated test database.

The worker COMMITS by design (independent of QA), so these tests use committed
data: every lecture they create is removed by committed_cleanup.purge_lectures,
and proposed migrations 026 (queue) and 027 (recording sources) are applied
for the module and removed with their rollback scripts. Microsoft Graph is the
fake client under the real discovery sources and metadata reader.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import psycopg
import pytest

from app.common.errors import DATABASE_ERROR, PlatformError
from app.config.settings import Settings
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.db.repositories.recording_sources import RecordingSourceRepository
from app.orchestration.locks import LectureLockManager, SchedulerCycleLock
from app.publisher.handlers import RecordingJobHandler
from app.publisher.recordings import (
    DEFERRED, LOCATED, NOT_APPLICABLE, PERMANENT, RETRY, REVIEW, WAITING, RecordingPolicy, RecordingWorker,
)
from app.publisher.transcripts import READY, TranscriptPolicy, TranscriptWorker
from tests.integration.committed_cleanup import purge_lectures
from tests.integration.publisher_db import (
    PROPOSED, RECORDING_MIGRATION, RECORDING_ROLLBACK, applied, recording_applied, reset_proposed_migrations,
)
from tests.integration.seeding import graph_id, insert
from tests.unit.recording_fakes import END, LOOKUP_USER, START, SUBJECT, FakeRecordingGraph, http, network
from tests.unit.transcript_fakes import FakeGraph as FakeTranscriptGraph, vtt
from tools.integration_db_guard import assert_isolated

MARKER = "p0c-recordings@example.invalid"
QUEUE_MIGRATION = PROPOSED / "026_create_publisher_queue_tables.sql"
QUEUE_ROLLBACK = PROPOSED / "026_create_publisher_queue_tables.rollback.sql"
AFTER = END + timedelta(hours=1)
FAST = RecordingPolicy(lock_timeout_ms=500)
CHANNEL = "19:p0cchannel0001@thread.tacv2"
CHANNEL_JOIN = "https://teams.microsoft.com/l/meetup-join/19%3ap0cchannel0001%40thread.tacv2/1718000000000?context=x"
TWO_HOURS = (END - START).total_seconds()


def _cleanup(admin):
    if "bunny_publisher_jobs" in applied(admin):
        admin.execute("DELETE FROM public.bunny_publisher_events")
        admin.execute("DELETE FROM public.bunny_publisher_jobs")
        admin.execute("DELETE FROM public.bunny_publisher_heartbeats")
        admin.execute(QUEUE_ROLLBACK.read_text(encoding="utf-8"))
    ids = [r[0] for r in admin.execute("SELECT lecture_id FROM public.lecture_sessions WHERE calendar_user_upn = %s",
                                       (MARKER,)).fetchall()]
    purge_lectures(admin, ids, with_artifacts=True)
    if recording_applied(admin):
        admin.execute("DELETE FROM public.bunny_recording_sources")
        admin.execute("DELETE FROM public.bunny_recording_discoveries")
        admin.execute(RECORDING_ROLLBACK.read_text(encoding="utf-8"))


class _Url(str):
    """The test database URL, never shown in a failure report."""

    def __repr__(self):
        return "'<isolated test database>'"


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
        assert "bunny_publisher_jobs" not in applied(admin) and recording_applied(admin) == []
        admin.execute(QUEUE_MIGRATION.read_text(encoding="utf-8"))
        admin.execute(RECORDING_MIGRATION.read_text(encoding="utf-8"))
        yield _Url(url)
    finally:
        try:
            _cleanup(admin)
        finally:
            admin.close()


def _connect(url):
    return lambda: psycopg.connect(url, prepare_threshold=None)


def _lecture(url, *, cancelled=False, suppressed=False, ready=True, start=START, end=END, subject=SUBJECT,
             join_url=None) -> str:
    lecture_id = uuid.uuid4()
    with psycopg.connect(url, autocommit=True) as c:
        assert_isolated(c)
        insert(c, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn=MARKER,
               calendar_event_id=f"evt-{lecture_id}", join_url=join_url or f"https://example.invalid/{lecture_id}",
               subject=subject, normalized_subject=subject.lower(), module=subject, scheduled_start=start,
               scheduled_end=end, session_date=start.date(), calendar_mapping_status="RESOLVED",
               group_match_status="MATCHED", discovery_status="READY",
               meeting_id=f"MTG-{lecture_id.hex[:12]}" if ready else None, downstream_ready=ready,
               is_cancelled=cancelled, meeting_lookup_user_id=LOOKUP_USER,
               meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID",
               metadata={"duplicate_suppression": {"winner_lecture_id": "x"}} if suppressed else {})
    return str(lecture_id)


def _worker(url, graph, *, clock=AFTER, region=None, policy=FAST, repository=None, reader=None):
    return RecordingWorker(connect=_connect(url), graph=graph,
                           discovery_factory=lambda: graph.discovery(region=region),
                           evidence_reader=reader or graph.reader(), clock=lambda: clock, policy=policy,
                           repository=repository)


def _discovery(url, lecture_id):
    with psycopg.connect(url) as c:
        return c.execute("""SELECT status, reason_code, review_reason, expected_part_count, located_part_count,
                                   missing_part_count, transcript_pairing, attempt_count, last_http_status
                              FROM public.bunny_recording_discoveries WHERE lecture_id = %s""",
                         (lecture_id,)).fetchone()


def _sources(url, lecture_id):
    with psycopg.connect(url) as c:
        return c.execute("""SELECT recording_part_index, graph_recording_id, locate_status, source_drive_id,
                                   source_item_id, media_origin, classification, transcript_artifact_id,
                                   discovery_sources, transcript_pairing
                              FROM public.bunny_recording_sources WHERE lecture_id = %s
                             ORDER BY recording_part_index""", (lecture_id,)).fetchall()


def _no_urls(url):
    with psycopg.connect(url) as c:
        return c.execute("""SELECT (SELECT count(*) FROM public.bunny_recording_sources s
                                     WHERE row_to_json(s)::text ~* '(https?://|tempauth)')
                                 + (SELECT count(*) FROM public.bunny_recording_discoveries d
                                     WHERE row_to_json(d)::text ~* '(https?://|tempauth)')""").fetchone()[0] == 0


def _one_part(graph, **kwargs):
    recording = graph.recording(START + timedelta(minutes=1), END - timedelta(minutes=2))
    return recording, graph.recorded_file(recording, **kwargs)


def _parts(graph, n, *, call_id="call-1"):
    """n consecutive recordings of one call, each overlapping the next by a minute (the 4 h cap pattern)."""
    span = (END - START) / n
    recordings = [graph.recording(START + span * i, START + span * (i + 1) + timedelta(minutes=1 if i < n - 1 else 0),
                                  call_id=call_id) for i in range(n)]
    return recordings, [graph.recorded_file(r, lead=20 + i) for i, r in enumerate(recordings)]


# -- discovery without QA, OpenAI, n8n or a transcript --------------------------------------------

def test_single_part_located_without_qa_row_transcript_or_openai(url, monkeypatch):
    for key in ("OPENAI_API_KEY", "N8N_PRECHECK_URL", "N8N_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording, mp4 = _one_part(graph)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (LOCATED, "ALL_PARTS_LOCATED")
    assert outcome.summary() == {"lecture_id": lecture_id, "expected_part_count": 1,
                                 "verified_source_part_count": 1, "missing_part_count": 0,
                                 "recording_status": "LOCATED", "reason": "ALL_PARTS_LOCATED", "review_reason": None}
    assert _discovery(url, lecture_id)[:7] == ("LOCATED", "ALL_PARTS_LOCATED", None, 1, 1, 0, "NO_TRANSCRIPT")
    [row] = _sources(url, lecture_id)
    assert row[:7] == (1, recording["id"], "LOCATED", mp4["parentReference"]["driveId"], mp4["id"],
                       "graph_created_origin", "FULL")
    assert row[8] == ["onedrive_recordings"] and _no_urls(url)
    with psycopg.connect(url) as c:          # nothing of QA's was needed or touched
        assert c.execute("SELECT count(*) FROM public.lecture_recording_links WHERE lecture_id = %s",
                         (lecture_id,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM public.lecture_transcript_selections WHERE lecture_id = %s",
                         (lecture_id,)).fetchone()[0] == 0
    assert outcome.next_refresh_at == END + timedelta(hours=24)


def test_rerun_reuses_the_stored_source_without_graph(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == LOCATED
    graph.calls.clear()
    again = worker.run(lecture_id)
    assert (again.kind, again.reason, again.graph_calls) == (LOCATED, "STORED_SOURCE_REUSED", 0)
    assert graph.calls == [] and len(_sources(url, lecture_id)) == 1 and _discovery(url, lecture_id)[7] == 1


@pytest.mark.parametrize("n", [2, 3])
def test_multipart_recording_resolved_in_order(url, n):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recordings, files = _parts(graph, n)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.expected_part_count, outcome.located_part_count) == (LOCATED, n, n)
    rows = _sources(url, lecture_id)
    assert [r[0] for r in rows] == list(range(1, n + 1))
    assert [r[1] for r in rows] == [r["id"] for r in recordings]
    assert [r[4] for r in rows] == [f["id"] for f in files] and len({r[4] for r in rows}) == n


def test_missing_second_part_is_never_treated_as_the_whole_lecture(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    first = graph.recording(START, START + timedelta(minutes=61))
    second = graph.recording(START + timedelta(minutes=60), END)
    graph.recorded_file(first)
    worker = _worker(url, graph)
    outcome = worker.run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "RECORDING_FILE_NOT_FOUND")
    assert outcome.summary()["missing_part_count"] == 1
    assert _discovery(url, lecture_id)[:6] == ("WAITING", "RECORDING_FILE_NOT_FOUND", None, 2, 1, 1)
    assert [r[2] for r in _sources(url, lecture_id)] == ["LOCATED", "NOT_FOUND"]
    # SharePoint shows the second file later: the first part's verified mapping is kept.
    graph.recorded_file(second)
    assert worker.run(lecture_id).kind == LOCATED
    assert [r[2] for r in _sources(url, lecture_id)] == ["LOCATED", "LOCATED"]


def test_ambiguous_candidate_requires_review(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording, _ = _one_part(graph)
    graph.recorded_file(recording, lead=60, size=12345)               # a second, different file fits too
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason, outcome.review_reason) == (
        REVIEW, "SEVERAL_FILES_FOR_PART", "PART_1:SEVERAL_FILES_FOR_PART")
    assert _discovery(url, lecture_id)[0] == "REVIEW_REQUIRED"
    [row] = _sources(url, lecture_id)
    assert row[2] == "AMBIGUOUS" and row[4] is None


def test_review_is_retained_until_an_operator_rediscovers(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording, _ = _one_part(graph)
    duplicate = graph.recorded_file(recording, lead=60, size=12345)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == REVIEW
    graph.onedrive[LOOKUP_USER] = [i for i in graph.onedrive[LOOKUP_USER] if i["id"] != duplicate["id"]]
    graph.calls.clear()
    kept = worker.run(lecture_id)                          # the ambiguity is gone, but nobody has looked
    assert (kept.kind, kept.reason, kept.graph_calls) == (REVIEW, "SEVERAL_FILES_FOR_PART", 0)
    assert graph.calls == [] and _sources(url, lecture_id)[0][2] == "AMBIGUOUS"
    reopened = worker.run(lecture_id, rediscover=True)
    assert reopened.kind == LOCATED and _sources(url, lecture_id)[0][2] == "LOCATED"


@pytest.mark.parametrize("kind", ["subject", "date"])
def test_wrong_subject_or_wrong_date_is_not_located(url, kind):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording = graph.recording(START, END)
    if kind == "subject":
        graph.recorded_file(recording, subject="Parallel Cohort Lecture")
    else:
        graph.mp4(started=START - timedelta(days=1, seconds=30), duration_seconds=TWO_HOURS)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "RECORDING_FILE_NOT_FOUND")
    assert [r[2] for r in _sources(url, lecture_id)] == ["NOT_FOUND"]


def test_duration_mismatch_requires_review(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph, duration=TWO_HOURS / 2)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (REVIEW, "FILE_DURATION_CONTRADICTS_RECORDING")


def test_duration_not_yet_indexed_waits(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording = graph.recording(START, END)
    graph.mp4(started=START - timedelta(seconds=30), duration_seconds=None)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "FILE_DURATION_UNAVAILABLE")
    assert [r[2] for r in _sources(url, lecture_id)] == ["DURATION_PENDING"] and recording


def test_recording_not_yet_available_and_in_progress(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    graph.recording(START - timedelta(days=7), END - timedelta(days=7))      # another occurrence only
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "RECORDING_NOT_YET_AVAILABLE")
    graph.recording(START, None)
    assert _worker(url, graph).run(lecture_id).reason == "RECORDING_IN_PROGRESS"


def test_channel_meeting_recording_in_the_channel_folder(url):
    lecture_id, graph = _lecture(url, join_url=CHANNEL_JOIN), FakeRecordingGraph()
    graph.teams = {"team-1": ["19:other@thread.tacv2", CHANNEL]}
    recording = graph.recording(START, END)
    mp4 = graph.recorded_file(recording, where="channel", channel=CHANNEL, drive=f"b!channel-drive-{CHANNEL}")
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == LOCATED
    [row] = _sources(url, lecture_id)
    assert row[3:5] == (f"b!channel-drive-{CHANNEL}", mp4["id"]) and row[8] == ["channel_recordings"]


def test_recording_in_another_sharepoint_site_found_by_search(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording = graph.recording(START, END)
    mp4 = graph.recorded_file(recording, where="search", drive="b!another-site-drive")
    outcome = _worker(url, graph, region="EMEA").run(lecture_id)
    assert outcome.kind == LOCATED
    [row] = _sources(url, lecture_id)
    assert row[3:5] == ("b!another-site-drive", mp4["id"]) and row[8] == ["tenant_search"]
    assert graph.count("item") == 1                     # search hits carry no duration: read it once


def test_channel_not_in_the_lookup_users_teams_is_an_inaccessible_location(url):
    lecture_id, graph = _lecture(url, join_url=CHANNEL_JOIN), FakeRecordingGraph()
    graph.recording(START, END)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (REVIEW, "SHAREPOINT_LOCATION_INACCESSIBLE:CHANNEL_RECORDINGS")


# -- Graph failures ------------------------------------------------------------------------------------

@pytest.mark.parametrize("error, kind, reason", [
    (http(429, "TooManyRequests"), RETRY, "GRAPH_RECORDINGS_RATE_LIMITED:429"),
    (http(503, "ServiceUnavailable"), RETRY, "GRAPH_RECORDINGS_SERVER_ERROR:503"),
    (network(), RETRY, "GRAPH_RECORDINGS_NETWORK:None"),
    (http(403, "Forbidden"), REVIEW, "GRAPH_RECORDINGS_ACCESS_DENIED:403"),
    (http(404, "NotFound"), WAITING, "GRAPH_RECORDINGS_NOT_FOUND:404"),
    (http(400, "BadRequest"), REVIEW, "GRAPH_RECORDINGS_REJECTED:400"),
])
def test_graph_listing_failures_are_classified(url, error, kind, reason):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    graph.recording_errors = [error]
    outcome = _worker(url, graph).run(lecture_id, attempt=1)
    assert (outcome.kind, outcome.reason) == (kind, reason)
    status = {RETRY: "RETRY_SCHEDULED", REVIEW: "REVIEW_REQUIRED", WAITING: "WAITING"}[kind]
    assert _discovery(url, lecture_id)[0] == status and _sources(url, lecture_id) == []
    if kind == RETRY:
        assert outcome.retry_after == timedelta(minutes=1)


def test_unauthorized_refreshes_the_token_once(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    graph.recording_errors = [http(401, "InvalidAuthenticationToken")]
    outcome = _worker(url, graph).run(lecture_id)
    assert outcome.kind == LOCATED and graph.invalidations == 1
    lecture2, graph2 = _lecture(url), FakeRecordingGraph()
    _one_part(graph2)
    graph2.recording_errors = [http(401), http(401)]
    assert _worker(url, graph2).run(lecture2).reason == "GRAPH_RECORDINGS_UNAUTHORIZED:401"


@pytest.mark.parametrize("error, kind, reason", [
    (http(403, "accessDenied"), REVIEW, "SHAREPOINT_ONEDRIVE_RECORDINGS_ACCESS_DENIED:403"),
    (http(503, "serviceNotAvailable"), RETRY, "SHAREPOINT_ONEDRIVE_RECORDINGS_SERVER_ERROR:503"),
])
def test_sharepoint_failures_never_locate(url, error, kind, reason):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    graph.onedrive_errors[LOOKUP_USER] = error
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (kind, reason) and _sources(url, lecture_id) == []


def test_file_vanishing_between_listing_and_metadata_read_waits(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _, mp4 = _one_part(graph)
    graph.item_errors[(mp4["parentReference"]["driveId"], mp4["id"])] = [http(404, "itemNotFound")]
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (WAITING, "RECORDING_FILE_NOT_FOUND")


def test_deadline_ends_the_wait_in_review(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    outcome = _worker(url, graph, clock=END + timedelta(hours=73)).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (REVIEW, "DEADLINE_PASSED:RECORDING_NOT_YET_AVAILABLE")
    assert _discovery(url, lecture_id)[:3] == ("REVIEW_REQUIRED", "DEADLINE_PASSED:RECORDING_NOT_YET_AVAILABLE",
                                                "DEADLINE_PASSED:RECORDING_NOT_YET_AVAILABLE")


def test_not_ended_cancelled_duplicate_unresolved_and_unknown(url):
    graph = FakeRecordingGraph()
    early = _worker(url, graph, clock=START + timedelta(minutes=30)).run(_lecture(url))
    assert (early.kind, early.reason) == (WAITING, "LECTURE_NOT_ENDED")
    assert _worker(url, graph).run(_lecture(url, cancelled=True)).kind == NOT_APPLICABLE
    assert _worker(url, graph).run(_lecture(url, suppressed=True)).reason == "LI_DUPLICATE"
    assert _worker(url, graph).run(_lecture(url, ready=False)).reason == "MEETING_NOT_RESOLVED"
    assert _worker(url, graph).run(str(uuid.uuid4())).kind == PERMANENT
    assert graph.calls == []


# -- previously resolved sources ------------------------------------------------------------------------

def test_failed_reads_leave_committed_mappings_intact(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    first = graph.recording(START, START + timedelta(minutes=61))
    graph.recording(START + timedelta(minutes=60), END)
    graph.recorded_file(first)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == WAITING
    before = _sources(url, lecture_id)
    graph.recording_errors = [http(503)]
    assert worker.run(lecture_id).kind == RETRY
    graph.onedrive_errors[LOOKUP_USER] = http(429)
    assert worker.run(lecture_id).kind == RETRY
    assert _sources(url, lecture_id) == before and before[0][2] == "LOCATED"
    assert _discovery(url, lecture_id)[4] == 1


def test_verification_confirms_unchanged_files(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _parts(graph, 2)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == LOCATED
    graph.calls.clear()
    outcome = worker.run(lecture_id, verify=True)
    assert (outcome.kind, outcome.reason) == (LOCATED, "SOURCES_VERIFIED")
    assert graph.count("recordings") == 1 and graph.count("item") == 2 and graph.count("onedrive") == 0


def test_verification_is_due_at_the_bounded_point(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == LOCATED
    assert _worker(url, graph, clock=END + timedelta(hours=2)).run(lecture_id).reason == "STORED_SOURCE_REUSED"
    assert _worker(url, graph, clock=END + timedelta(hours=25)).run(lecture_id).reason == "SOURCES_VERIFIED"
    assert _worker(url, graph, clock=END + timedelta(hours=30)).run(lecture_id).reason == "STORED_SOURCE_REUSED"


@pytest.mark.parametrize("change, status, reason", [
    ("delete", "SOURCE_MISSING", "SOURCE_FILE_MISSING"),
    ("resize", "SOURCE_CHANGED", "SOURCE_FILE_CHANGED"),
])
def test_missing_or_changed_source_file_requires_review(url, change, status, reason):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _, mp4 = _one_part(graph)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == LOCATED
    key = (mp4["parentReference"]["driveId"], mp4["id"])
    if change == "delete":
        del graph.items[key]
    else:
        graph.items[key]["size"] = mp4["size"] // 2
    outcome = worker.run(lecture_id, verify=True)
    assert (outcome.kind, outcome.reason, outcome.review_reason) == (REVIEW, reason, f"PART_1:{reason}")
    [row] = _sources(url, lecture_id)
    assert row[2] == status and row[3:5] == key                 # the identity is kept, never erased
    assert _discovery(url, lecture_id)[:5] == ("REVIEW_REQUIRED", reason, f"PART_1:{reason}", 1, 0)


def test_verification_read_failure_keeps_the_verified_mapping(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _, mp4 = _one_part(graph)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == LOCATED
    graph.item_errors[(mp4["parentReference"]["driveId"], mp4["id"])] = [http(503)]
    assert worker.run(lecture_id, verify=True).reason == "REVERIFY_FAILED_STORED_KEPT"
    graph.recording_errors = [network()]
    assert worker.run(lecture_id, verify=True).reason == "REVERIFY_FAILED_STORED_KEPT"
    assert _discovery(url, lecture_id)[0] == "LOCATED" and _sources(url, lecture_id)[0][2] == "LOCATED"


def test_a_late_extra_part_after_location_requires_review(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    worker = _worker(url, graph)
    assert worker.run(lecture_id).kind == LOCATED
    graph.recording(END + timedelta(minutes=5), END + timedelta(minutes=20))   # attaches: within 20 min
    outcome = worker.run(lecture_id, verify=True)
    assert (outcome.kind, outcome.reason) == (REVIEW, "RECORDING_SET_CHANGED")
    assert _sources(url, lecture_id)[0][2] == "LOCATED"           # never re-pointed automatically


def test_one_file_can_never_belong_to_two_lectures(url):
    graph = FakeRecordingGraph()
    _one_part(graph)
    first, second = _lecture(url), _lecture(url)                  # an unsuppressed duplicate of one slot
    assert _worker(url, graph).run(first).kind == LOCATED
    outcome = _worker(url, graph).run(second)
    assert (outcome.kind, outcome.reason) == (REVIEW, "SOURCE_CLAIMED_BY_ANOTHER_LECTURE")
    assert _sources(url, second) == [] and len(_sources(url, first)) == 1


# -- transactions, crashes and concurrency --------------------------------------------------------------

class _CrashingRepository(RecordingSourceRepository):
    """Writes the first part row, then the process dies (or the database fails) mid-transaction."""

    def __init__(self, error):
        self.error = error

    def replace_parts(self, connection, lecture_id, mappings, **kwargs):
        super().replace_parts(connection, lecture_id, mappings[:1], **kwargs)
        raise self.error


def test_crash_halfway_through_persistence_commits_nothing_and_restarts_cleanly(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _parts(graph, 2)
    with pytest.raises(RuntimeError):
        _worker(url, graph, repository=_CrashingRepository(RuntimeError("killed"))).run(lecture_id)
    assert _discovery(url, lecture_id) is None and _sources(url, lecture_id) == []
    failed = _worker(url, graph, repository=_CrashingRepository(PlatformError(DATABASE_ERROR, "x"))).run(lecture_id)
    assert (failed.kind, failed.reason) == (RETRY, "DATABASE_ERROR:PlatformError") and failed.graph_calls > 0
    assert _discovery(url, lecture_id) is None
    resumed = _worker(url, graph).run(lecture_id)                 # the lecture lock died with the session
    assert resumed.kind == LOCATED and len(_sources(url, lecture_id)) == 2


class _SlowGraph(FakeRecordingGraph):
    def get_collection(self, path):
        time.sleep(0.3)
        return super().get_collection(path)


def test_duplicate_workers_resolve_a_lecture_once(url):
    lecture_id, graph = _lecture(url), _SlowGraph()
    _parts(graph, 2)
    results, barrier = [], threading.Barrier(3)

    def run():
        barrier.wait()
        results.append(_worker(url, graph).run(lecture_id))

    threads = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    kinds = sorted(r.kind for r in results)
    assert kinds.count(LOCATED) >= 1 and set(kinds) <= {LOCATED, DEFERRED}
    assert len(_sources(url, lecture_id)) == 2 and _discovery(url, lecture_id)[0] == "LOCATED"


def test_a_lecture_lock_held_by_qa_or_p0b_defers_without_writing(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    holder = psycopg.connect(url, autocommit=True)
    try:
        assert LectureLockManager().try_acquire(holder, lecture_id)
        outcome = _worker(url, graph).run(lecture_id)
        assert (outcome.kind, outcome.reason) == (DEFERRED, "LECTURE_LOCKED")
        assert outcome.retry_after == timedelta(minutes=10) and _discovery(url, lecture_id) is None
        assert graph.calls == []
    finally:
        holder.close()
    assert _worker(url, graph).run(lecture_id).kind == LOCATED


def test_a_running_qa_cycle_does_not_block_recording_discovery(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    qa = psycopg.connect(url, autocommit=True)
    try:
        assert SchedulerCycleLock().try_acquire(qa)               # the nightly cycle is running
        assert _worker(url, graph).run(lecture_id).kind == LOCATED
    finally:
        qa.close()


def _transcript_worker(url, tgraph):
    return TranscriptWorker(connect=_connect(url), gateway=tgraph.gateway(), graph_client=tgraph,
                            clock=lambda: AFTER, policy=TranscriptPolicy(lock_timeout_ms=500))


def test_transcript_parts_are_paired_and_a_later_transcript_is_validated(url):
    lecture_id, graph, tgraph = _lecture(url), FakeRecordingGraph(), FakeTranscriptGraph()
    tids = [graph_id(uuid.uuid4().int % 90000 + 10000) for _ in range(2)]
    recordings, _ = _parts(graph, 2, call_id="call-1")

    # The recording is ready before the transcript.
    assert _worker(url, graph).run(lecture_id).transcript_pairing == "NO_TRANSCRIPT"
    for i, (tid, rec) in enumerate(zip(tids, recordings)):
        start = START + (END - START) / 2 * i
        tgraph.add(tid, start=start, end=start + (END - START) / 2, call_id="call-1", content=vtt(55, label=str(i)),
                   correlation=rec["contentCorrelationId"])            # the same stretch of the call
    assert _transcript_worker(url, tgraph).run(lecture_id).kind == READY
    outcome = _worker(url, graph).run(lecture_id, verify=True)
    assert (outcome.kind, outcome.transcript_pairing) == (LOCATED, "PAIRED")
    rows = _sources(url, lecture_id)
    assert all(r[7] is not None for r in rows) and [r[9] for r in rows] == ["content_correlation_id"] * 2
    assert _discovery(url, lecture_id)[6] == "PAIRED"


def test_a_transcript_from_another_recording_is_a_conflict(url):
    lecture_id, graph, tgraph = _lecture(url), FakeRecordingGraph(), FakeTranscriptGraph()
    tid = graph_id(uuid.uuid4().int % 90000 + 10000)
    tgraph.add(tid, start=START, end=END, call_id="call-1", content=vtt(110), correlation="corr-last-week")
    assert _transcript_worker(url, tgraph).run(lecture_id).kind == READY
    graph.recording(START - timedelta(days=7), END - timedelta(days=7), correlation="corr-last-week")
    _one_part(graph)
    outcome = _worker(url, graph).run(lecture_id)
    assert (outcome.kind, outcome.reason) == (REVIEW, "TRANSCRIPT_PART_BELONGS_TO_ANOTHER_RECORDING")
    assert _discovery(url, lecture_id)[6] == "CONFLICT"


def test_recording_and_transcript_workers_run_side_by_side(url):
    lecture_id, graph, tgraph = _lecture(url), _SlowGraph(), FakeTranscriptGraph()
    _one_part(graph)
    tgraph.add(graph_id(uuid.uuid4().int % 90000 + 10000), start=START + timedelta(minutes=1),
               end=END - timedelta(minutes=2), content=vtt(110))
    results = {}
    threads = [threading.Thread(target=lambda: results.setdefault("rec", _worker(url, graph).run(lecture_id))),
               threading.Thread(target=lambda: results.setdefault("tr", _transcript_worker(url, tgraph).run(lecture_id)))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # One may yield the lecture lock to the other; neither fails, and a rerun finishes the deferred one.
    assert {results["rec"].kind, results["tr"].kind} <= {LOCATED, READY, DEFERRED}
    if results["rec"].kind == DEFERRED:
        assert _worker(url, graph).run(lecture_id).kind == LOCATED
    if results["tr"].kind == DEFERRED:
        assert _transcript_worker(url, tgraph).run(lecture_id).kind == READY
    assert _discovery(url, lecture_id)[0] == "LOCATED"


# -- queue, coverage, schema -----------------------------------------------------------------------------

def test_handler_records_the_outcome_and_schedules_one_verification(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    queue = PublisherQueueRepository()
    handler = RecordingJobHandler(worker=_worker(url, graph), connect=_connect(url), queue=queue)
    with psycopg.connect(url, autocommit=True) as c:
        handler.enqueue(c, lecture_id)
        job = queue.claim(c, "worker-a", ["RESOLVE_RECORDING"])
    outcome = handler.handle(job, "worker-a")
    assert outcome.kind == LOCATED
    with psycopg.connect(url) as c:
        jobs = c.execute("""SELECT status, payload, next_attempt_at FROM public.bunny_publisher_jobs
                             WHERE lecture_id = %s ORDER BY created_at""", (lecture_id,)).fetchall()
        events = [r[0] for r in c.execute("SELECT event FROM public.bunny_publisher_events WHERE lecture_id = %s",
                                          (lecture_id,)).fetchall()]
    assert [j[0] for j in jobs] == ["SUCCEEDED", "QUEUED"] and jobs[1][1] == {"verify": True}
    assert jobs[1][2] == END + timedelta(hours=24) and events == ["RECORDING_LOCATED"]


def test_handler_sends_ambiguity_to_review(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording, _ = _one_part(graph)
    graph.recorded_file(recording, lead=70, size=1)
    queue = PublisherQueueRepository()
    handler = RecordingJobHandler(worker=_worker(url, graph), connect=_connect(url), queue=queue)
    with psycopg.connect(url, autocommit=True) as c:
        handler.enqueue(c, lecture_id)
        job = queue.claim(c, "worker-a", ["RESOLVE_RECORDING"])
    handler.handle(job, "worker-a")
    with psycopg.connect(url) as c:
        assert c.execute("SELECT status, last_error_code FROM public.bunny_publisher_jobs WHERE job_id = %s",
                         (job["job_id"],)).fetchone() == ("REVIEW_REQUIRED", "SEVERAL_FILES_FOR_PART")


def test_coverage_view_shows_every_eligible_lecture(url):
    # The view judges "ended" by the database clock, so these lectures are in the past.
    start = START.replace(year=2026, month=1, day=14)
    end = start + (END - START)
    pending = _lecture(url, start=start - timedelta(days=1), end=end - timedelta(days=1))
    located, graph = _lecture(url, start=start, end=end), FakeRecordingGraph()
    graph.recorded_file(graph.recording(start, end))
    assert _worker(url, graph, clock=end + timedelta(hours=1)).run(located).kind == LOCATED
    cancelled = _lecture(url, cancelled=True, start=start, end=end)
    with psycopg.connect(url) as c:
        rows = RecordingSourceRepository().coverage(c, start.date() - timedelta(days=1), start.date())
    by_id = {str(r["lecture_id"]): r for r in rows}
    assert by_id[pending]["recording_status"] == "PENDING"
    assert (by_id[located]["recording_status"], by_id[located]["located_part_count"]) == ("LOCATED", 1)
    assert cancelled not in by_id                                   # cancelled lectures are not eligible


def test_measured_parts_see_their_verified_source_through_the_view(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    recording, mp4 = _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == LOCATED
    with psycopg.connect(url) as c:
        insert(c, "lecture_recording_parts", recording_part_id=uuid.uuid4(), lecture_id=lecture_id, part_index=1,
               canonical_offset_ms=0, user_id=LOOKUP_USER, meeting_id="m", recording_id=recording["id"],
               media_duration_seconds=7000, media_duration_source="mp4_movie_header",
               media_coordinate_version="v1")
        row = c.execute("""SELECT part_index, source_drive_id, source_item_id FROM public.bunny_recording_part_sources_v
                            WHERE lecture_id = %s""", (lecture_id,)).fetchone()
        c.rollback()                                                # the measured row is not kept
    assert row == (1, mp4["parentReference"]["driveId"], mp4["id"])


def test_rollback_refuses_while_rows_exist(url):
    lecture_id, graph = _lecture(url), FakeRecordingGraph()
    _one_part(graph)
    assert _worker(url, graph).run(lecture_id).kind == LOCATED
    with psycopg.connect(url) as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute(RECORDING_ROLLBACK.read_text(encoding="utf-8").replace("BEGIN;", "").replace("COMMIT;", ""))
        c.rollback()
        assert recording_applied(c) == ["bunny_recording_discoveries", "bunny_recording_sources"]
