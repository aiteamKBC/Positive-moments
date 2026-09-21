"""
Phase 4B Part N/O: what actually makes the scheduler run.

THE HONEST ANSWER FIRST
-----------------------
`SCHEDULER_ENABLED=true` does NOT schedule anything. It is a safety interlock:
`SchedulerService.run_cycle` refuses to do anything while it is false. Setting
it to true removes that refusal and nothing else. No process appears, no timer
starts, and nothing fires at 21:00.

Something outside this code must call a cycle. There are exactly two supported
ways, both using the SAME entrypoint the manual CLI and the validated pilots
used - there is no second scheduling implementation anywhere in this platform:

  1. WINDOWS TASK SCHEDULER (the current local reality). A task fires
     `scheduler-cycle` at the configured hours. The OS owns the timing, the
     retry and the restart-after-reboot, and each firing is a bounded process
     that exits. This is what `automation/scheduler/` installs.

  2. A LONG-RUNNING LOOP (`run_forever` below), for a container. It computes
     the next fire time from the same cron expression and sleeps until then.
     A Compose service with `restart: unless-stopped` owns the restart, exactly
     as the media worker does.

Option 1 needs no process between firings. Option 2 needs the process alive.
Both are stated in the deployment notes rather than assumed.

WHY THE CRON PARSER IS DELIBERATELY TINY
----------------------------------------
It accepts the shape the platform actually documents - a minute list, an hour
list, and `*` for day/month/weekday - and REFUSES everything else loudly. A
full cron implementation would be more code than the thing it schedules, and a
silently misparsed expression is how an unattended system runs at the wrong
time for a month before anybody notices.

OVERLAP
-------
Neither option prevents two cycles overlapping on its own: Task Scheduler can
fire again while the last run is slow, and a restarted container can briefly
double up. The PostgreSQL cycle-level advisory lock is what actually prevents
it, and it works across processes, containers and machines because it lives in
the database rather than in any of them.
"""
import logging
import signal
import time
from datetime import datetime, timedelta

from app.orchestration.locks import SchedulerCycleBusy
from app.orchestration.scheduler import SchedulerDisabled


class CronExpressionError(ValueError):
    """The schedule could not be understood, so nothing is scheduled."""


def parse_cron(expression: str) -> dict:
    """
    Parse the supported subset: `<minutes> <hours> * * *`.

    Returns the minute and hour sets. Anything using day-of-month, month or
    day-of-week is refused rather than approximated.
    """
    parts = (expression or "").split()
    if len(parts) != 5:
        raise CronExpressionError(
            f"expected 5 cron fields, got {len(parts)}: {expression!r}")
    minutes, hours, day, month, weekday = parts
    if (day, month, weekday) != ("*", "*", "*"):
        raise CronExpressionError(
            "only daily schedules are supported; day-of-month, month and "
            f"day-of-week must all be '*': {expression!r}")
    return {"minutes": _field(minutes, 0, 59, expression),
            "hours": _field(hours, 0, 23, expression)}


def _field(value: str, low: int, high: int, expression: str) -> set:
    if value == "*":
        return set(range(low, high + 1))
    found = set()
    for part in value.split(","):
        if not part.isdigit():
            raise CronExpressionError(
                f"unsupported cron field {part!r} in {expression!r}; only "
                "'*' and comma-separated numbers are supported")
        number = int(part)
        if not low <= number <= high:
            raise CronExpressionError(
                f"cron value {number} out of range {low}-{high} in {expression!r}")
        found.add(number)
    if not found:
        raise CronExpressionError(f"empty cron field in {expression!r}")
    return found


def next_fire_time(expression: str, *, after: datetime) -> datetime:
    """
    The next instant matching the expression, strictly after `after`, in
    `after`'s own timezone.

    Minute resolution, and it searches forward a bounded number of minutes so a
    schedule that can never match raises instead of looping for ever.
    """
    schedule = parse_cron(expression)
    moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    # Two days of minutes: enough for any daily schedule, including one that
    # only matches once a day across a DST boundary.
    for _ in range(2 * 24 * 60):
        if moment.hour in schedule["hours"] and moment.minute in schedule["minutes"]:
            return moment
        moment += timedelta(minutes=1)
    raise CronExpressionError(f"no fire time within two days for {expression!r}")


class SchedulerDaemon:
    """
    The long-running option. A loop, a sleep and one call.

    It owns no orchestration logic whatsoever: every cycle goes through
    `SchedulerService.run_cycle`, the same method the CLI calls and the same
    one the controlled pilots validated.
    """

    def __init__(self, *, scheduler, connection_factory, aptem_connection_factory=None,
                 sleep=time.sleep, now=None):
        self.scheduler = scheduler
        self.connection_factory = connection_factory
        self.aptem_connection_factory = aptem_connection_factory
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(self.scheduler.config.zone))
        self._stopping = False
        self.log = logging.getLogger(__name__)

    def request_stop(self, *_args) -> None:
        """Finish the current cycle, then exit. Never kill one mid-write."""
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for name in ("SIGTERM", "SIGINT"):
            handler = getattr(signal, name, None)
            if handler is not None:
                signal.signal(handler, self.request_stop)

    def run_forever(self, *, max_cycles: int | None = None) -> dict:
        """
        Sleep until the next scheduled instant, run one cycle, repeat.

        `max_cycles` exists so this is testable and so an operator can run a
        bounded number of cycles deliberately. Left unset, it runs until the
        process is asked to stop.
        """
        completed, skipped, refused = 0, 0, 0
        while not self._stopping:
            if max_cycles is not None and completed + skipped + refused >= max_cycles:
                break
            target = next_fire_time(self.scheduler.config.cron, after=self._now())
            delay = max((target - self._now()).total_seconds(), 0)
            self.log.info("scheduler sleeping until next cycle", extra={"fields": {
                "service": "scheduler_daemon", "next_fire_at": target.isoformat(),
                "sleep_seconds": round(delay), "cron": self.scheduler.config.cron,
                "timezone": self.scheduler.config.timezone}})
            self._sleep(delay)
            if self._stopping:
                break
            outcome = self.run_once()
            if outcome["outcome"] == "COMPLETED":
                completed += 1
            elif outcome["outcome"] == "SKIPPED_ANOTHER_CYCLE_RUNNING":
                skipped += 1
            else:
                refused += 1
        return {"cycles_completed": completed, "cycles_skipped": skipped,
                "cycles_refused": refused, "stopped": self._stopping}

    def run_once(self) -> dict:
        """One cycle, with every expected outcome handled as data, not a crash."""
        try:
            with self.connection_factory() as connection:
                summary = self.scheduler.run_cycle(
                    connection,
                    aptem_connection_factory=self.aptem_connection_factory)
                connection.commit()
                return {"outcome": "COMPLETED", "status": summary.get("status"),
                        "run_id": summary.get("run_id")}
        except SchedulerCycleBusy:
            # Another cycle is mid-flight. Standing down is correct: the
            # running cycle will cover this window.
            self.log.info("scheduler cycle skipped; another is running")
            return {"outcome": "SKIPPED_ANOTHER_CYCLE_RUNNING"}
        except SchedulerDisabled:
            self.log.warning("SCHEDULER_ENABLED is false; nothing was run")
            return {"outcome": "REFUSED_SCHEDULER_DISABLED"}
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the daemon
            # The next scheduled cycle is a better recovery than a dead
            # process: the orchestrator is idempotent, so a failed cycle costs
            # the window and nothing else.
            self.log.exception("scheduler cycle failed", extra={"fields": {
                "service": "scheduler_daemon", "error": type(exc).__name__}})
            return {"outcome": "FAILED", "error": type(exc).__name__}
