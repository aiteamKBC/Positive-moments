"""P0-C recording worker: policy, database outage, QA/OpenAI independence, handler mapping, migration 027 shape."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.publisher import handlers
from app.publisher.recordings import (
    DEFERRED, LOCATED, NOT_APPLICABLE, PERMANENT, RETRY, REVIEW, WAITING, RecordingOutcome, RecordingPolicy,
    RecordingWorker,
)
from tests.unit.recording_fakes import END, FakeRecordingGraph
from tests.unit.test_publisher_transcript_worker import JOB, LID, _Conn, _Queue

POLICY = RecordingPolicy()
REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "app" / "db" / "migrations_proposed" / "027_create_recording_source_tables.sql"


def test_verification_is_bounded_to_one_point_after_the_lecture():
    located = END + timedelta(hours=1)
    assert not POLICY.verify_due(END, located, END + timedelta(hours=23))
    assert POLICY.verify_due(END, located, END + timedelta(hours=25))
    assert not POLICY.verify_due(END, END + timedelta(hours=25), END + timedelta(days=30))
    assert POLICY.next_verify_at(END, located) == END + timedelta(hours=24)
    assert POLICY.next_verify_at(END, END + timedelta(hours=25)) is None


def test_waiting_and_retry_schedules_are_bounded():
    assert POLICY.waiting_delay(timedelta(minutes=30)) == timedelta(minutes=15)
    assert POLICY.waiting_delay(timedelta(days=2)) == timedelta(hours=3)
    assert [POLICY.retry_delay(a) for a in (1, 2, 3, 4, 9)] == [
        timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1), timedelta(hours=3)]


def test_database_unavailable_is_a_retry_with_no_graph_call():
    graph = FakeRecordingGraph()

    def refuse():
        raise OSError("connection refused")

    worker = RecordingWorker(connect=refuse, graph=graph, discovery_factory=graph.discovery,
                             evidence_reader=graph.reader())
    outcome = worker.run("00000000-0000-4000-8000-000000000001", attempt=2)
    assert (outcome.kind, outcome.retry_after) == (RETRY, timedelta(minutes=5))
    assert outcome.reason == "DATABASE_UNAVAILABLE:OSError" and graph.calls == []


def test_recording_worker_imports_no_qa_writer_or_model_code_and_needs_no_openai_key():
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("OPENAI", "QA_MODEL", "N8N", "DATABASE_URL"))}
    probe = (
        "import sys, app.publisher.recordings, app.publisher.factory, app.publisher.handlers\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'openai' or m.startswith(("
        "'app.qa', 'app.writer', 'app.rendering', 'app.positive_moments', 'app.engagement', 'app.attendance',"
        "'app.media.moments_media', 'app.media.part_jobs', 'app.media.runner',"
        "'app.orchestration.orchestrator', 'app.orchestration.runner', 'app.orchestration.n8n_preflight',"
        "'app.orchestration.scheduler', 'app.orchestration.daemon')))\n"
        "print(bad)\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_worker_never_builds_a_graph_writer():
    source = (REPO / "app" / "publisher" / "recordings.py").read_text(encoding="utf-8") + \
        (REPO / "app" / "publisher" / "factory.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""|#[^\n]*', "", source)
    assert "RecordingLinkPublisher" not in code and "createLink" not in code and "post_json" not in code
    assert "qa_doctors_sessions" not in code and "write_link" not in code


# -- handler mapping ------------------------------------------------------------------------------

class _Worker:
    def __init__(self, outcome):
        self.outcome, self.seen = outcome, []

    def run(self, lecture_id, *, attempt, verify=False, rediscover=False):
        self.seen.append((lecture_id, attempt, verify, rediscover))
        return self.outcome


@pytest.mark.parametrize("outcome, expected", [
    (RecordingOutcome(LID, LOCATED, "ALL_PARTS_LOCATED", next_refresh_at=END + timedelta(hours=24)),
     [("succeed",), ("event", "RECORDING_LOCATED", LID),
      ("enqueue", f"lecture:{LID}", END + timedelta(hours=24), {"verify": True})]),
    (RecordingOutcome(LID, LOCATED, "STORED_SOURCE_REUSED"), [("succeed",), ("event", "RECORDING_LOCATED", LID)]),
    (RecordingOutcome(LID, NOT_APPLICABLE, "LECTURE_CANCELLED"),
     [("succeed",), ("event", "RECORDING_NOT_APPLICABLE", LID)]),
    (RecordingOutcome(LID, PERMANENT, "LECTURE_NOT_FOUND"),
     [("fail", "LECTURE_NOT_FOUND"), ("event", "RECORDING_FAILED", None)]),
    (RecordingOutcome(LID, REVIEW, "SEVERAL_FILES_FOR_PART"),
     [("review", "SEVERAL_FILES_FOR_PART"), ("event", "RECORDING_REVIEW_REQUIRED", LID)]),
    (RecordingOutcome(LID, WAITING, "RECORDING_FILE_NOT_FOUND", retry_after=timedelta(minutes=15)),
     [("retry", "RECORDING_FILE_NOT_FOUND", timedelta(minutes=15)), ("event", "RECORDING_WAITING", LID)]),
    (RecordingOutcome(LID, RETRY, "GRAPH_RECORDINGS_RATE_LIMITED:429", retry_after=timedelta(minutes=1)),
     [("retry", "GRAPH_RECORDINGS_RATE_LIMITED:429", timedelta(minutes=1)), ("event", "RECORDING_RETRY", LID)]),
    (RecordingOutcome(LID, DEFERRED, "LECTURE_LOCKED", retry_after=timedelta(minutes=10)),
     [("retry", "LECTURE_LOCKED", timedelta(minutes=10)), ("event", "RECORDING_DEFERRED", LID)]),
])
def test_handler_maps_outcomes_to_job_transitions(outcome, expected):
    queue, worker = _Queue(), _Worker(outcome)
    handler = handlers.RecordingJobHandler(worker=worker, connect=_Conn, queue=queue)
    handler.enqueue = lambda c, lecture_id, **kw: queue.enqueue(
        c, subject_key=handlers.subject_key(lecture_id), not_before=kw["not_before"], payload={"verify": kw["refresh"]})
    assert handler.handle({**JOB, "payload": {}}, "w1") is outcome
    assert worker.seen == [(LID, 3, False, False)]
    assert queue.calls == expected


def test_a_verify_job_runs_the_worker_in_verify_mode():
    worker = _Worker(RecordingOutcome(LID, LOCATED, "SOURCES_VERIFIED"))
    handlers.RecordingJobHandler(worker=worker, connect=_Conn, queue=_Queue()).handle(
        {**JOB, "payload": {"verify": True}}, "w1")
    handlers.RecordingJobHandler(worker=worker, connect=_Conn, queue=_Queue()).handle(
        {**JOB, "payload": {"rediscover": True}}, "w1")
    assert worker.seen == [(LID, 3, True, False), (LID, 3, False, True)]


def test_outcome_summary_and_event_carry_no_urls():
    outcome = RecordingOutcome(LID, WAITING, "RECORDING_FILE_NOT_FOUND", expected_part_count=3,
                               located_part_count=2, parts=[{"part_index": 1, "locate_status": "LOCATED"}])
    assert outcome.summary()["missing_part_count"] == 1 and outcome.recording_status == "WAITING"
    assert "http" not in repr(outcome.as_event())


# -- migration 027 shape ----------------------------------------------------------------------------

def test_migration_027_is_additive_and_touches_no_existing_table():
    text = MIGRATION.read_text(encoding="utf-8")
    code = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", text))
    code = re.sub(r"\bON (DELETE|UPDATE)\b", "", code)
    assert not re.search(r"\b(ALTER|DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", code)
    assert set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", text)) == {
        "bunny_recording_discoveries", "bunny_recording_sources"}
    assert set(re.findall(r"CREATE OR REPLACE VIEW public\.([a-z_]+)", text)) == {
        "bunny_recording_coverage_v", "bunny_recording_part_sources_v"}
