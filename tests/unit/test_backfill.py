"""
QA Core RC2: Operations Backfill.

The feature's whole claim is that it reuses production components rather than
reimplementing them, so most of these tests assert on WHAT IT CALLS and with
which arguments. A backfill that quietly grew its own discovery or its own QA
path would still produce plausible numbers; the way to catch that is to check
that the production entry points were the ones invoked.

Fakes stand in for the orchestrator, the resolver, the discovery service and
the database. They are not a pipeline - they record what the runner asked for.
"""
from datetime import date

import pytest

from app.common.errors import PlatformError
from app.db.repositories.backfill import (
    CANCELLED,
    COMPLETED,
    DAY_COMPLETED,
    DAY_DEFERRED_CYCLE_BUSY,
    DAY_FAILED,
    DAY_SKIPPED_CANCELLED,
    FAILED,
    MODE_EXECUTE,
    MODE_PREVIEW,
)
from app.orchestration.backfill import (
    BACKFILL_RUNNER_VERSION,
    INVALID_RANGE,
    MAX_RANGE_DAYS,
    RANGE_TOO_LARGE,
    BackfillPreviewService,
    BackfillRunner,
    business_days,
    validate_range,
)
from app.orchestration.locks import SchedulerCycleBusy
from app.orchestration.stages import RUN_TYPE_BACKFILL

SEPTEMBER_FROM = date(2026, 9, 1)
SEPTEMBER_TO = date(2026, 9, 21)


# --- the range ---------------------------------------------------------------

def test_a_single_day_range_is_one_day():
    assert business_days(date(2026, 9, 5), date(2026, 9, 5)) == [date(2026, 9, 5)]


def test_the_september_range_is_inclusive_at_both_ends():
    days = business_days(SEPTEMBER_FROM, SEPTEMBER_TO)
    assert len(days) == 21
    assert days[0] == SEPTEMBER_FROM and days[-1] == SEPTEMBER_TO


def test_a_reversed_range_is_refused():
    with pytest.raises(PlatformError) as caught:
        validate_range(SEPTEMBER_TO, SEPTEMBER_FROM)
    assert caught.value.code == INVALID_RANGE


def test_an_implausibly_large_range_is_refused():
    with pytest.raises(PlatformError) as caught:
        validate_range(date(2026, 1, 1), date(2026, 12, 31))
    assert caught.value.code == RANGE_TOO_LARGE


def test_the_september_range_is_comfortably_inside_the_limit():
    """The mandatory first use case must not trip the guard."""
    validate_range(SEPTEMBER_FROM, SEPTEMBER_TO)
    assert MAX_RANGE_DAYS >= 31


# --- fakes -------------------------------------------------------------------

class FakeConnection:
    def __init__(self):
        self.committed = 0

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def commit(self):
        self.committed += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRepository:
    """Records transitions. Holds one run, like the runner ever sees."""

    def __init__(self, run):
        self.run = dict(run)
        self.days = []
        self.advances = []
        self.finished = None
        self.heartbeats = []
        self._cancel_after = None
        self._cancel_calls = 0

    def cancel_after(self, calls):
        self._cancel_after = calls

    def get(self, _connection, _run_id):
        return self.run

    def cancel_requested(self, _connection, _run_id):
        self._cancel_calls += 1
        if self._cancel_after is None:
            return False
        return self._cancel_calls > self._cancel_after

    def heartbeat(self, _connection, run_id, **kwargs):
        self.heartbeats.append(kwargs)

    def record_day(self, _connection, _run_id, **kwargs):
        self.days.append(kwargs)

    def advance(self, _connection, _run_id, **kwargs):
        self.advances.append(kwargs)

    def finish(self, _connection, _run_id, **kwargs):
        self.finished = kwargs

    def claim(self, _connection, **_kwargs):
        return None


class FakeOrchestrator:
    """Records every run_window call and returns a normal-shaped summary."""

    def __init__(self, *, summary=None, raises=None):
        self.calls = []
        self._summary = summary
        self._raises = raises

    def run_window(self, _connection, target_date, **kwargs):
        self.calls.append({"target_date": target_date, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._summary or {
            "status": "COMPLETED", "run_id": "run-1",
            "graph_calls": 3, "provider_calls": 1,
            "discovery": [{"registry_rows_created": 1}],
            "lectures": [{"changed": True}, {"changed": False}],
            "counts": {"completed_count": 1, "waiting_count": 0,
                       "review_count": 0, "failed_count": 0,
                       "skipped_count": 0},
        }


def runner(repository, orchestrator, **kwargs):
    return BackfillRunner(
        orchestrator=orchestrator,
        connection_factory=FakeConnection,
        readonly_connection_factory=FakeConnection,
        repository=repository, **kwargs)


def a_run(**overrides):
    return {"backfill_run_id": "bf-1", "requested_from": date(2026, 9, 1),
            "requested_to": date(2026, 9, 3), "mode": MODE_EXECUTE,
            "current_business_date": None, **overrides}


# --- the pipeline it reuses ---------------------------------------------------

def test_every_day_goes_through_the_production_orchestrator():
    """
    Not "calls something that processes lectures" - calls `run_window`, the
    exact entry point the nightly scheduler uses, once per business date in
    chronological order.
    """
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert [call["target_date"] for call in orchestrator.calls] == [
        date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def test_each_day_is_its_own_window_with_discovery_on():
    """
    `lookback_days=0` matters: a backfill day must reconcile THAT day, not
    drag three more days of the scheduler's lookback into every step.
    """
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    runner(repository, orchestrator).run_claimed("bf-1")

    for call in orchestrator.calls:
        assert call["lookback_days"] == 0
        assert call["discover"] is True
        assert call["dry_run"] is False
        assert call["run_type"] == RUN_TYPE_BACKFILL


def test_the_runner_never_asks_for_a_force_reprocess():
    """
    There is no such parameter, and there must never be one. A backfill that
    could force a COMPLETE stage would re-buy every model generation in
    September the first time somebody widened a date by mistake.
    """
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    runner(repository, orchestrator).run_claimed("bf-1")

    for call in orchestrator.calls:
        assert "force" not in call and "force_reprocess" not in call


# --- resume -------------------------------------------------------------------

def test_a_resumed_run_starts_at_its_checkpoint():
    repository = FakeRepository(a_run(current_business_date=date(2026, 9, 3)))
    orchestrator = FakeOrchestrator()
    runner(repository, orchestrator).run_claimed("bf-1")

    assert [call["target_date"] for call in orchestrator.calls] == [date(2026, 9, 3)]


def test_the_checkpoint_moves_only_after_the_day_is_recorded():
    """
    The ordering that makes a crash safe: the day summary is written, then the
    checkpoint advances. A crash between them re-runs the day, which existing
    stage resolution turns into NOTHING_TO_DO.
    """
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    runner(repository, orchestrator).run_claimed("bf-1")

    assert len(repository.days) == 3
    assert len(repository.advances) == 3
    assert repository.advances[0]["next_business_date"] == date(2026, 9, 2)
    # The final advance has nowhere left to go.
    assert repository.advances[-1]["next_business_date"] is None


def test_a_stop_request_leaves_the_run_resumable_rather_than_finished():
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    service = runner(repository, orchestrator)
    service.request_stop()
    outcome = service.run_claimed("bf-1")

    assert outcome["outcome"] == "STOPPED"
    assert outcome["resume_from"] == "2026-09-01"
    assert repository.finished is None, "a stopped runner must not finish the run"
    assert orchestrator.calls == []


# --- cancel --------------------------------------------------------------------

def test_cancel_stops_at_a_day_boundary_and_records_cancelled():
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    repository.cancel_after(1)                # allow day one, then cancel
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == "CANCELLED"
    assert [call["target_date"] for call in orchestrator.calls] == [date(2026, 9, 1)]
    assert repository.finished["status"] == CANCELLED
    assert repository.days[-1]["status"] == DAY_SKIPPED_CANCELLED


def test_cancel_never_interrupts_the_day_in_flight():
    """
    The cancel check happens BEFORE a day starts, never during it. A day ends
    in a database write, and tearing that down midway is how half-written
    state happens.
    """
    repository, orchestrator = FakeRepository(a_run()), FakeOrchestrator()
    repository.cancel_after(2)
    runner(repository, orchestrator).run_claimed("bf-1")

    completed = [day for day in repository.days if day["status"] == DAY_COMPLETED]
    assert len(completed) == 2, "both started days must have finished"


# --- coexistence with the nightly scheduler -------------------------------------

def test_a_busy_cycle_lock_defers_the_day_rather_than_failing_it():
    """
    The scheduler holds the cycle lock. Deferring is correct: it reconciles the
    same registry with the same rules, so there is nothing to fight over.
    """
    repository = FakeRepository(a_run())
    orchestrator = FakeOrchestrator(raises=SchedulerCycleBusy("busy"))
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert {day["status"] for day in repository.days} == {DAY_DEFERRED_CYCLE_BUSY}
    assert repository.finished["status"] == COMPLETED


# --- failure handling -----------------------------------------------------------

def test_one_bad_day_does_not_abandon_the_month():
    repository = FakeRepository(a_run())
    orchestrator = FakeOrchestrator(raises=PlatformError("TRANSCRIPT_UNREADABLE", "x"))
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert len([day for day in repository.days if day["status"] == DAY_FAILED]) == 3


def test_a_global_blocker_stops_the_whole_run():
    repository = FakeRepository(a_run())
    orchestrator = FakeOrchestrator(raises=PlatformError("DATABASE_ERROR", "gone"))
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == FAILED
    assert repository.finished["status"] == FAILED
    # It stopped on the FIRST day rather than failing all three.
    assert len(orchestrator.calls) == 1


def test_an_active_legacy_qa_path_blocks_the_run_and_says_so():
    """
    The preflight inside run_window fails closed. Every remaining day would
    refuse identically, so continuing would be 20 more pointless Graph sweeps.
    """
    repository = FakeRepository(a_run())
    orchestrator = FakeOrchestrator(summary={
        "status": "BLOCKED_LEGACY_QA_ACTIVE", "run_id": "run-1",
        "lectures": [], "counts": {}})
    outcome = runner(repository, orchestrator).run_claimed("bf-1")

    assert outcome["outcome"] == "BLOCKED_LEGACY_QA_ACTIVE"
    assert repository.finished["status"] == "BLOCKED_LEGACY_QA_ACTIVE"
    assert "legacy" in repository.finished["error_summary"].lower()
    assert len(orchestrator.calls) == 1


# --- counts ----------------------------------------------------------------------

def test_counts_come_from_the_orchestrator_rather_than_being_recomputed():
    repository = FakeRepository(a_run(requested_to=date(2026, 9, 1)))
    orchestrator = FakeOrchestrator(summary={
        "status": "COMPLETED", "run_id": "run-1",
        "discovery": [{"registry_rows_created": 2}],
        "lectures": [{"changed": True}, {"changed": True}, {"changed": False}],
        "counts": {"completed_count": 1, "waiting_count": 4, "review_count": 2,
                   "failed_count": 3, "skipped_count": 5}})
    runner(repository, orchestrator).run_claimed("bf-1")

    advance = repository.advances[0]["counts"]
    assert advance["waiting"] == 4
    assert advance["review_required"] == 2
    assert advance["failed"] == 3
    assert advance["suppressed"] == 5
    assert advance["already_complete"] == 1
    assert advance["discovered"] == 2
    assert advance["processed"] == 2


# --- preview ----------------------------------------------------------------------

class FakeResolver:
    def __init__(self, states):
        self.states = states
        self.days_asked = []

    def for_day(self, _connection, day):
        self.days_asked.append(day)
        return self.states


class FakeDiscovery:
    def __init__(self, outcome=None, raises=None):
        self.calls = []
        self._outcome = outcome
        self._raises = raises

    def discover_day(self, _kbc, _aptem, target_date, *, persist=True):
        self.calls.append({"date": target_date, "persist": persist})
        if self._raises is not None:
            raise self._raises
        return self._outcome or {"calendar_events_found": 19,
                                 "canonical_lecture_candidates": 7,
                                 "active_aptem_groups_loaded": 39}


class FakePreflight:
    def __init__(self, status="LEGACY_QA_DISABLED"):
        self.status = status

    def check(self):
        return {"status": self.status, "writes_permitted": self.status == "LEGACY_QA_DISABLED"}


def state(action=None, blocking=None, blocking_state=None, suppressed=False):
    return {"lecture_id": "l-1", "subject": "Subject", "session_date": "2026-09-16",
            "next_executable_action": action, "blocking_stage": blocking,
            "duplicate_suppression": suppressed,
            "stages": {blocking: {"state": blocking_state}} if blocking else {}}


def preview_service(states, discovery=None, preflight=None):
    return BackfillPreviewService(
        resolver=FakeResolver(states),
        discovery_service=discovery,
        preflight=preflight or FakePreflight())


def test_preview_never_persists_anything_it_discovers():
    """The single most important property of the preview path."""
    discovery = FakeDiscovery()
    service = preview_service([], discovery=discovery)
    service.preview(FakeConnection(), date(2026, 9, 16), date(2026, 9, 17))

    assert len(discovery.calls) == 2
    assert all(call["persist"] is False for call in discovery.calls)


def test_preview_uses_the_production_discovery_for_every_day():
    discovery = FakeDiscovery()
    service = preview_service([], discovery=discovery)
    service.preview(FakeConnection(), date(2026, 9, 16), date(2026, 9, 18))

    assert [call["date"] for call in discovery.calls] == [
        date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]


def test_preview_reads_active_groups_through_discovery_not_a_local_list():
    discovery = FakeDiscovery()
    result = preview_service([], discovery=discovery).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16))

    assert result.active_groups == 39


def test_preview_buckets_lectures_by_the_resolvers_own_verdict():
    states = [
        state(),                                                  # complete
        state(action="RUN_QA"),                                   # needs work
        state(blocking="ATTENDANCE", blocking_state="WAITING"),
        state(blocking="DISCOVERY", blocking_state="REVIEW_REQUIRED"),
        state(suppressed=True),
    ]
    result = preview_service(states).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16), discover=False)
    day = result.days[0]

    assert day.known_lectures == 5
    assert day.already_complete == 1
    assert day.needs_processing == 1
    assert day.waiting == 1
    assert day.review_required == 1
    assert day.suppressed == 1


def test_a_suppressed_duplicate_is_never_counted_as_needing_processing():
    result = preview_service([state(action="RUN_QA", suppressed=True)]).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16), discover=False)

    assert result.days[0].suppressed == 1
    assert result.days[0].needs_processing == 0


def test_newly_discoverable_never_goes_negative():
    """
    The registry can legitimately hold more lectures for a day than today's
    calendar returns - a cancelled or rescheduled event still has real
    history - and that is not a negative discovery.
    """
    discovery = FakeDiscovery(outcome={"calendar_events_found": 3,
                                       "canonical_lecture_candidates": 1,
                                       "active_aptem_groups_loaded": 39})
    result = preview_service([state(), state(), state()],
                             discovery=discovery).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16))

    assert result.days[0].newly_discoverable == 0


def test_an_unavailable_aptem_source_degrades_honestly():
    discovery = FakeDiscovery(raises=PlatformError("NO_ACTIVE_APTEM_GROUPS", "none"))
    result = preview_service([], discovery=discovery).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16))

    assert result.days[0].discovery_status == "FAILED"
    assert result.days[0].error_code == "NO_ACTIVE_APTEM_GROUPS"


def test_preview_reports_an_active_legacy_path_without_refusing():
    """A preview writes nothing, so an active legacy path is news, not a block."""
    result = preview_service([], preflight=FakePreflight("LEGACY_QA_ACTIVE")).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16), discover=False)

    assert result.as_dict()["writes_permitted"] is False
    assert any("legacy" in note.lower() for note in result.notes)


def test_the_serialised_preview_marks_itself_read_only():
    result = preview_service([]).preview(
        FakeConnection(), date(2026, 9, 16), date(2026, 9, 16), discover=False)
    payload = result.as_dict()

    assert payload["read_only"] is True
    assert payload["requested_days"] == 1
    assert set(payload["totals"]) >= {"matched_lectures", "waiting",
                                      "review_required", "suppressed"}


# --- preview runs never reach the orchestrator -------------------------------------

def test_a_preview_run_never_calls_the_orchestrator():
    """
    Mode is what separates looking from doing, and this is the assertion that
    keeps it honest: a PREVIEW run must not reach `run_window` at all.
    """
    repository = FakeRepository(a_run(mode=MODE_PREVIEW,
                                      requested_to=date(2026, 9, 2)))
    orchestrator = FakeOrchestrator()
    service = runner(repository, orchestrator,
                     preview_service=preview_service([], discovery=FakeDiscovery()))
    outcome = service.run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert orchestrator.calls == [], "a preview must never run a pipeline cycle"
    assert len(repository.days) == 2


def test_a_preview_run_records_what_an_execute_run_would_face():
    repository = FakeRepository(a_run(mode=MODE_PREVIEW,
                                      requested_to=date(2026, 9, 1)))
    service = runner(repository, FakeOrchestrator(),
                     preview_service=preview_service(
                         [state(action="RUN_QA")], discovery=FakeDiscovery()))
    service.run_claimed("bf-1")

    day = repository.days[0]["counts"]
    assert day["calendar_events_considered"] == 19
    assert day["matched_lectures"] == 7
    # Nothing was processed, and the preview does not pretend otherwise.
    assert day["processed"] == 0


def test_the_runner_version_is_recorded_for_traceability():
    assert BACKFILL_RUNNER_VERSION == "backfill_runner_v1"
