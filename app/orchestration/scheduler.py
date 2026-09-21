"""
Phase 4A Part K/Q: the scheduler wrapper.

It calls `PipelineOrchestrator.run_window`. That is the whole of it. There is
no second code path for the automated case, because the value of validating the
manual path is exactly zero if the scheduled path is different code.

CONFIGURATION IS ENVIRONMENT-DRIVEN AND EXPLICIT
------------------------------------------------
No cron time is buried in a service, no timezone is assumed from the host, and
nothing is enabled by default. `SchedulerConfig.from_environment()` reads every
value from the environment and reports what it read, so "when does this run?"
is answered by printing the config rather than by reading the source.

The default schedule deliberately matches the legacy QA Master's own cron,
`0 21,23 * * *` in Africa/Cairo, because that is the operating rhythm the
college already has and the transcripts already follow. Copying it is a
compatibility decision, not a preference: inventing a new time would mean
inventing a new set of assumptions about when transcripts are ready.

SCHEDULER_ENABLED
-----------------
Defaults to FALSE. `run_cycle` refuses when disabled, and refuses loudly rather
than returning a quiet no-op, so a cycle that did not happen cannot be mistaken
for a cycle that found nothing to do.
"""
import os
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.orchestration.stages import RUN_TYPE_SCHEDULED


# The legacy QA Master's own schedule and timezone. Kept as the compatibility
# starting point rather than reinvented.
LEGACY_QA_MASTER_CRON = "0 21,23 * * *"
DEFAULT_TIMEZONE = "Africa/Cairo"
# Three days covers a late transcript, a late attendance row and a weekend
# without ever reaching back into history that is already settled.
DEFAULT_LOOKBACK_DAYS = 3

SCHEDULER_DISABLED = "SCHEDULER_DISABLED"

# The complete set of environment variables this scheduler reads. Declared once
# so `describe()` can answer "where do I change this?" without the reader
# having to find the parsing code.
ENVIRONMENT_VARIABLES = {
    "enabled": "SCHEDULER_ENABLED",
    "timezone": "SCHEDULER_TIMEZONE",
    "cron": "SCHEDULER_CRON",
    "lookback_days": "SCHEDULER_LOOKBACK_DAYS",
    "discover": "SCHEDULER_DISCOVER",
    "allow_graph": "SCHEDULER_ALLOW_GRAPH",
    "allow_provider": "SCHEDULER_ALLOW_PROVIDER",
    "allow_legacy_writes": "SCHEDULER_ALLOW_LEGACY_WRITES",
    "max_passes": "SCHEDULER_MAX_PASSES",
}


class SchedulerDisabled(RuntimeError):
    """SCHEDULER_ENABLED is false. Nothing ran, and that is the correct outcome."""


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "enabled")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    timezone: str = DEFAULT_TIMEZONE
    cron: str = LEGACY_QA_MASTER_CRON
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    # Whether a scheduled cycle may discover new lectures (a Graph call) and
    # whether it may buy a model generation. Both default ON because a
    # scheduler that cannot do either cannot process a new lecture - but both
    # are switches, so a cautious pilot can run with the expensive half off.
    discover: bool = True
    allow_graph: bool = True
    allow_provider: bool = True
    # Phase 4B. Whether a scheduled cycle may perform the safe legacy
    # QA/Perfect production sync. On by default, because a scheduler that
    # cannot finish a lecture is not finishing anything.
    allow_legacy_writes: bool = True
    max_passes: int = 8
    source: dict = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> "SchedulerConfig":
        names = ENVIRONMENT_VARIABLES
        config = cls(
            enabled=_flag(names["enabled"], False),
            timezone=os.environ.get(names["timezone"], "").strip() or DEFAULT_TIMEZONE,
            cron=os.environ.get(names["cron"], "").strip() or LEGACY_QA_MASTER_CRON,
            lookback_days=_int(names["lookback_days"], DEFAULT_LOOKBACK_DAYS),
            discover=_flag(names["discover"], True),
            allow_graph=_flag(names["allow_graph"], True),
            allow_provider=_flag(names["allow_provider"], True),
            allow_legacy_writes=_flag(names["allow_legacy_writes"], True),
            max_passes=_int(names["max_passes"], 8),
            source={key: os.environ.get(value, "") for key, value in names.items()})
        config.validate()
        return config

    def validate(self) -> None:
        # A bad timezone must fail at configuration time. Discovering it at
        # 21:00 unattended means the cycle silently runs on the wrong day.
        ZoneInfo(self.timezone)
        if self.lookback_days < 0:
            raise ValueError("SCHEDULER_LOOKBACK_DAYS must not be negative")
        parts = self.cron.split()
        if len(parts) != 5:
            raise ValueError("SCHEDULER_CRON must be a 5-field cron expression")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def target_date(self, now: datetime | None = None) -> _date:
        """
        The business day a cycle starting now should reconcile.

        Africa/Cairo, matching the rest of the platform: the college's day, not
        the server's.
        """
        moment = now.astimezone(self.zone) if now else datetime.now(self.zone)
        return moment.date()

    def window(self, now: datetime | None = None) -> list[_date]:
        target = self.target_date(now)
        return [target - timedelta(days=offset)
                for offset in range(self.lookback_days, -1, -1)]

    def describe(self) -> dict:
        return {"scheduler_enabled": self.enabled, "timezone": self.timezone,
                "cron": self.cron, "lookback_days": self.lookback_days,
                "discover": self.discover, "allow_graph": self.allow_graph,
                "allow_provider": self.allow_provider,
                "allow_legacy_writes": self.allow_legacy_writes,
                "max_passes": self.max_passes,
                "environment_variables": dict(ENVIRONMENT_VARIABLES),
                "environment_values_seen": self.source,
                "legacy_qa_master_cron": LEGACY_QA_MASTER_CRON,
                "cron_matches_legacy_master": self.cron == LEGACY_QA_MASTER_CRON}


class SchedulerService:
    """
    One scheduled cycle. Not a daemon, not a loop, not a thread.

    Phase 4A stops here deliberately: the cycle is the unit that has to be
    proven safe, and proving it is easier when running one is an explicit act.
    Whatever eventually fires this - cron, systemd, a supervisor - calls
    `run_cycle` exactly as the CLI does.
    """

    def __init__(self, *, orchestrator, config: SchedulerConfig | None = None):
        self.orchestrator = orchestrator
        self.config = config or SchedulerConfig()

    def run_cycle(self, connection, *, now: datetime | None = None,
                  aptem_connection=None, aptem_connection_factory=None,
                  dry_run: bool = False, force: bool = False) -> dict:
        """
        `force` runs a cycle through the scheduled entrypoint while
        SCHEDULER_ENABLED is still false. That is exactly the controlled pilot
        Part P asks for: the same code path, run once, on purpose, by a human.
        It does not enable anything.
        """
        if not self.config.enabled and not force:
            raise SchedulerDisabled(
                "SCHEDULER_ENABLED is false; no automatic execution occurred")
        target = self.config.target_date(now)
        outcome = self.orchestrator.run_window(
            connection, target, lookback_days=self.config.lookback_days,
            run_type=RUN_TYPE_SCHEDULED, dry_run=dry_run,
            discover=self.config.discover and not dry_run,
            aptem_connection=aptem_connection,
            aptem_connection_factory=aptem_connection_factory)
        return {**outcome, "scheduler": self.config.describe(),
                "scheduler_forced": force and not self.config.enabled,
                "cycle_target_date": target.isoformat()}
