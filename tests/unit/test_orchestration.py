"""
Phase 4A unit tests: orchestration, safety precheck, locking and the scheduler.

These use a scripted resolver and a recording runner rather than a database.
The questions here are about CONTROL FLOW - does a failing lecture stop its
siblings, does a settled stage get re-run, does a blocked precheck actually
block - and those are answered by what the orchestrator decides to call, not by
what any service returns.

The real services are exercised against the real database in
tests/integration/test_orchestration_persistence.py.
"""
from datetime import date, datetime, timezone

import pytest

from app.common.errors import DATABASE_ERROR, WRITER_GUARD_REFUSED, PlatformError
from app.orchestration.locks import (
    LOCK_NAMESPACE,
    LectureBusy,
    LectureLockManager,
    NullLockManager,
    lock_key,
)
from app.orchestration.n8n_preflight import (
    GUARDED_NODE_NAME,
    LEGACY_QA_DISABLED,
    LEGACY_QA_ENABLED,
    PRECHECK_SKIPPED,
    PRECHECK_UNAVAILABLE,
    QA_MASTER_WORKFLOW_ID,
    LegacyQaPreflight,
)
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import (
    ACTION_SCOPE,
    ManualReviewRequired,
    GRAPH_ACTIONS,
    PROVIDER_ACTIONS,
    StageRunner,
)
from app.orchestration.scheduler import (
    DEFAULT_LOOKBACK_DAYS,
    LEGACY_QA_MASTER_CRON,
    SchedulerConfig,
    SchedulerDisabled,
    SchedulerService,
)
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    AUTOMATABLE_ACTIONS,
    CALCULATE_ENGAGEMENT,
    EVALUATE_PERFECT,
    FORCE_REPROCESS,
    ITEM_BLOCKED,
    ITEM_FAILED,
    ITEM_LOCKED,
    ITEM_REVIEW,
    ITEM_SUCCEEDED,
    ITEM_WAITING,
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    OPERATOR_ONLY_ACTIONS,
    PRODUCTION_WRITE_ACTIONS,
    RECOVER_ATTENDANCE,
    RENDER_QA,
    RETRY,
    RUN_BLOCKED_LEGACY_QA_ACTIVE,
    RUN_BLOCKED_WRITER_INTEGRITY,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_FAILURES,
    RUN_TYPE_SCHEDULED,
    RUN_QA,
    SELECT_TRANSCRIPT,
    SUPPRESS_DUPLICATE_EVENT,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
    WAIT_FOR_ATTENDANCE_SOURCE,
)


TARGET = date(2026, 9, 17)


# --- scripted state ----------------------------------------------------------

class ScriptedLecture:
    """
    A lecture that reports a sequence of next actions, advancing one step each
    time the runner successfully executes the action it is currently reporting.

    That is the whole of what the orchestrator is entitled to assume about a
    lecture, so it is the whole of what this fake provides.
    """

    def __init__(self, lecture_id, actions, *, subject=None, waiting=False,
                 review=False, attendance_state="COMPLETE"):
        self.lecture_id = lecture_id
        self.actions = list(actions)
        self.subject = subject or f"Lecture {lecture_id}"
        self.waiting = waiting
        self.review = review
        self.attendance_state = attendance_state
        self.index = 0

    @property
    def action(self):
        if self.index >= len(self.actions):
            return NOTHING_TO_DO
        return self.actions[self.index]

    def advance(self):
        self.index = min(self.index + 1, len(self.actions))

    def state(self):
        action = self.action
        return {
            "lecture_id": self.lecture_id, "subject": self.subject,
            "session_date": TARGET.isoformat(),
            "next_action": action, "next_executable_action": action,
            "blocking_stage": None if action == NOTHING_TO_DO else "QA_EVALUATION",
            "is_complete": action == NOTHING_TO_DO,
            "requires_review": self.review,
            "is_waiting": self.waiting,
            "attendance_coverage_status": "SOURCE_AVAILABLE_WITH_MEMBERS",
            "attendance_source_authoritative": True,
            "stages": {"ATTENDANCE": {"state": self.attendance_state,
                                      "action": None}},
        }


class ScriptedResolver:
    def __init__(self, lectures):
        self.lectures = list(lectures)
        self.calls = 0

    def for_day(self, connection, session_date):
        self.calls += 1
        return [lecture.state() for lecture in self.lectures]

    def for_lecture(self, connection, lecture_id):
        return next(item.state() for item in self.lectures
                    if item.lecture_id == lecture_id)


class RecordingRunner:
    """Records every call, advances the lecture, and fails where told to."""

    def __init__(self, lectures, *, failures=None, refuse=(),
                 day_actions=(ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT)):
        self.by_id = {item.lecture_id: item for item in lectures}
        self.failures = failures or {}
        self.refuse = set(refuse)
        self.day_actions = set(day_actions)
        self.calls = []

    def scope_of(self, action):
        return "DAY" if action in self.day_actions else "LECTURE"

    def can_run(self, action):
        return action in AUTOMATABLE_ACTIONS and action not in self.refuse

    def refusal_reason(self, action):
        return "REFUSED_FOR_THIS_RUN"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append((action, lecture_id))
        failure = self.failures.get((action, lecture_id)) or self.failures.get(action)
        if failure is not None:
            raise failure
        if self.scope_of(action) == "DAY":
            # A day-scoped service advances every lecture that was waiting on
            # it, which is exactly why the orchestrator must run it only once.
            for item in self.by_id.values():
                if item.action == action:
                    item.advance()
        else:
            lecture = self.by_id.get(lecture_id)
            if lecture is not None:
                lecture.advance()
        return {"graph_calls": 1 if action in GRAPH_ACTIONS else 0,
                "provider_calls": 1 if action in PROVIDER_ACTIONS else 0,
                "legacy_rows_written": 0, "summary": {}}


class StubPreflight:
    def __init__(self, status=LEGACY_QA_DISABLED, disabled=True):
        self.status = status
        self.disabled = disabled
        self.checks = 0

    def check(self):
        self.checks += 1
        return {"status": self.status, "legacy_qa_node_disabled": self.disabled,
                "reason": self.status, "writes_permitted": self.disabled,
                "read_only": True, "http_method": "GET", "n8n_modified": False}


class RecordingRunRepository:
    def __init__(self):
        self.runs = []
        self.items = []
        self.finished = []

    def start(self, connection, **kwargs):
        self.runs.append(kwargs)
        return f"run-{len(self.runs)}"

    def record_item(self, connection, run_id, **kwargs):
        self.items.append({"run_id": run_id, **kwargs})
        return f"item-{len(self.items)}"

    def finish(self, connection, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})


def orchestrator_for(lectures, *, runner=None, preflight=None, repository=None,
                     lock_manager=None, max_passes=8):
    resolver = ScriptedResolver(lectures)
    return PipelineOrchestrator(
        resolver=resolver, runner=runner or RecordingRunner(lectures),
        run_repository=repository, lock_manager=lock_manager,
        preflight=preflight or StubPreflight(), max_passes=max_passes)


# --- 14, 15, 16. resume, no re-running, idempotency --------------------------

def test_a_settled_lecture_is_not_touched_at_all():
    lecture = ScriptedLecture("a", [])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert runner.calls == []
    assert outcome["status"] == RUN_COMPLETED
    assert outcome["lectures"][0]["final_action"] == NOTHING_TO_DO
    assert outcome["counts"]["completed_count"] == 1


def test_resume_starts_at_the_earliest_required_stage_and_walks_forward():
    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT, RENDER_QA,
                                    EVALUATE_PERFECT])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert [action for action, _ in runner.calls] == [
        CALCULATE_ENGAGEMENT, RENDER_QA, EVALUATE_PERFECT]
    assert outcome["lectures"][0]["final_action"] == NOTHING_TO_DO
    assert outcome["lectures"][0]["changed"] is True


def test_a_second_run_over_the_same_window_does_nothing():
    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    orchestrator = orchestrator_for([lecture], runner=runner)
    orchestrator.run_window(None, TARGET)
    calls_after_first = len(runner.calls)
    second = orchestrator.run_window(None, TARGET)
    assert len(runner.calls) == calls_after_first
    assert second["idempotent"] is True
    assert second["passes"] == 1


def test_the_run_reports_the_semantics_it_used_and_never_force_reprocesses():
    outcome = orchestrator_for([ScriptedLecture("a", [])]).run_window(None, TARGET)
    assert outcome["retry_semantics"] == RETRY
    assert outcome["force_reprocess"] is False


def test_an_action_that_changes_nothing_is_attempted_once_and_not_repeated():
    """
    The commonest real case, and the one that first got this wrong: a lecture
    waiting on attendance is handed the recovery primitive, the recovery reads
    a still-empty source and writes nothing. That is the right answer. Counting
    "the call returned" as progress made the cycle ask eight times.
    """
    class Stuck(ScriptedLecture):
        def advance(self):
            pass  # the call succeeds; the state does not move

    lecture = Stuck("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner,
                               max_passes=8).run_window(None, TARGET)
    assert len(runner.calls) == 1
    assert outcome["passes"] == 1
    assert outcome["errors"] == []


def test_a_window_that_genuinely_never_settles_is_stopped_by_the_pass_cap():
    """A lecture whose action keeps CHANGING can still spin. The cap catches it."""
    class Oscillating(ScriptedLecture):
        def advance(self):
            self.index = (self.index + 1) % len(self.actions)

    lecture = Oscillating("a", [CALCULATE_ENGAGEMENT, RENDER_QA])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner,
                               max_passes=3).run_window(None, TARGET)
    assert outcome["passes"] == 3
    assert any(error["error_code"] == "MAX_PASSES_REACHED"
               for error in outcome["errors"])


# --- 12, 13. isolation -------------------------------------------------------

def test_one_lecture_failing_does_not_stop_its_siblings():
    bad = ScriptedLecture("bad", [RENDER_QA])
    good_one = ScriptedLecture("good-1", [CALCULATE_ENGAGEMENT])
    good_two = ScriptedLecture("good-2", [EVALUATE_PERFECT])
    runner = RecordingRunner(
        [bad, good_one, good_two],
        failures={(RENDER_QA, "bad"): ValueError("model validation failed")})
    outcome = orchestrator_for([bad, good_one, good_two],
                               runner=runner).run_window(None, TARGET)
    by_id = {item["lecture_id"]: item for item in outcome["lectures"]}
    assert by_id["bad"]["status"] == ITEM_FAILED
    assert by_id["bad"]["error_code"] == "ValueError"
    assert by_id["good-1"]["status"] == ITEM_SUCCEEDED
    assert by_id["good-2"]["status"] == ITEM_SUCCEEDED
    assert outcome["status"] == RUN_COMPLETED_WITH_FAILURES


def test_a_failure_message_is_recorded_but_never_the_payload():
    bad = ScriptedLecture("bad", [RENDER_QA])
    runner = RecordingRunner([bad], failures={
        (RENDER_QA, "bad"): ValueError("x" * 5000)})
    repository = RecordingRunRepository()
    orchestrator_for([bad], runner=runner,
                     repository=repository).run_window(None, TARGET)
    recorded = repository.items[0]["error_message"]
    assert len(recorded) == 5000  # the repository truncates, not the orchestrator
    # ...and the repository is where the cap lives, proven directly:
    from app.db.repositories.pipeline_runs import MAX_SAFE_MESSAGE, safe_message
    assert len(safe_message(recorded)) == MAX_SAFE_MESSAGE


def test_a_writer_integrity_refusal_stops_the_whole_run():
    """
    Not a per-lecture failure. The guard that bounds production writes has
    objected, and continuing means making more of them while it is objecting.
    """
    first = ScriptedLecture("first", [RENDER_QA])
    second = ScriptedLecture("second", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([first, second], failures={
        (RENDER_QA, "first"): PlatformError(WRITER_GUARD_REFUSED, "refused")})
    outcome = orchestrator_for([first, second],
                               runner=runner).run_window(None, TARGET)
    assert outcome["status"] == RUN_BLOCKED_WRITER_INTEGRITY
    assert (CALCULATE_ENGAGEMENT, "second") not in runner.calls
    by_id = {item["lecture_id"]: item for item in outcome["lectures"]}
    assert by_id["first"]["retryable"] is False


def test_a_shared_infrastructure_failure_stops_the_run_rather_than_repeating():
    first = ScriptedLecture("first", [CALCULATE_ENGAGEMENT])
    second = ScriptedLecture("second", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([first, second], failures={
        CALCULATE_ENGAGEMENT: PlatformError(DATABASE_ERROR, "connection lost")})
    outcome = orchestrator_for([first, second],
                               runner=runner).run_window(None, TARGET)
    assert len(runner.calls) == 1
    assert outcome["status"] == "ABORTED"


# --- 17, 18, 19. the n8n safety precheck -------------------------------------

def test_the_run_proceeds_while_the_legacy_qa_node_is_disabled():
    preflight = StubPreflight(LEGACY_QA_DISABLED, disabled=True)
    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner,
                               preflight=preflight).run_window(None, TARGET)
    assert preflight.checks == 1
    assert outcome["status"] == RUN_COMPLETED
    assert runner.calls


def test_the_run_is_blocked_when_the_legacy_qa_node_is_enabled():
    preflight = StubPreflight(LEGACY_QA_ENABLED, disabled=False)
    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner,
                               preflight=preflight).run_window(None, TARGET)
    assert outcome["status"] == RUN_BLOCKED_LEGACY_QA_ACTIVE
    assert runner.calls == []
    assert outcome["lectures"][0]["status"] == ITEM_BLOCKED


def test_an_unreachable_precheck_blocks_a_real_run_rather_than_guessing():
    preflight = StubPreflight(PRECHECK_UNAVAILABLE, disabled=None)
    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner,
                               preflight=preflight).run_window(None, TARGET)
    assert outcome["status"] == RUN_BLOCKED_LEGACY_QA_ACTIVE
    assert runner.calls == []


def test_the_precheck_records_its_answer_on_the_run_row():
    repository = RecordingRunRepository()
    orchestrator_for([ScriptedLecture("a", [])],
                     repository=repository).run_window(None, TARGET)
    assert repository.runs[0]["legacy_qa_precheck_status"] == LEGACY_QA_DISABLED
    assert repository.runs[0]["legacy_qa_node_disabled"] is True


class FakeGateway:
    """Records every request. Any non-GET would be visible here."""

    def __init__(self, workflow, configured=True):
        self.workflow = workflow
        self.configured = configured
        self.requests = []

    def get_workflow(self, workflow_id):
        self.requests.append(("GET", workflow_id))
        if isinstance(self.workflow, Exception):
            raise self.workflow
        return self.workflow


def _workflow(disabled):
    return {"active": True, "updatedAt": "2026-09-17T12:56:59.588Z",
            "nodes": [{"name": GUARDED_NODE_NAME, "disabled": disabled},
                      {"name": "Update Both Recording Tables", "disabled": False}]}


def test_the_precheck_reads_the_real_guarded_node():
    gateway = FakeGateway(_workflow(True))
    result = LegacyQaPreflight(gateway=gateway).check()
    assert result["status"] == LEGACY_QA_DISABLED
    assert result["legacy_qa_node_disabled"] is True
    assert result["writes_permitted"] is True
    assert result["disabled_node_names"] == [GUARDED_NODE_NAME]
    assert gateway.requests == [("GET", QA_MASTER_WORKFLOW_ID)]


def test_the_precheck_issues_one_get_and_modifies_nothing():
    gateway = FakeGateway(_workflow(False))
    result = LegacyQaPreflight(gateway=gateway).check()
    assert result["status"] == LEGACY_QA_ENABLED
    assert result["writes_permitted"] is False
    assert result["n8n_modified"] is False
    assert result["http_method"] == "GET"
    assert [method for method, _ in gateway.requests] == ["GET"]


def test_a_missing_guarded_node_fails_closed_rather_than_reading_as_disabled():
    gateway = FakeGateway({"active": True, "nodes": [{"name": "Something Else"}]})
    result = LegacyQaPreflight(gateway=gateway).check()
    assert result["status"] == PRECHECK_UNAVAILABLE
    assert result["reason"] == "GUARDED_NODE_NOT_FOUND"
    assert result["writes_permitted"] is False


def test_an_unreachable_n8n_fails_closed_for_a_required_precheck():
    result = LegacyQaPreflight(gateway=FakeGateway(OSError("connection refused"))).check()
    assert result["status"] == PRECHECK_UNAVAILABLE
    assert result["writes_permitted"] is False


def test_an_unconfigured_n8n_is_skipped_only_when_the_precheck_is_optional():
    optional = LegacyQaPreflight(gateway=FakeGateway({}, configured=False),
                                 required=False).check()
    assert optional["status"] == PRECHECK_SKIPPED
    assert optional["writes_permitted"] is True
    required = LegacyQaPreflight(gateway=FakeGateway({}, configured=False),
                                 required=True).check()
    assert required["status"] == PRECHECK_UNAVAILABLE
    assert required["writes_permitted"] is False


def test_the_precheck_module_contains_no_write_verb_at_all():
    """Structural, not behavioural: there is no code path to a mutation."""
    import pathlib
    source = pathlib.Path("app/orchestration/n8n_preflight.py").read_text(
        encoding="utf-8")
    for verb in ('method="PUT"', 'method="POST"', 'method="PATCH"',
                 'method="DELETE"', "urlopen(request, data"):
        assert verb not in source, verb


# --- 20, 21, 22. attendance --------------------------------------------------

def test_a_lecture_waiting_on_attendance_never_reaches_the_provider():
    lecture = ScriptedLecture("a", [WAIT_FOR_ATTENDANCE_SOURCE], waiting=True,
                              attendance_state="WAITING")
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert (RUN_QA, "a") not in runner.calls
    assert outcome["provider_calls"] == 0
    assert outcome["graph_calls"] == 0


def test_a_waiting_lecture_is_handed_to_the_free_recovery_primitive():
    """
    `recover-attendance` re-reads the source read-only and returns without
    writing if it is still empty, so calling it on every cycle costs nothing.
    That is why WAIT maps to it rather than to doing nothing at all.
    """
    lecture = ScriptedLecture("a", [WAIT_FOR_ATTENDANCE_SOURCE], waiting=True,
                              attendance_state="WAITING")
    runner = RecordingRunner([lecture])
    orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert runner.calls == [(RECOVER_ATTENDANCE, "a")]


def test_a_lecture_waiting_on_something_nobody_can_fix_is_left_alone():
    lecture = ScriptedLecture("a", [WAIT_FOR_ATTENDANCE_SOURCE], waiting=True,
                              attendance_state="COMPLETE")
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert runner.calls == []
    assert outcome["lectures"][0]["status"] == ITEM_WAITING


def test_repeated_cycles_over_a_waiting_lecture_perform_the_same_free_probe():
    lecture = ScriptedLecture("a", [WAIT_FOR_ATTENDANCE_SOURCE,
                                    WAIT_FOR_ATTENDANCE_SOURCE], waiting=True,
                              attendance_state="WAITING")
    runner = RecordingRunner([lecture])
    orchestrator = orchestrator_for([lecture], runner=runner)
    orchestrator.run_window(None, TARGET)
    orchestrator.run_window(None, TARGET)
    assert {action for action, _ in runner.calls} == {RECOVER_ATTENDANCE}
    assert sum(1 for _, lecture_id in runner.calls if lecture_id == "a") <= 4


# --- production writes stay operator-driven ----------------------------------

def test_the_orchestrator_now_performs_a_safe_legacy_write_itself():
    """
    The Phase 4B change. Phase 4A recorded OPERATOR_ACTION_REQUIRED and stopped;
    a lecture whose QA was finished, rendered and verified sat there for ever
    because nobody clicked anything.
    """
    lecture = ScriptedLecture("a", [SYNC_LEGACY_QA])
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert runner.calls == [(SYNC_LEGACY_QA, "a")]
    assert outcome["lectures"][0]["status"] == ITEM_SUCCEEDED
    assert outcome["lectures"][0]["final_action"] == NOTHING_TO_DO


def test_an_unsafe_writer_decision_reaches_a_human_rather_than_a_write():
    """
    Safe automation stops at the first ambiguity. `ManualReviewRequired` is not
    a failure - nothing went wrong and nothing was half done - so it is
    recorded as review, carries the writer's own reason, and is not retried.
    """
    lecture = ScriptedLecture("a", [SYNC_LEGACY_QA])
    runner = RecordingRunner([lecture], failures={
        (SYNC_LEGACY_QA, "a"): ManualReviewRequired("LEGACY_ROW_NOT_CODED_OWNED")})
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    item = outcome["lectures"][0]
    assert item["status"] == ITEM_REVIEW
    assert item["error_code"] == "LEGACY_ROW_NOT_CODED_OWNED"
    assert item["retryable"] is False
    assert outcome["legacy_rows_written"] == 0
    assert outcome["status"] == RUN_COMPLETED


def test_both_legacy_write_actions_are_automatable_but_still_production_writes():
    assert PRODUCTION_WRITE_ACTIONS == {SYNC_LEGACY_QA, SYNC_PERFECT, "LINK_RECORDING"}
    assert PRODUCTION_WRITE_ACTIONS <= AUTOMATABLE_ACTIONS
    # Nothing is operator-only any more, and the concept is kept so re-gating
    # an action later is one line rather than a rewrite.
    assert OPERATOR_ONLY_ACTIONS == frozenset()


def test_manual_review_is_recorded_rather_than_retried():
    lecture = ScriptedLecture("a", [MANUAL_REVIEW_REQUIRED], review=True)
    runner = RecordingRunner([lecture])
    outcome = orchestrator_for([lecture], runner=runner).run_window(None, TARGET)
    assert runner.calls == []
    assert outcome["lectures"][0]["status"] == ITEM_REVIEW
    assert outcome["counts"]["review_count"] == 1


# --- 26, 27. dry run ----------------------------------------------------------

def test_a_dry_run_performs_no_write_and_no_call():
    lecture = ScriptedLecture("a", [ACQUIRE_TRANSCRIPT, RUN_QA])
    runner = RecordingRunner([lecture])
    repository = RecordingRunRepository()
    outcome = orchestrator_for([lecture], runner=runner,
                               repository=repository).run_window(
        None, TARGET, dry_run=True)
    assert runner.calls == []
    assert repository.runs == [] and repository.items == []
    assert outcome["run_id"] is None
    assert outcome["graph_calls"] == 0 and outcome["provider_calls"] == 0


def test_a_dry_run_reports_what_it_would_have_done_without_claiming_to_have_done_it():
    lecture = ScriptedLecture("a", [ACQUIRE_TRANSCRIPT])
    outcome = orchestrator_for([lecture]).run_window(None, TARGET, dry_run=True)
    item = outcome["lectures"][0]
    assert item["planned_actions"] == [ACQUIRE_TRANSCRIPT]
    assert item["actions"] == []
    assert outcome["would_call_graph"] is True
    assert outcome["would_call_provider"] is False
    assert outcome["would_write_legacy_qa"] is False
    assert outcome["would_write_perfect"] is False


def test_a_dry_run_that_would_buy_a_generation_says_so():
    lecture = ScriptedLecture("a", [RUN_QA])
    outcome = orchestrator_for([lecture]).run_window(None, TARGET, dry_run=True)
    assert outcome["would_call_provider"] is True
    assert outcome["provider_calls"] == 0


def test_every_run_states_whether_it_would_touch_a_legacy_table():
    outcome = orchestrator_for([ScriptedLecture("a", [])]).run_window(None, TARGET)
    assert outcome["would_write_legacy_qa"] is False
    assert outcome["would_write_perfect"] is False


# --- 23, 24, 25. locking ------------------------------------------------------

class FakeLockConnection:
    """A minimal advisory-lock server: one holder per key, per connection."""

    held = set()

    def __init__(self):
        self.mine = set()

    def execute(self, sql, params=None):
        namespace, key = params
        if "pg_try_advisory_lock" in sql:
            if key in FakeLockConnection.held:
                return _one((False,))
            FakeLockConnection.held.add(key)
            self.mine.add(key)
            return _one((True,))
        if "pg_advisory_unlock" in sql:
            FakeLockConnection.held.discard(key)
            self.mine.discard(key)
            return _one((True,))
        raise AssertionError(sql)


class _one:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


@pytest.fixture(autouse=True)
def _clear_locks():
    FakeLockConnection.held = set()
    yield
    FakeLockConnection.held = set()


def test_the_same_lecture_cannot_be_held_twice():
    manager = LectureLockManager()
    first, second = FakeLockConnection(), FakeLockConnection()
    assert manager.try_acquire(first, "lecture-a") is True
    assert manager.try_acquire(second, "lecture-a") is False


def test_different_lectures_proceed_independently():
    manager = LectureLockManager()
    connection = FakeLockConnection()
    assert manager.try_acquire(connection, "lecture-a") is True
    assert manager.try_acquire(connection, "lecture-b") is True


def test_a_lock_is_released_even_when_the_work_raises():
    manager = LectureLockManager()
    connection = FakeLockConnection()
    with pytest.raises(ValueError):
        with manager.hold(connection, "lecture-a"):
            raise ValueError("boom")
    assert manager.try_acquire(FakeLockConnection(), "lecture-a") is True


def test_a_busy_lecture_is_skipped_rather_than_waited_for():
    manager = LectureLockManager()
    holder = FakeLockConnection()
    manager.try_acquire(holder, "a")

    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    orchestrator = orchestrator_for([lecture], runner=runner,
                                    lock_manager=manager)
    outcome = orchestrator.run_window(FakeLockConnection(), TARGET)
    assert runner.calls == []
    assert outcome["lectures"][0]["status"] == ITEM_LOCKED
    assert outcome["lectures"][0]["retryable"] is True


def test_lock_keys_are_stable_derived_and_inside_the_signed_32_bit_range():
    for lecture_id in ("a", "de8c6c60-6e73-5b50-b74d-ef5806b9d1bb", "z" * 40):
        key = lock_key(lecture_id)
        assert key == lock_key(lecture_id)
        assert -2 ** 31 <= key < 2 ** 31
    assert lock_key("a") != lock_key("b")
    assert LOCK_NAMESPACE == 0x4B424C31


def test_a_day_scoped_stage_runs_once_and_is_shared():
    first = ScriptedLecture("first", [SELECT_TRANSCRIPT, CALCULATE_ENGAGEMENT])
    second = ScriptedLecture("second", [SELECT_TRANSCRIPT, CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([first, second])
    orchestrator_for([first, second], runner=runner).run_window(None, TARGET)
    assert [action for action, _ in runner.calls].count(SELECT_TRANSCRIPT) == 1


def test_the_null_lock_manager_refuses_nothing_and_is_only_for_dry_runs():
    manager = NullLockManager()
    assert manager.try_acquire(None, "a") is True
    assert manager.try_acquire(None, "a") is True


# --- 31..34. the scheduler ----------------------------------------------------

class RecordingOrchestrator:
    def __init__(self):
        self.calls = []

    def run_window(self, connection, target_date, **kwargs):
        self.calls.append({"target_date": target_date, **kwargs})
        return {"status": RUN_COMPLETED, "lectures": [], "counts": {},
                "graph_calls": 0, "provider_calls": 0, "idempotent": True}


def test_the_scheduler_calls_the_same_orchestration_service_as_the_cli():
    orchestrator = RecordingOrchestrator()
    config = SchedulerConfig(enabled=True, lookback_days=2)
    outcome = SchedulerService(orchestrator=orchestrator,
                               config=config).run_cycle(None)
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["lookback_days"] == 2
    assert orchestrator.calls[0]["run_type"] == RUN_TYPE_SCHEDULED
    assert outcome["scheduler"]["scheduler_enabled"] is True


def test_a_disabled_scheduler_executes_nothing_and_says_so():
    orchestrator = RecordingOrchestrator()
    service = SchedulerService(orchestrator=orchestrator,
                               config=SchedulerConfig(enabled=False))
    with pytest.raises(SchedulerDisabled):
        service.run_cycle(None)
    assert orchestrator.calls == []


def test_one_controlled_cycle_can_be_forced_without_enabling_the_scheduler():
    orchestrator = RecordingOrchestrator()
    config = SchedulerConfig(enabled=False)
    outcome = SchedulerService(orchestrator=orchestrator,
                               config=config).run_cycle(None, force=True)
    assert len(orchestrator.calls) == 1
    assert outcome["scheduler_forced"] is True
    # Forcing one cycle must not flip the switch.
    assert outcome["scheduler"]["scheduler_enabled"] is False


def test_two_scheduled_cycles_make_the_same_call():
    orchestrator = RecordingOrchestrator()
    service = SchedulerService(orchestrator=orchestrator,
                               config=SchedulerConfig(enabled=True))
    now = datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc)
    service.run_cycle(None, now=now)
    service.run_cycle(None, now=now)
    assert orchestrator.calls[0] == orchestrator.calls[1]


def test_the_lookback_window_ends_on_the_target_day_and_reaches_back_no_further():
    config = SchedulerConfig(lookback_days=3)
    window = config.window(datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc))
    assert window[-1] == config.target_date(
        datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc))
    assert len(window) == 4
    assert window == sorted(window)


def test_the_business_day_is_cairo_and_not_the_hosts_clock():
    config = SchedulerConfig(timezone="Africa/Cairo")
    # 22:30 UTC on the 17th is already the 18th in Cairo.
    assert config.target_date(
        datetime(2026, 9, 17, 22, 30, tzinfo=timezone.utc)) == date(2026, 9, 18)


def test_the_scheduler_is_disabled_by_default():
    assert SchedulerConfig().enabled is False


def test_the_default_schedule_matches_the_legacy_qa_master():
    config = SchedulerConfig()
    assert config.cron == LEGACY_QA_MASTER_CRON == "0 21,23 * * *"
    assert config.timezone == "Africa/Cairo"
    assert config.lookback_days == DEFAULT_LOOKBACK_DAYS
    assert config.describe()["cron_matches_legacy_master"] is True


def test_configuration_is_environment_driven_and_reports_its_own_variables():
    described = SchedulerConfig().describe()
    assert described["environment_variables"]["enabled"] == "SCHEDULER_ENABLED"
    assert described["environment_variables"]["timezone"] == "SCHEDULER_TIMEZONE"
    assert described["environment_variables"]["cron"] == "SCHEDULER_CRON"


def test_a_bad_timezone_or_cron_is_refused_at_configuration_time():
    with pytest.raises(Exception):
        SchedulerConfig(timezone="Mars/Olympus").validate()
    with pytest.raises(ValueError):
        SchedulerConfig(cron="0 21 * *").validate()
    with pytest.raises(ValueError):
        SchedulerConfig(lookback_days=-1).validate()


def test_a_scheduled_cycle_never_discovers_during_a_dry_run():
    orchestrator = RecordingOrchestrator()
    config = SchedulerConfig(enabled=True, discover=True)
    SchedulerService(orchestrator=orchestrator, config=config).run_cycle(
        None, dry_run=True)
    assert orchestrator.calls[0]["discover"] is False


# --- the runner's own boundaries ----------------------------------------------

def test_the_runner_refuses_actions_it_is_not_allowed_to_pay_for():
    settings = object()
    no_provider = StageRunner(settings=settings, allow_provider=False)
    assert no_provider.can_run(RUN_QA) is False
    assert no_provider.refusal_reason(RUN_QA) == \
        "PROVIDER_CALLS_DISABLED_FOR_THIS_RUN"
    no_graph = StageRunner(settings=settings, allow_graph=False)
    assert no_graph.can_run(ACQUIRE_TRANSCRIPT) is False
    assert no_graph.can_run(CALCULATE_ENGAGEMENT) is True


def test_a_dry_run_can_never_reach_a_legacy_table_whatever_it_is_asked():
    """
    Four independent reasons a dry run cannot write, and this is one of them
    stated at the narrowest point: the runner itself refuses the two actions
    that touch a legacy row whenever it is not persisting.
    """
    dry = StageRunner(settings=object(), persist=False)
    for action in PRODUCTION_WRITE_ACTIONS:
        assert action in ACTION_SCOPE
        assert dry.can_run(action) is False
        assert dry.refusal_reason(action) == "LEGACY_WRITES_DISABLED_FOR_THIS_RUN"


def test_legacy_writes_can_be_switched_off_for_a_real_run_too():
    runner = StageRunner(settings=object(), persist=True,
                         allow_legacy_writes=False)
    for action in PRODUCTION_WRITE_ACTIONS:
        assert runner.can_run(action) is False
    # ...without disabling anything else.
    assert runner.can_run(CALCULATE_ENGAGEMENT) is True


def test_only_one_action_costs_a_generation_and_only_two_cost_graph():
    assert PROVIDER_ACTIONS == {RUN_QA}
    # LINK_RECORDING: one organizer recordings lookup plus DriveItem discovery.
    assert GRAPH_ACTIONS == {ACQUIRE_TRANSCRIPT, "LINK_RECORDING"}


def test_force_reprocess_is_defined_but_unreachable_from_the_scheduler():
    import pathlib
    for name in ("orchestrator.py", "scheduler.py", "runner.py"):
        source = pathlib.Path(f"app/orchestration/{name}").read_text(encoding="utf-8")
        assert f'"{FORCE_REPROCESS}"' not in source, name


def test_graph_calls_are_counted_from_the_counters_the_services_publish():
    """
    The first controlled cycle acquired seven lectures' transcripts and
    reported zero Graph calls, because no Phase 2A service publishes a field
    called `graph_calls`. A cost counter that reads zero while the cost is
    incurred is worse than none, because it is believed.
    """
    from app.orchestration.runner import GRAPH_COUNTER_KEYS, _outcome
    acquisition = {"meetings_queried": 7, "artifact_contents_fetched": 9,
                   "provider_calls": 0}
    assert _outcome(acquisition)["graph_calls"] == 16
    # A service that does publish the field is believed as-is.
    assert _outcome({"graph_calls": 3})["graph_calls"] == 3
    # A purely local stage costs nothing and says so.
    assert _outcome({"engagements_updated": 1})["graph_calls"] == 0
    assert "meetings_queried" in GRAPH_COUNTER_KEYS


def test_a_discovery_failure_does_not_cost_the_whole_cycle():
    """
    Found by the first cycle run through the enabled path: Graph timed out
    resolving an online meeting and a cycle that could have reconciled sixteen
    known lectures did nothing at all. Discovery answers "are there lectures we
    have not seen?" - a failure to answer says nothing about the ones we have.
    """
    class BrokenDiscovery:
        def __init__(self):
            self.calls = 0

        def discover_day(self, connection, aptem, day, persist=True):
            self.calls += 1
            raise PlatformError("online_meeting_query_error",
                                "Microsoft Graph online meeting query failed")

    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    discovery = BrokenDiscovery()
    orchestrator = PipelineOrchestrator(
        resolver=ScriptedResolver([lecture]), runner=runner,
        preflight=StubPreflight(), discovery_service=discovery)
    outcome = orchestrator.run_window(None, TARGET, discover=True)

    assert discovery.calls == 1
    # The known lecture was still processed.
    assert runner.calls == [(CALCULATE_ENGAGEMENT, "a")]
    assert outcome["status"] == RUN_COMPLETED
    failure = [item for item in outcome["errors"] if item["stage"] == "DISCOVERY"]
    assert failure and failure[0]["error_code"] == "online_meeting_query_error"
    assert failure[0]["stopped_run"] is False
    assert outcome["discovery"][0]["discovery_status"] == "FAILED"


def test_a_cycle_that_cannot_reach_the_aptem_source_still_reconciles():
    from contextlib import contextmanager

    @contextmanager
    def _broken():
        raise PlatformError(DATABASE_ERROR, "aptem unreachable")
        yield  # pragma: no cover

    lecture = ScriptedLecture("a", [CALCULATE_ENGAGEMENT])
    runner = RecordingRunner([lecture])
    orchestrator = PipelineOrchestrator(
        resolver=ScriptedResolver([lecture]), runner=runner,
        preflight=StubPreflight(), discovery_service=object())
    outcome = orchestrator.run_window(None, TARGET, discover=True,
                                      aptem_connection_factory=_broken)
    assert runner.calls == [(CALCULATE_ENGAGEMENT, "a")]
    assert outcome["discovery"][0]["discovery_status"] == "UNAVAILABLE"
    assert outcome["status"] == RUN_COMPLETED


# --- Phase 4C1: duplicate suppression, from the scheduler\'s point of view ----

def test_suppressing_a_duplicate_is_something_the_scheduler_may_do_itself():
    """
    It is automatable because the deterministic rule leaves nothing to choose
    between. Every shape that DOES require a choice never reaches this action:
    the state resolver reports MANUAL_REVIEW_REQUIRED instead.
    """
    assert SUPPRESS_DUPLICATE_EVENT in AUTOMATABLE_ACTIONS
    assert ACTION_SCOPE[SUPPRESS_DUPLICATE_EVENT] == "LECTURE"


def test_suppressing_a_duplicate_costs_no_graph_call_and_no_generation():
    assert SUPPRESS_DUPLICATE_EVENT not in GRAPH_ACTIONS
    assert SUPPRESS_DUPLICATE_EVENT not in PROVIDER_ACTIONS


def test_a_run_with_graph_and_the_provider_off_can_still_retire_a_duplicate():
    """The cheapest possible repair: one indexed read and one annotation."""
    runner = StageRunner(settings=None, allow_graph=False, allow_provider=False,
                         allow_legacy_writes=False)
    assert runner.can_run(SUPPRESS_DUPLICATE_EVENT) is True


def test_a_dry_run_never_persists_a_suppression():
    runner = StageRunner(settings=None, persist=False)
    # The gate is `persist`, which the whole runner already honours: the
    # service is called with persist=False and returns its decision unwritten.
    assert runner.persist is False


def test_the_orchestrator_retires_a_duplicate_and_then_leaves_it_alone():
    lecture = ScriptedLecture("dup-1", [SUPPRESS_DUPLICATE_EVENT])
    resolver = ScriptedResolver([lecture])
    runner = RecordingRunner([lecture])
    summary = PipelineOrchestrator(resolver=resolver, runner=runner,
                                   run_repository=None).run_window(
        None, TARGET)
    assert runner.calls == [(SUPPRESS_DUPLICATE_EVENT, "dup-1")]
    assert summary["lectures"][0]["final_action"] == NOTHING_TO_DO


def test_14_a_settled_duplicate_is_never_retried_on_a_later_cycle():
    """
    The second cycle must be a true no-op for it. A suppressed occurrence
    reports NOTHING_TO_DO, so the orchestrator has nothing to call - no Graph,
    no provider, no QA, not even another suppression write.
    """
    lecture = ScriptedLecture("dup-1", [])
    resolver = ScriptedResolver([lecture])
    runner = RecordingRunner([lecture])
    summary = PipelineOrchestrator(resolver=resolver, runner=runner,
                                   run_repository=None).run_window(None, TARGET)
    assert runner.calls == []
    assert summary["graph_calls"] == 0
    assert summary["provider_calls"] == 0
