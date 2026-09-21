"""
Phase 4B unit tests: what actually makes the scheduler run, and when.

The load-bearing claim these protect is an uncomfortable one:
`SCHEDULER_ENABLED=true` schedules nothing. It removes a refusal. Something
outside this code - Task Scheduler or a container - has to call a cycle, and
tests that quietly assumed otherwise would let that misunderstanding ship.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.orchestration.daemon import (
    CronExpressionError,
    SchedulerDaemon,
    next_fire_time,
    parse_cron,
)
from app.orchestration.locks import SchedulerCycleBusy
from app.orchestration.scheduler import (
    DEFAULT_LOOKBACK_DAYS,
    LEGACY_QA_MASTER_CRON,
    SchedulerConfig,
    SchedulerDisabled,
    SchedulerService,
)


CAIRO = ZoneInfo("Africa/Cairo")


# --- 32, 33, 34. the schedule is unchanged -------------------------------------

def test_the_timezone_is_still_africa_cairo():
    assert SchedulerConfig().timezone == "Africa/Cairo"


def test_the_cron_is_still_the_legacy_master_schedule():
    assert SchedulerConfig().cron == LEGACY_QA_MASTER_CRON == "0 21,23 * * *"


def test_the_lookback_is_still_three_days():
    assert SchedulerConfig().lookback_days == DEFAULT_LOOKBACK_DAYS == 3


def test_phase_4b_did_not_change_the_schedule():
    """
    Stated as its own test because the brief said not to change these values
    silently, and a default drifting is exactly the kind of change nobody
    notices in a diff.
    """
    described = SchedulerConfig().describe()
    assert described["cron"] == "0 21,23 * * *"
    assert described["timezone"] == "Africa/Cairo"
    assert described["lookback_days"] == 3
    assert described["cron_matches_legacy_master"] is True


# --- the cron subset -------------------------------------------------------------

def test_the_documented_schedule_parses_to_exactly_two_firings_a_day():
    fields = parse_cron("0 21,23 * * *")
    assert fields == {"minutes": {0}, "hours": {21, 23}}


def test_the_next_firing_is_computed_in_the_business_timezone():
    after = datetime(2026, 9, 19, 14, 30, tzinfo=CAIRO)
    assert next_fire_time("0 21,23 * * *", after=after) == \
        datetime(2026, 9, 19, 21, 0, tzinfo=CAIRO)


def test_after_the_last_firing_the_next_is_tomorrow():
    after = datetime(2026, 9, 19, 23, 30, tzinfo=CAIRO)
    assert next_fire_time("0 21,23 * * *", after=after) == \
        datetime(2026, 9, 20, 21, 0, tzinfo=CAIRO)


def test_a_firing_exactly_on_the_hour_does_not_fire_twice():
    """Strictly after: a cycle that just ran must not immediately run again."""
    after = datetime(2026, 9, 19, 21, 0, tzinfo=CAIRO)
    assert next_fire_time("0 21,23 * * *", after=after) == \
        datetime(2026, 9, 19, 23, 0, tzinfo=CAIRO)


@pytest.mark.parametrize("expression", [
    "0 21 * * 1",          # day-of-week
    "0 21 1 * *",          # day-of-month
    "0 21 * 3 *",          # month
    "*/15 * * * *",        # step syntax
    "0 21-23 * * *",       # range syntax
    "0 21,23 * *",         # too few fields
    "0 99 * * *",          # out of range
])
def test_an_unsupported_expression_is_refused_rather_than_approximated(expression):
    """
    A silently misparsed schedule is how an unattended system runs at the wrong
    time for a month. The parser covers what the platform documents and says so
    loudly about everything else.
    """
    with pytest.raises(CronExpressionError):
        next_fire_time(expression, after=datetime(2026, 9, 19, 12, 0, tzinfo=CAIRO))


# --- 30, 31. the interlock ---------------------------------------------------------

class RecordingOrchestrator:
    def __init__(self):
        self.calls = []

    def run_window(self, connection, target_date, **kwargs):
        self.calls.append({"target_date": target_date, **kwargs})
        return {"status": "COMPLETED", "run_id": "run-1", "lectures": [],
                "counts": {}, "graph_calls": 0, "provider_calls": 0}


class FakeConnection:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _daemon(config, orchestrator=None, **kwargs):
    orchestrator = orchestrator or RecordingOrchestrator()
    scheduler = SchedulerService(orchestrator=orchestrator, config=config)
    connection = FakeConnection()
    daemon = SchedulerDaemon(scheduler=scheduler,
                             connection_factory=lambda: connection,
                             sleep=lambda seconds: None, **kwargs)
    return daemon, orchestrator, connection


def test_a_disabled_scheduler_runs_nothing_even_with_a_daemon_running():
    daemon, orchestrator, _ = _daemon(SchedulerConfig(enabled=False))
    outcome = daemon.run_once()
    assert outcome["outcome"] == "REFUSED_SCHEDULER_DISABLED"
    assert orchestrator.calls == []


def test_enabling_it_runs_exactly_the_existing_orchestration_service():
    daemon, orchestrator, connection = _daemon(SchedulerConfig(enabled=True))
    outcome = daemon.run_once()
    assert outcome["outcome"] == "COMPLETED"
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["run_type"] == "SCHEDULED"
    assert connection.commits == 1


def test_the_daemon_contains_no_orchestration_logic_of_its_own():
    """
    Structural. There is one scheduling implementation, and the daemon is a
    loop and a sleep around it.
    """
    import inspect

    import app.orchestration.daemon as daemon
    source = inspect.getsource(daemon)
    for forbidden in ("run_window", "PipelineStateResolver", "StageRunner",
                      "LegacyQaWriter", "for_day("):
        assert forbidden not in source, forbidden
    assert "run_cycle" in source


# --- overlap and failure ------------------------------------------------------------

def test_a_cycle_that_finds_another_running_stands_down():
    class Busy(RecordingOrchestrator):
        def run_window(self, *args, **kwargs):
            raise SchedulerCycleBusy("another cycle is running")

    daemon, _, _ = _daemon(SchedulerConfig(enabled=True), orchestrator=Busy())
    assert daemon.run_once()["outcome"] == "SKIPPED_ANOTHER_CYCLE_RUNNING"


def test_one_failed_cycle_does_not_end_the_daemon():
    """
    The next scheduled cycle is a better recovery than a dead process. The
    orchestrator is idempotent, so a failed cycle costs its window and nothing
    more.
    """
    class Broken(RecordingOrchestrator):
        def run_window(self, *args, **kwargs):
            raise RuntimeError("database went away")

    daemon, _, _ = _daemon(SchedulerConfig(enabled=True), orchestrator=Broken())
    assert daemon.run_once()["outcome"] == "FAILED"
    summary = daemon.run_forever(max_cycles=2)
    assert summary["cycles_refused"] == 2


def test_the_loop_runs_one_cycle_per_firing_and_can_be_bounded():
    daemon, orchestrator, _ = _daemon(SchedulerConfig(enabled=True))
    summary = daemon.run_forever(max_cycles=3)
    assert summary["cycles_completed"] == 3
    assert len(orchestrator.calls) == 3


def test_a_stop_request_finishes_the_loop_without_starting_another_cycle():
    daemon, orchestrator, _ = _daemon(SchedulerConfig(enabled=True))
    daemon.request_stop()
    summary = daemon.run_forever(max_cycles=5)
    assert summary["stopped"] is True
    assert orchestrator.calls == []


def test_the_daemon_is_disabled_by_default_like_everything_else():
    daemon, orchestrator, _ = _daemon(SchedulerConfig())
    daemon.run_forever(max_cycles=1)
    assert orchestrator.calls == []


# --- 35. the deployment artefacts exist and restart -----------------------------------

def test_the_container_option_restarts_itself():
    import pathlib
    compose = pathlib.Path("automation/scheduler/docker-compose.yml").read_text(
        encoding="utf-8")
    assert "restart: unless-stopped" in compose
    # Matching the media worker's conventions rather than inventing new ones.
    assert "json-file" in compose
    assert "max-size" in compose


def test_the_windows_task_option_exists_and_retries():
    import pathlib
    script = pathlib.Path(
        "automation/scheduler/install-windows-task.ps1").read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in script
    # A second firing while one is running must not start a second cycle.
    assert "MultipleInstances IgnoreNew" in script
    assert "-RestartCount" in script
    assert "StartWhenAvailable" in script
    batch = pathlib.Path(
        "automation/scheduler/run-scheduler-cycle.bat").read_text(encoding="utf-8")
    assert "scheduler-cycle" in batch


def test_the_deployment_says_plainly_that_the_variable_schedules_nothing():
    import pathlib
    source = pathlib.Path("app/orchestration/daemon.py").read_text(encoding="utf-8")
    assert "does NOT schedule anything" in source
