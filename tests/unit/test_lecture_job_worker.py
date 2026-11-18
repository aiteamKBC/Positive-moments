"""
The lecture job worker's decisions, with fakes for the queue, the recovery and the database.
What it must never do: run a job in the quiet window, run after the attempts are spent, call a
deferral caused by the nightly cycle a failed attempt, or call a lecture done while QA is not.
"""
from contextlib import contextmanager

from app.common.errors import PlatformError
from app.orchestration.lecture_jobs import (
    ATTEMPTS_EXHAUSTED,
    ENTRY_POINT,
    QA_NOT_COMPLETED,
    LectureJobWorker,
)
from app.orchestration.locks import SchedulerCycleBusy
from app.orchestration.transcript_recovery import (
    NOTHING_TO_PROCESS,
    WAITING_FOR_TRANSCRIPT_PARTS,
    ProcessingRefused,
)


class Conn:
    def commit(self):
        pass

    def rollback(self):
        pass


class Queue:
    def __init__(self, job=None, installed=True):
        self.job = job
        self.installed = installed
        self.settled = []
        self.claims = 0

    def available(self, _c):
        return self.installed

    def claim(self, _c, **_k):
        self.claims += 1
        job, self.job = self.job, None
        return job

    def complete(self, _c, job_id, **k):
        self.settled.append(("COMPLETED", job_id, k))

    def fail(self, _c, job_id, **k):
        self.settled.append(("FAILED", job_id, k))

    def retry_later(self, _c, job_id, **k):
        self.settled.append(("QUEUED", job_id, k))


class Recovery:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def process(self, _c, lecture_id, *, entry_point):
        self.calls.append((lecture_id, entry_point))
        if self.raises:
            raise self.raises
        return self.result


def job(attempt=1, max_attempts=3):
    return {"job_id": "J1", "lecture_id": "L1", "attempt_count": attempt, "max_attempts": max_attempts}


def processed(qa_state="COMPLETE", final_action="WAIT_FOR_ATTENDANCE_SOURCE"):
    return {"processing": {"status": "COMPLETED", "final_action": final_action, "qa_state": qa_state,
                           "final_blocking_stage": "ATTENDANCE", "run_id": "R1", "provider_calls": 1,
                           "graph_calls": 0, "actions": ["RUN_QA"], "error_code": None}}


def worker(queue, recovery, quiet=False):
    @contextmanager
    def factory():
        yield Conn()
    return LectureJobWorker(recovery=recovery, connection_factory=factory, repository=queue,
                            in_quiet_window=lambda: quiet, worker_id="w")


def test_a_lecture_whose_qa_settled_completes_the_job():
    queue, recovery = Queue(job()), Recovery(processed())
    assert worker(queue, recovery).run_next()["status"] == "COMPLETED"
    assert recovery.calls == [("L1", ENTRY_POINT)]
    assert queue.settled[0][0] == "COMPLETED" and queue.settled[0][2]["pipeline_run_id"] == "R1"


def test_qa_still_ahead_is_retried_with_backoff_while_attempts_remain():
    queue = Queue(job(attempt=2))
    worker(queue, Recovery(processed(qa_state="REVIEW_REQUIRED", final_action="RUN_QA"))).run_next()
    verdict, _, detail = queue.settled[0]
    assert verdict == "QUEUED" and detail["error_code"] == QA_NOT_COMPLETED
    assert detail["delay_seconds"] == 2 * 15 * 60 and detail["refund_attempt"] is False


def test_qa_still_ahead_on_the_last_attempt_fails_the_job():
    queue = Queue(job(attempt=3))
    worker(queue, Recovery(processed(qa_state="REVIEW_REQUIRED", final_action="RUN_QA"))).run_next()
    assert queue.settled[0][0] == "FAILED"


def test_qa_that_needs_a_person_fails_the_job_rather_than_completing_it():
    queue = Queue(job())
    worker(queue, Recovery(processed(qa_state="REVIEW_REQUIRED",
                                     final_action="MANUAL_REVIEW_REQUIRED"))).run_next()
    assert queue.settled[0][0] == "FAILED" and queue.settled[0][2]["error_code"] == "QA_REVIEW_REQUIRED"


def test_the_nightly_cycle_holding_the_lock_defers_without_spending_an_attempt():
    queue = Queue(job())
    worker(queue, Recovery(raises=SchedulerCycleBusy("busy"))).run_next()
    verdict, _, detail = queue.settled[0]
    assert verdict == "QUEUED" and detail["refund_attempt"] is True
    assert detail["error_code"] == "SCHEDULER_CYCLE_RUNNING"


def test_an_already_evaluated_lecture_completes_without_running():
    queue = Queue(job())
    refusal = ProcessingRefused(NOTHING_TO_PROCESS, {"qa_state": "COMPLETE", "next_action": "NOTHING_TO_DO"})
    worker(queue, Recovery(raises=refusal)).run_next()
    assert queue.settled[0][0] == "COMPLETED"


def test_nothing_to_process_before_qa_is_a_failure_not_a_completion():
    queue = Queue(job())
    refusal = ProcessingRefused(NOTHING_TO_PROCESS, {"qa_state": "MISSING",
                                                     "next_action": "WAIT_FOR_SPEAKERS"})
    worker(queue, Recovery(raises=refusal)).run_next()
    assert queue.settled[0][0] == "FAILED" and queue.settled[0][2]["error_code"] == NOTHING_TO_PROCESS


def test_a_refusal_no_retry_can_change_fails_at_once():
    queue = Queue(job(attempt=1))
    worker(queue, Recovery(raises=ProcessingRefused(WAITING_FOR_TRANSCRIPT_PARTS, {}))).run_next()
    assert queue.settled[0][0] == "FAILED"
    assert queue.settled[0][2]["error_code"] == WAITING_FOR_TRANSCRIPT_PARTS


def test_a_platform_error_is_retried():
    queue = Queue(job(attempt=1))
    worker(queue, Recovery(raises=PlatformError("DATABASE_ERROR", "x"))).run_next()
    assert queue.settled[0][0] == "QUEUED" and queue.settled[0][2]["error_code"] == "DATABASE_ERROR"


def test_spent_attempts_fail_without_running():
    queue, recovery = Queue(job(attempt=4, max_attempts=3)), Recovery(processed())
    worker(queue, recovery).run_next()
    assert recovery.calls == [] and queue.settled[0][2]["error_code"] == ATTEMPTS_EXHAUSTED


def test_nothing_is_claimed_in_the_quiet_window_or_before_migration_030():
    for queue, quiet in ((Queue(job()), True), (Queue(job(), installed=False), False)):
        recovery = Recovery(processed())
        assert worker(queue, recovery, quiet=quiet).run_next() is None
        assert queue.claims == 0 and recovery.calls == []


def test_drain_runs_jobs_until_none_is_due_and_never_raises():
    queue = Queue(job())
    w = worker(queue, Recovery(processed()))
    assert w.drain() == 1

    def boom():
        raise RuntimeError("boom")
    w.run_next = boom
    assert w.drain() == 0


def test_the_backfill_service_is_built_with_the_lecture_job_worker():
    """
    The production wiring `backfill-runner` uses (factory.build_backfill_runner): the service that
    is already deployed and restarted by systemd is the one that works the queue, with the QA
    provider allowed, the real cycle and lecture locks, the run narrowed to the one lecture and
    the day-wide Graph transcript sweep refused.
    """
    from app.config.settings import Settings
    from app.orchestration.factory import build_backfill_runner
    from app.orchestration.locks import LectureLockManager, SchedulerCycleLock

    import dataclasses
    # Placeholder Graph values: the backfill orchestrator's discovery is built, never called.
    settings = dataclasses.replace(
        Settings.from_environment(), graph_tenant_id="tenant.invalid", graph_client_id="client.invalid",
        graph_client_secret="not-a-secret", graph_base_url="https://graph.invalid/v1.0")
    runner = build_backfill_runner(settings, connection_factory=lambda: None)
    jobs = runner.lecture_jobs
    assert isinstance(jobs, LectureJobWorker)
    assert jobs.recovery.allow_provider is True
    orchestrator = jobs.recovery.orchestrator_factory("5695cdfd-027c-5cc2-8a70-eddcc4b0e893")
    assert isinstance(orchestrator.cycle_lock, SchedulerCycleLock)
    assert isinstance(orchestrator.lock_manager, LectureLockManager)
    assert orchestrator.runner.allow_provider is True
    assert orchestrator.runner.lecture_scope == frozenset({"5695cdfd-027c-5cc2-8a70-eddcc4b0e893"})
    assert orchestrator.discovery_service is None          # no calendar discovery for a lecture job
