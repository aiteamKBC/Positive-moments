"""P0-B transcript worker: policy, Graph failure classification, OpenAI/QA independence, handler mapping."""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from app.publisher import handlers
from app.publisher.transcripts import (
    DEFERRED, NOT_APPLICABLE, PERMANENT, READY, RETRY, REVIEW, WAITING, TranscriptOutcome, TranscriptPolicy,
    TranscriptWorker,
)
from tests.unit.transcript_fakes import END, FakeGraph, http, network

POLICY = TranscriptPolicy()
REPO = Path(__file__).resolve().parents[2]


# -- policy -----------------------------------------------------------------------

def test_refresh_is_bounded_to_two_points_after_the_lecture():
    fetched = END + timedelta(hours=1)
    assert not POLICY.refresh_due(END, fetched, END + timedelta(hours=5))
    assert POLICY.refresh_due(END, fetched, END + timedelta(hours=7))           # 6 h point passed
    refreshed = END + timedelta(hours=7)
    assert not POLICY.refresh_due(END, refreshed, END + timedelta(hours=20))
    assert POLICY.refresh_due(END, refreshed, END + timedelta(hours=25))        # 24 h point passed
    final = END + timedelta(hours=25)
    assert not POLICY.refresh_due(END, final, END + timedelta(days=30))         # never again
    assert POLICY.next_refresh_at(END, END + timedelta(hours=1)) == END + timedelta(hours=6)
    assert POLICY.next_refresh_at(END, END + timedelta(hours=7)) == END + timedelta(hours=24)
    assert POLICY.next_refresh_at(END, END + timedelta(hours=25)) is None


def test_waiting_backs_off_with_age():
    assert [POLICY.waiting_delay(timedelta(hours=h)) for h in (1, 5, 20, 40)] == [
        timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=3)]


# -- Graph failure classification (through the REAL gateway) ------------------------

def _worker(graph=None, **kwargs):
    graph = graph or FakeGraph()
    return TranscriptWorker(connect=lambda: None, gateway=graph.gateway(), graph_client=graph, **kwargs), graph


@pytest.mark.parametrize("error, kind, reason", [
    (http(403, "Forbidden"), REVIEW, "GRAPH_ACCESS_BLOCKED:403"),
    (http(403, "ApplicationAccessPolicy"), REVIEW, "GRAPH_ACCESS_BLOCKED:403"),
    (http(429, "TooManyRequests"), RETRY, "GRAPH_RATE_LIMITED:429"),
    (http(500, "InternalServerError"), RETRY, "GRAPH_SERVER_ERROR:500"),
    (http(503, "ServiceUnavailable"), RETRY, "GRAPH_SERVER_ERROR:503"),
    (http(401, "InvalidAuthenticationToken"), RETRY, "GRAPH_UNAUTHORIZED:401"),
    (network(), RETRY, "GRAPH_NETWORK:None"),
    (http(404, "NotFound"), WAITING, "TRANSCRIPT_NOT_FOUND"),
    (http(400, "BadRequest"), REVIEW, "GRAPH_REJECTED:400"),
])
def test_listing_failures_are_classified(error, kind, reason):
    worker, graph = _worker()
    graph.listing_errors = [error]
    listing = worker.gateway.list_transcripts("user", "meeting")
    outcome = worker._classify(listing.status, listing.http_status, 1, timedelta(hours=1))
    assert (outcome.kind, outcome.reason) == (kind, reason)
    assert (outcome.retry_after is None) == (kind == REVIEW)


def test_empty_listing_is_waiting_not_an_error():
    worker, graph = _worker()
    listing = worker.gateway.list_transcripts("user", "meeting")
    assert worker._classify(listing.status, listing.http_status, 1, timedelta(hours=1)).kind == WAITING


def test_http_401_refreshes_the_token_exactly_once():
    worker, graph = _worker()
    graph.listing_errors = [http(401, "InvalidAuthenticationToken"), None]
    counters = {"graph_calls": 0}
    listing = worker._graph(counters, lambda: worker.gateway.list_transcripts("u", "m"))
    assert listing.status == "NO_TRANSCRIPTS_AVAILABLE" and graph.invalidations == 1 and counters["graph_calls"] == 2
    graph.listing_errors = [http(401), http(401)]
    listing = worker._graph(counters, lambda: worker.gateway.list_transcripts("u", "m"))
    assert listing.http_status == 401 and graph.invalidations == 2       # one refresh, then give up


def test_database_unavailable_never_reaches_graph():
    graph = FakeGraph()

    def refuse():
        raise psycopg.OperationalError("connection refused")
    worker = TranscriptWorker(connect=refuse, gateway=graph.gateway(), graph_client=graph)
    outcome = worker.run("00000000-0000-4000-8000-000000000001", attempt=2)
    assert outcome.kind == RETRY and outcome.reason.startswith("DATABASE_UNAVAILABLE")
    assert outcome.retry_after == timedelta(minutes=5) and graph.calls == []


# -- OpenAI / QA independence ---------------------------------------------------------

def test_worker_imports_no_qa_writer_or_model_code_and_needs_no_openai_key():
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("OPENAI", "QA_MODEL", "N8N", "DATABASE_URL"))}
    probe = (
        "import sys, app.publisher.handlers, app.publisher.transcripts\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'openai' or m.startswith(("
        "'app.qa', 'app.writer', 'app.rendering', 'app.positive_moments', 'app.engagement', 'app.attendance',"
        "'app.orchestration.orchestrator', 'app.orchestration.runner', 'app.orchestration.n8n_preflight',"
        "'app.orchestration.scheduler', 'app.orchestration.daemon')))\n"
        "print(bad)\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# -- handler mapping ------------------------------------------------------------------------

class _Conn:
    autocommit = False

    @contextmanager
    def transaction(self):
        yield

    def close(self):
        pass


class _Queue:
    def __init__(self, owned=True):
        self.calls, self.owned = [], owned

    def succeed(self, c, job_id, worker):
        self.calls.append(("succeed",))
        return self.owned

    def fail(self, c, job_id, worker, **kw):
        self.calls.append(("fail", kw["error_code"]))
        return self.owned

    def review(self, c, job_id, worker, **kw):
        self.calls.append(("review", kw["error_code"]))
        return self.owned

    def retry(self, c, job_id, worker, **kw):
        self.calls.append(("retry", kw["error_code"], kw["delay"]))
        return "RETRY_WAIT" if self.owned else None

    def event(self, c, event, **kw):
        self.calls.append(("event", event, kw["lecture_id"]))

    def enqueue(self, c, **kw):
        self.calls.append(("enqueue", kw["subject_key"], kw["not_before"], kw["payload"]))
        return {}, True


class _Worker:
    def __init__(self, outcome):
        self.outcome, self.seen = outcome, []

    def run(self, lecture_id, *, attempt):
        self.seen.append((lecture_id, attempt))
        return self.outcome


LID = "00000000-0000-4000-8000-0000000000aa"
JOB = {"job_id": "job-1", "lecture_id": LID, "subject_key": f"lecture:{LID}", "attempt_count": 3}


@pytest.mark.parametrize("outcome, expected", [
    (TranscriptOutcome(LID, READY, "TRANSCRIPT_STORED", next_refresh_at=END + timedelta(hours=6)),
     [("succeed",), ("event", "TRANSCRIPT_READY", LID),
      ("enqueue", f"lecture:{LID}", END + timedelta(hours=6), {"refresh": True})]),
    (TranscriptOutcome(LID, READY, "STORED_TRANSCRIPT_REUSED"), [("succeed",), ("event", "TRANSCRIPT_READY", LID)]),
    (TranscriptOutcome(LID, NOT_APPLICABLE, "LECTURE_CANCELLED"),
     [("succeed",), ("event", "TRANSCRIPT_NOT_APPLICABLE", LID)]),
    (TranscriptOutcome(LID, PERMANENT, "LECTURE_NOT_FOUND"),
     [("fail", "LECTURE_NOT_FOUND"), ("event", "TRANSCRIPT_FAILED", None)]),
    (TranscriptOutcome(LID, REVIEW, "GRAPH_ACCESS_BLOCKED:403"),
     [("review", "GRAPH_ACCESS_BLOCKED:403"), ("event", "TRANSCRIPT_REVIEW_REQUIRED", LID)]),
    (TranscriptOutcome(LID, WAITING, "NO_TRANSCRIPTS_AVAILABLE", retry_after=timedelta(minutes=15)),
     [("retry", "NO_TRANSCRIPTS_AVAILABLE", timedelta(minutes=15)), ("event", "TRANSCRIPT_WAITING", LID)]),
    (TranscriptOutcome(LID, DEFERRED, "ROW_LOCKED", retry_after=timedelta(minutes=5)),
     [("retry", "ROW_LOCKED", timedelta(minutes=5)), ("event", "TRANSCRIPT_DEFERRED", LID)]),
])
def test_handler_maps_outcomes_to_job_transitions(outcome, expected):
    queue, worker = _Queue(), _Worker(outcome)
    handler = handlers.TranscriptJobHandler(worker=worker, connect=_Conn, queue=queue)
    handler.enqueue = lambda c, lecture_id, **kw: queue.enqueue(  # route through the fake
        c, subject_key=handlers.subject_key(lecture_id), not_before=kw["not_before"], payload={"refresh": kw["refresh"]})
    assert handler.handle(JOB, "w1") is outcome
    assert worker.seen == [(LID, 3)]
    assert queue.calls == expected


def test_handler_reports_a_lost_lease_and_schedules_nothing():
    queue = _Queue(owned=False)
    outcome = TranscriptOutcome(LID, READY, "TRANSCRIPT_STORED", next_refresh_at=END)
    handlers.TranscriptJobHandler(worker=_Worker(outcome), connect=_Conn, queue=queue).handle(JOB, "w1")
    assert queue.calls == [("succeed",), ("event", "TRANSCRIPT_LEASE_LOST", LID)]


def test_outcome_event_carries_no_text_or_urls():
    outcome = TranscriptOutcome(LID, READY, "TRANSCRIPT_STORED", detail={"parts": [{"content_sha256": "a" * 64}]})
    text = repr(outcome.as_event())
    assert "http" not in text and "WEBVTT" not in text
