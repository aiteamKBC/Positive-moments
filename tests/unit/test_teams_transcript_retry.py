"""
Retry Teams transcript: when it is offered, what queueing does, and what the worker's run does,
with stub state and no database. The real code path end to end is in
tests/integration/test_teams_transcript_retry.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.common.errors import PlatformError
from app.db.repositories.lecture_processing_jobs import REASON_TEAMS_TRANSCRIPT_RETRY
from app.orchestration.locks import LectureBusy
from app.orchestration.teams_transcript_retry import (
    LECTURE_NOT_ENDED,
    LECTURE_NOT_FOUND,
    MEETING_NOT_RESOLVED,
    NO_TRANSCRIPT_PROBLEM,
    QA_ALREADY_COMPLETE,
    SUPPRESSED_DUPLICATE,
    TEAMS_TRANSCRIPT_UNAVAILABLE,
    TEAMS_UNREACHABLE,
    LockHeldByCaller,
    TeamsTranscriptRetry,
    lock_for_transaction,
    teams_reason_text,
    transcript_problem,
)
from app.orchestration.transcript_recovery import (
    ALREADY_QUEUED,
    NOTHING_TO_PROCESS,
    QUEUE_UNAVAILABLE,
    ProcessingRefused,
)
from app.publisher.transcripts import DEFERRED, READY, RETRY, REVIEW, TranscriptOutcome

NOW = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)
ENDED = datetime(2026, 11, 17, 11, tzinfo=timezone.utc)
LECTURE = "94008498-1fa8-51a2-a5c6-c9cd4077e8ab"


def state(*, not_delivered=None, blocking="SELECTION", selection=("MISSING", "ACQUIRE_TRANSCRIPT",
          "TRANSCRIPT_LISTING_NOT_SETTLED"), qa="MISSING", meeting="COMPLETE", suppressed=False,
          next_action="ACQUIRE_TRANSCRIPT"):
    sel_state, sel_action, sel_reason = selection
    stages = {"MEETING": {"state": meeting, "action": None},
              "TRANSCRIPT": {"state": "COMPLETE", "action": None},
              "SELECTION": {"state": sel_state, "action": sel_action, "reason": sel_reason},
              "QA_EVALUATION": {"state": qa, "action": None},
              "LEGACY_QA_SYNC": {"state": "MISSING"}, "PERFECT_SYNC": {"state": "MISSING"}}
    return {"lecture_id": LECTURE, "subject": "Marketing Impact and Planning", "session_date": "2026-11-17",
            "stages": stages, "blocking_stage": blocking, "next_executable_action": next_action,
            "not_delivered": not_delivered is not None, "not_delivered_reason": not_delivered,
            "is_suppressed_duplicate": suppressed}


class Resolver:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    def for_lecture(self, _connection, _lecture_id):
        self.calls += 1
        return self.states[min(self.calls, len(self.states)) - 1]


class Connection:
    def __init__(self, scheduled_end=ENDED, exists=True):
        self.row = (scheduled_end,) if exists else None

    def execute(self, _sql, _params=None):
        row = self.row

        class Cursor:
            def fetchone(self):
                return row
        return Cursor()


class Jobs:
    def __init__(self, *, accepts=True, active=None):
        self.accepts, self.active, self.enqueued = accepts, active, []

    def available(self, _c):
        return True

    def accepts_reason(self, _c, reason):
        assert reason == REASON_TEAMS_TRANSCRIPT_RETRY
        return self.accepts

    def active_for_lecture(self, _c, _lecture_id):
        return self.active

    def latest_for_lecture(self, _c, _lecture_id):
        return self.active

    def enqueue(self, _c, lecture_id, *, reason, requested_by=None):
        self.enqueued.append((lecture_id, reason, requested_by))
        created = self.active is None
        self.active = self.active or {"job_id": "J1", "lecture_id": lecture_id, "reason": reason,
                                      "status": "QUEUED"}
        return self.active, created


class Orchestrator:
    def __init__(self):
        self.runs = []

    def run_window(self, _connection, session_date, *, run_type, lecture_ids):
        self.runs.append((session_date.isoformat(), lecture_ids))
        return {"status": "COMPLETED", "run_id": "R1", "provider_calls": 1, "graph_calls": 0,
                "lectures": [{"lecture_id": LECTURE, "status": "SUCCEEDED", "actions": ["RUN_QA"],
                              "error_code": None}]}


def service(resolver, *, jobs=None, teams=None, orchestrator=None, lock_free=True):
    calls, locks = [], []

    def fetch(lecture_id):
        calls.append(lecture_id)
        return teams

    def lock(_connection, lecture_id):
        locks.append((lecture_id, len(calls)))           # and how many Teams calls preceded it
        return lock_free
    retry = TeamsTranscriptRetry(resolver=resolver, jobs=jobs or Jobs(),
                                 orchestrator_factory=lambda _l: orchestrator or Orchestrator(),
                                 fetch_transcript=fetch, clock=lambda: NOW, lock_lecture=lock)
    retry.teams_calls, retry.lock_calls = calls, locks
    return retry


# --- when it is offered --------------------------------------------------------------------------

def test_the_reference_lecture_held_by_the_permanent_fix_is_offered():
    plan = service(Resolver(state())).plan(Connection(), LECTURE)
    assert plan["available"] is True
    assert plan["problem"] == "TRANSCRIPT_LISTING_NOT_SETTLED"
    assert plan["action"] == "ACQUIRE_TRANSCRIPT"
    assert plan["scope"] == "LECTURE"
    # Every cost entry is a yes/no the console renders as such.
    assert plan["cost"] == {"calls_microsoft_graph": True, "buys_model_generation": True,
                            "writes_legacy_production_row": True, "affects_other_lectures_that_day": False}


def test_a_lecture_closed_as_not_delivered_for_its_transcript_is_offered():
    closed = state(not_delivered="EMPTY_TRANSCRIPT_FOR_OCCURRENCE", blocking=None,
                   selection=("NOT_APPLICABLE", None, "EMPTY_TRANSCRIPT_FOR_OCCURRENCE"),
                   qa="NOT_APPLICABLE", next_action="NOTHING_TO_DO")
    plan = service(Resolver(closed)).plan(Connection(), LECTURE)
    assert plan["available"] is True and plan["problem"] == "EMPTY_TRANSCRIPT_FOR_OCCURRENCE"


@pytest.mark.parametrize("lecture_state, connection, reason", [
    (state(qa="COMPLETE"), Connection(), QA_ALREADY_COMPLETE),
    (state(meeting="MISSING"), Connection(), MEETING_NOT_RESOLVED),
    (state(), Connection(scheduled_end=NOW + timedelta(hours=1)), LECTURE_NOT_ENDED),
    (state(blocking="ATTENDANCE", selection=("COMPLETE", None, None)), Connection(), NO_TRANSCRIPT_PROBLEM),
    (state(suppressed=True), Connection(), SUPPRESSED_DUPLICATE),
    (state(not_delivered="QA_NON_DELIVERED", blocking=None, selection=("COMPLETE", None, None)),
     Connection(), NO_TRANSCRIPT_PROBLEM),
])
def test_it_is_not_offered_where_it_cannot_help_or_would_touch_finished_results(lecture_state, connection,
                                                                               reason):
    plan = service(Resolver(lecture_state)).plan(connection, LECTURE)
    assert plan["available"] is False and plan["reason"] == reason and plan["reason_message"]


def test_an_unknown_lecture_is_refused():
    plan = service(Resolver()).plan(Connection(exists=False), LECTURE)
    assert plan["reason"] == LECTURE_NOT_FOUND


def test_before_migration_031_nothing_is_offered():
    plan = service(Resolver(state()), jobs=Jobs(accepts=False)).plan(Connection(), LECTURE)
    assert plan["reason"] == QUEUE_UNAVAILABLE


def test_an_active_job_of_any_reason_blocks_a_second_one():
    jobs = Jobs(active={"job_id": "J0", "reason": "MANUAL_TRANSCRIPT_UPLOAD", "status": "RUNNING"})
    plan = service(Resolver(state()), jobs=jobs).plan(Connection(), LECTURE)
    assert plan["reason"] == ALREADY_QUEUED


# --- queueing ------------------------------------------------------------------------------------

def test_queueing_creates_one_teams_job_and_runs_nothing():
    jobs = Jobs()
    retry = service(Resolver(state()), jobs=jobs)
    result = retry.queue(Connection(), LECTURE, requested_by="ops.user")
    assert result["queued"] and result["created"]
    assert jobs.enqueued == [(LECTURE, REASON_TEAMS_TRANSCRIPT_RETRY, "ops.user")]
    assert retry.teams_calls == []                         # no Graph in the request


def test_a_second_click_returns_the_active_job_and_queues_nothing_new():
    active = {"job_id": "J0", "reason": REASON_TEAMS_TRANSCRIPT_RETRY, "status": "QUEUED"}
    jobs = Jobs(active=active)
    result = service(Resolver(state()), jobs=jobs).queue(Connection(), LECTURE)
    assert result["queued"] is True and result["created"] is False and result["reason"] == ALREADY_QUEUED
    assert result["job"] is active and jobs.enqueued == []


def test_queueing_a_refused_lecture_writes_nothing():
    jobs = Jobs()
    result = service(Resolver(state(qa="COMPLETE")), jobs=jobs).queue(Connection(), LECTURE)
    assert result["queued"] is False and result["reason"] == QA_ALREADY_COMPLETE
    assert jobs.enqueued == []


# --- the worker's run ----------------------------------------------------------------------------

def outcome(kind, reason):
    return TranscriptOutcome(LECTURE, kind, reason, graph_calls=2,
                             detail={"artifacts_listed": 10, "parts_fetched": 1})


RECOVERED = state(blocking="CANONICAL_CUES", selection=("COMPLETE", None, None),
                  next_action="BUILD_CANONICAL_CUES")
FINAL = state(blocking="ATTENDANCE", selection=("COMPLETE", None, None), qa="COMPLETE",
              next_action="WAIT_FOR_ATTENDANCE_SOURCE")


def test_the_run_asks_teams_once_then_resumes_the_lecture_through_the_orchestrator():
    orchestrator = Orchestrator()
    retry = service(Resolver(state(), RECOVERED, FINAL), teams=outcome(READY, "TRANSCRIPT_STORED"),
                    orchestrator=orchestrator)
    result = retry.process(Connection(), LECTURE, entry_point="QUEUE")
    assert retry.teams_calls == [LECTURE]
    assert orchestrator.runs == [("2026-11-17", [LECTURE])]
    processing = result["processing"]
    assert processing["teams"]["kind"] == READY and processing["qa_state"] == "COMPLETE"
    assert processing["graph_calls"] == 2 and processing["provider_calls"] == 1


def test_teams_still_without_a_usable_transcript_changes_nothing_and_says_what_to_do():
    orchestrator = Orchestrator()
    still = state(not_delivered="EMPTY_TRANSCRIPT_FOR_OCCURRENCE", blocking=None,
                  selection=("NOT_APPLICABLE", None, "EMPTY_TRANSCRIPT_FOR_OCCURRENCE"),
                  qa="NOT_APPLICABLE", next_action="NOTHING_TO_DO")
    retry = service(Resolver(state(), still), teams=outcome(REVIEW, "TRANSCRIPT_UNUSABLE"),
                    orchestrator=orchestrator)
    with pytest.raises(ProcessingRefused) as refused:
        retry.process(Connection(), LECTURE)
    assert refused.value.code == TEAMS_TRANSCRIPT_UNAVAILABLE
    assert "Upload transcript" in refused.value.detail
    assert refused.value.plan["teams"]["reason"] == "TRANSCRIPT_UNUSABLE"
    assert orchestrator.runs == []                         # no stage ran, nothing was bought


def test_a_lecture_busy_elsewhere_is_deferred_and_a_transient_teams_failure_is_retried():
    with pytest.raises(LectureBusy):
        service(Resolver(state()), teams=outcome(DEFERRED, "LECTURE_LOCKED")).process(Connection(), LECTURE)
    with pytest.raises(PlatformError) as transient:
        service(Resolver(state()), teams=outcome(RETRY, "GRAPH_SERVER_ERROR:503")).process(Connection(), LECTURE)
    assert transient.value.code == TEAMS_UNREACHABLE


def test_a_lecture_finished_meanwhile_is_done_without_teams_or_a_model_call():
    orchestrator = Orchestrator()
    retry = service(Resolver(state(qa="COMPLETE")), orchestrator=orchestrator)
    with pytest.raises(ProcessingRefused) as refused:
        retry.process(Connection(), LECTURE)
    assert refused.value.code == NOTHING_TO_PROCESS and refused.value.plan["qa_state"] == "COMPLETE"
    assert retry.teams_calls == [] and orchestrator.runs == []


def test_a_transcript_fixed_meanwhile_skips_teams_and_finishes_the_pipeline():
    orchestrator = Orchestrator()
    retry = service(Resolver(RECOVERED, FINAL), orchestrator=orchestrator)
    result = retry.process(Connection(), LECTURE)
    assert retry.teams_calls == [] and orchestrator.runs == [("2026-11-17", [LECTURE])]
    assert result["processing"]["teams"] is None


def test_the_operator_sees_plain_words_for_teams_answers():
    assert "too short or empty" in teams_reason_text("TRANSCRIPT_UNUSABLE")
    assert "refused" in teams_reason_text("GRAPH_REJECTED:400")
    assert "no transcript for this lecture" in teams_reason_text("NO_TRANSCRIPT_FOR_OCCURRENCE:NO_SAME_DAY_CANDIDATES")
    assert transcript_problem(state(blocking="TRANSCRIPT")) is not None


# --- the lecture lock (production job 0f25407b, 2026-11-20) ------------------------------------------

def test_the_lecture_lock_is_taken_once_before_teams_is_asked():
    retry = service(Resolver(state(), RECOVERED, FINAL), teams=outcome(READY, "TRANSCRIPT_STORED"))
    retry.process(Connection(), LECTURE)
    assert retry.lock_calls == [(LECTURE, 0)] and retry.teams_calls == [LECTURE]


def test_a_lecture_held_elsewhere_is_deferred_before_teams_or_any_stage():
    orchestrator = Orchestrator()
    retry = service(Resolver(state()), teams=outcome(READY, "TRANSCRIPT_STORED"),
                    orchestrator=orchestrator, lock_free=False)
    with pytest.raises(LectureBusy):
        retry.process(Connection(), LECTURE)
    assert retry.teams_calls == [] and orchestrator.runs == []


def test_a_refused_plan_takes_no_lock():
    retry = service(Resolver(state(qa="COMPLETE")))
    with pytest.raises(ProcessingRefused):
        retry.process(Connection(), LECTURE)
    assert retry.lock_calls == []


class _TxConnection:
    def __init__(self, granted=True, autocommit=False):
        self.autocommit, self.granted, self.sql = autocommit, granted, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        granted = self.granted

        class Cursor:
            def fetchone(self):
                return (granted,)
        return Cursor()


def test_the_lock_is_the_transaction_form_of_the_shared_lecture_lock():
    from app.orchestration.locks import LOCK_NAMESPACE, lock_key
    connection = _TxConnection()
    assert lock_for_transaction(connection, LECTURE) is True
    assert connection.sql == [("SELECT pg_try_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, lock_key(LECTURE)))]
    assert lock_for_transaction(_TxConnection(granted=False), LECTURE) is False


def test_the_lock_is_refused_on_an_autocommit_connection_where_it_would_protect_nothing():
    connection = _TxConnection(autocommit=True)
    with pytest.raises(PlatformError):
        lock_for_transaction(connection, LECTURE)
    assert connection.sql == []


def test_the_teams_fetch_takes_no_lock_of_its_own():
    held = LockHeldByCaller()
    assert held.try_acquire(object(), LECTURE) is True and held.release(object(), LECTURE) is True
