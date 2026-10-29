"""
Continuous publisher configuration: read once at startup, validated, immutable.

Deliberately NOT app.config.settings.Settings: that module imports the QA
prompt, and the publisher's import boundary excludes QA. The fields the shared
builders read (graph_*, calendar_user_upn, lms_database_url, ...) have the same
names, so this object can be handed to them directly.

MODES
  SHADOW            discovery, LMS matching, transcript and recording acquisition,
                    and import PLANNING. No Bunny call that writes. The default.
  STAGING_WRITE     Bunny writes to the explicitly configured STAGING library only.
  PRODUCTION_WRITE  refused unless PUBLISHER_PRODUCTION_WRITE_ENABLED=true and a
                    production library is configured. Disabled by default; the
                    student-facing release gate is separate (tokens, CDN, LMS API).

PRODUCTION LIBRARY = THE STAGING LIBRARY, ADOPTED (decision 2026-10-24)
  Bunny library 786045 is Kent's permanent delivery library. PRODUCTION_WRITE
  therefore accepts BUNNY_PRODUCTION_LIBRARY_ID equal to the staging library:
  every video, caption and readiness row is keyed by bunny_library_id, never by
  mode, so switching the mode ADOPTS the existing rows - nothing is re-imported,
  no Bunny id changes, no readiness resets. A staging library that DIFFERS from
  the production library is still allowed (a separate test library).

DATABASE CONNECTION
  PUBLISHER_DATABASE_URL is the publisher's own Lecture Intelligence
  connection and must be Neon's DIRECT endpoint (host without "-pooler"). The
  daemon's task locks and the per-lecture locks are SESSION advisory locks held
  across several statements on one connection; through a transaction-mode
  pooler consecutive statements can reach different server backends, so a
  lock would leak on one backend while its unlock ran on another. A pooled
  host is therefore refused at startup. The generic DATABASE_URL (the pooled
  URL QA, Django and the media runner use) is only a fallback, and is subject
  to the same refusal.

Secrets are never part of repr() and never logged; `describe()` reports which
are present, not their values.
"""
from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path

SHADOW, STAGING_WRITE, PRODUCTION_WRITE = "SHADOW", "STAGING_WRITE", "PRODUCTION_WRITE"
# CDN (pull zone) token authentication policy for learner delivery. DEFERRED for the 2026-10 rollout
# (uninterrupted playback first): not a release blocker, reported as DEFERRED, the publisher never signs
# its caption read-back. REQUIRED (a future hardening step) makes it a release blocker and signs.
CDN_DEFERRED, CDN_REQUIRED = "DEFERRED", "REQUIRED"
CDN_POLICIES = (CDN_DEFERRED, CDN_REQUIRED)
APPROVED_REFERRERS_DEFAULT = "lms.kentbusinesscollege.org"
MODES = (SHADOW, STAGING_WRITE, PRODUCTION_WRITE)
# The old Bunny account's library: never a destination, in any mode.
FORBIDDEN_LIBRARY_IDS = frozenset({785080})

# libpq parameters for every publisher-opened PostgreSQL connection (Lecture Intelligence and Aptem):
# a bounded connect, and TCP keepalives plus a user timeout so a silently dropped route errors out
# (and the worker retries) instead of hanging on the OS default.
DB_CONNECT_KWARGS = {"connect_timeout": 15, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
                     "keepalives_count": 3, "tcp_user_timeout": 60000}

ROOT = Path(__file__).resolve().parents[2]


def database_endpoint(url: str) -> str:
    """'neon-pooled' / 'neon-direct' / 'other' / 'missing' - the host kind only, never the URL."""
    if not url:
        return "missing"
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return "other"
    if host.endswith(".neon.tech"):
        return "neon-pooled" if "-pooler." in host else "neon-direct"
    return "other"


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("publisher configuration refused: " + "; ".join(problems))


@dataclass(frozen=True)
class PublisherConfig:
    mode: str = SHADOW
    # -- databases and Graph (same names as Settings) -------------------------
    database_url: str = field(default="", repr=False)
    aptem_database_url: str = field(default="", repr=False)
    lms_database_url: str = field(default="", repr=False)
    graph_tenant_id: str = field(default="", repr=False)
    graph_client_id: str = field(default="", repr=False)
    graph_client_secret: str = field(default="", repr=False)
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    calendar_user_upn: str = field(default="", repr=False)
    recording_link_search_region: str = ""
    # -- scope ------------------------------------------------------------------
    # Lectures before this date are historical-backfill scope: never planned,
    # never marked failed, never "legacy" by inference.
    start_date: date | None = None
    # Optional upper bound (inclusive): a scoped replay sets start = end = the day.
    end_date: date | None = None
    lookback_days: int = 3
    test_programmes: str = ""
    discover_calendar: bool = True
    # -- Bunny ------------------------------------------------------------------
    staging_library_id: int | None = None
    staging_api_key: str = field(default="", repr=False)
    staging_account_api_key: str = field(default="", repr=False)
    staging_cdn_hostname: str = ""
    production_library_id: int | None = None
    production_api_key: str = field(default="", repr=False)
    production_account_api_key: str = field(default="", repr=False)
    production_write_enabled: bool = False
    production_cdn_hostname: str = ""
    # The pull zone's token key, used ONLY to sign the publisher's own caption
    # read-back, and only when cdn_token_auth_policy is REQUIRED. Never logged.
    cdn_token_key: str = field(default="", repr=False)
    # -- learner-delivery security policy (checked by `security-check`, enforced by `release`) ---------
    cdn_token_auth_policy: str = CDN_DEFERRED
    approved_referrers: tuple = (APPROVED_REFERRERS_DEFAULT,)
    require_block_none_referrer: bool = True
    caption_language: str = "en"
    caption_label: str = "English"
    # -- automatic release (off by default) ----------------------------------------
    # When on, the scheduler's AUTO_RELEASE task releases any lecture (no date restriction: live and historical
    # backfill alike) that is verified READY, LMS-matched and never published before, through the same
    # audited delivery.release().
    auto_release: bool = False
    # -- runtime ----------------------------------------------------------------
    workers: int = 2
    max_imports_in_flight: int = 2
    poll_seconds: float = 5.0
    discovery_interval: timedelta = timedelta(minutes=5)
    light_reconcile_interval: timedelta = timedelta(hours=6)
    full_reconcile_at: time = time(5, 0)            # Africa/Cairo
    heartbeat_interval: timedelta = timedelta(seconds=30)
    heartbeat_stale_after: timedelta = timedelta(minutes=5)
    job_lease_seconds: int = 15 * 60
    identify_timeout: timedelta = timedelta(minutes=30)
    uncertain_reconcile_window: timedelta = timedelta(hours=2)
    encoding_stall_after: timedelta = timedelta(hours=6)
    max_import_generations: int = 3
    duration_tolerance_seconds: float = 2.0
    shutdown_grace_seconds: float = 120.0
    # -- historical backfill (app/publisher/backfill.py; runs only when an operator confirms one) -----
    backfill_max_days: int = 31                     # inclusive days per run
    backfill_batch_size: int = 10                   # lectures a run starts per tick
    backfill_max_active: int = 20                   # lectures of a run in progress at once
    backfill_max_imports: int = 1                   # historical IMPORT_VIDEO jobs live at once
    backfill_interval: timedelta = timedelta(seconds=60)
    # -- alerts (prepared; delivery is not enabled in this phase) -------------------
    alert_destination: str = ""
    alert_delivery_enabled: bool = False

    # -- derived ------------------------------------------------------------------
    @property
    def writes_bunny(self) -> bool:
        return self.mode in (STAGING_WRITE, PRODUCTION_WRITE)

    @property
    def library_id(self) -> int | None:
        """The ONE library this process may write to (None in SHADOW)."""
        if self.mode == STAGING_WRITE:
            return self.staging_library_id
        if self.mode == PRODUCTION_WRITE:
            return self.production_library_id
        return None

    @property
    def cdn_hostname(self) -> str:
        """The CDN host the publisher reads its own caption files back from (None in SHADOW)."""
        if self.mode == STAGING_WRITE:
            return self.staging_cdn_hostname
        if self.mode == PRODUCTION_WRITE:
            return self.production_cdn_hostname
        return ""

    @property
    def cdn_signing_key(self) -> str:
        """The key the caption read-back signs with: none while CDN token authentication is DEFERRED."""
        return self.cdn_token_key if self.cdn_token_auth_policy == CDN_REQUIRED else ""

    @property
    def api_key(self) -> str:
        return self.production_api_key if self.mode == PRODUCTION_WRITE else self.staging_api_key

    @property
    def account_api_key(self) -> str:
        return (self.production_account_api_key if self.mode == PRODUCTION_WRITE
                else self.staging_account_api_key)

    def problems(self) -> list[str]:
        found = []
        if self.mode not in MODES:
            found.append(f"PUBLISHER_MODE must be one of {', '.join(MODES)}")
        for name in ("database_url", "graph_tenant_id", "graph_client_id", "graph_client_secret"):
            if not getattr(self, name):
                found.append(f"{name} is required")
        if self.database_url and database_endpoint(self.database_url) == "neon-pooled":
            found.append("PUBLISHER_DATABASE_URL must be Neon's DIRECT endpoint (host without '-pooler'): "
                         "the publisher holds session advisory locks")
        if self.start_date is None:
            found.append("PUBLISHER_START_DATE is required (earlier lectures are backfill scope)")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            found.append("PUBLISHER_END_DATE must not be before PUBLISHER_START_DATE")
        if self.discover_calendar and not (self.aptem_database_url and self.calendar_user_upn):
            found.append("calendar discovery needs APTEM_DATABASE_URL and KBC_LECTURE_CALENDAR_USER_UPN")
        if not self.lms_database_url:
            found.append("LMS_DATABASE_URL is required (LMS occurrence matching)")
        if not 1 <= self.workers <= 8:
            found.append("PUBLISHER_WORKERS must be 1..8")
        if not 1 <= self.max_imports_in_flight <= 10:
            found.append("PUBLISHER_MAX_IMPORTS_IN_FLIGHT must be 1..10")
        if not 1 <= self.lookback_days <= 14:
            found.append("PUBLISHER_LOOKBACK_DAYS must be 1..14")
        if not 1 <= self.backfill_max_days <= 366:
            found.append("PUBLISHER_BACKFILL_MAX_DAYS must be 1..366")
        if not 1 <= self.backfill_batch_size <= 100 or not 1 <= self.backfill_max_active <= 200:
            found.append("PUBLISHER_BACKFILL_BATCH_SIZE must be 1..100 and PUBLISHER_BACKFILL_MAX_ACTIVE 1..200")
        if not 1 <= self.backfill_max_imports <= self.max_imports_in_flight:
            found.append("PUBLISHER_BACKFILL_MAX_IMPORTS must be 1..PUBLISHER_MAX_IMPORTS_IN_FLIGHT")
        if self.discovery_interval < timedelta(minutes=1):
            found.append("PUBLISHER_DISCOVERY_INTERVAL_SECONDS must be >= 60")
        if self.alert_delivery_enabled:
            found.append("PUBLISHER_ALERT_DELIVERY_ENABLED: alert delivery is not enabled in this phase")
        for library in (self.staging_library_id, self.production_library_id):
            if library in FORBIDDEN_LIBRARY_IDS:
                found.append(f"library {library} is the old account's library and is never a destination")
        if self.mode == STAGING_WRITE:
            if not self.staging_library_id or not self.staging_api_key:
                found.append("STAGING_WRITE needs BUNNY_STAGING_LIBRARY_ID and BUNNY_STAGING_API_KEY")
            if not self.staging_account_api_key:
                found.append("STAGING_WRITE needs BUNNY_STAGING_ACCOUNT_API_KEY to verify Free encoding "
                             "and disabled AI transcribing")
        if self.mode == PRODUCTION_WRITE:
            if not self.production_write_enabled:
                found.append("PRODUCTION_WRITE is disabled (PUBLISHER_PRODUCTION_WRITE_ENABLED is not true)")
            if not self.production_library_id or not self.production_api_key or not self.production_account_api_key:
                found.append("PRODUCTION_WRITE needs BUNNY_PRODUCTION_LIBRARY_ID, _API_KEY and _ACCOUNT_API_KEY")
            if self.cdn_token_auth_policy == CDN_REQUIRED and not (self.cdn_token_key and self.production_cdn_hostname):
                found.append("PUBLISHER_CDN_TOKEN_AUTH=REQUIRED needs BUNNY_CDN_TOKEN_KEY and "
                             "BUNNY_PRODUCTION_CDN_HOSTNAME (the caption read-back must be signed)")
        if self.cdn_token_auth_policy not in CDN_POLICIES:
            found.append(f"PUBLISHER_CDN_TOKEN_AUTH must be one of {', '.join(CDN_POLICIES)}")
        if not self.approved_referrers:
            found.append("PUBLISHER_APPROVED_REFERRERS must name at least one LMS domain")
        if self.auto_release and self.mode != PRODUCTION_WRITE:
            found.append("PUBLISHER_AUTO_RELEASE needs PUBLISHER_MODE=PRODUCTION_WRITE")
        return found

    def validate(self) -> "PublisherConfig":
        problems = self.problems()
        if problems:
            raise ConfigError(problems)
        return self

    def describe(self) -> dict:
        """Safe to print: no secret values, no connection strings."""
        return {
            "mode": self.mode, "writes_bunny": self.writes_bunny, "library_id": self.library_id,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "database_endpoint": database_endpoint(self.database_url),
            "adopts_staging_library": self.mode == PRODUCTION_WRITE and self.production_library_id is not None
            and self.production_library_id == self.staging_library_id,
            "cdn_readback_configured": bool(self.cdn_hostname),
            "cdn_token_auth_policy": self.cdn_token_auth_policy,
            "cdn_readback_signed": bool(self.cdn_signing_key),
            "cdn_token_key_ignored": bool(self.cdn_token_key) and not self.cdn_signing_key,
            "approved_referrers": list(self.approved_referrers),
            "require_block_none_referrer": self.require_block_none_referrer,
            "auto_release": self.auto_release,
            "lookback_days": self.lookback_days, "workers": self.workers,
            "max_imports_in_flight": self.max_imports_in_flight,
            "backfill": {"max_days": self.backfill_max_days, "batch_size": self.backfill_batch_size,
                         "max_active": self.backfill_max_active, "max_imports": self.backfill_max_imports,
                         "interval_seconds": self.backfill_interval.total_seconds()},
            "discovery_interval_seconds": self.discovery_interval.total_seconds(),
            "light_reconcile_interval_hours": self.light_reconcile_interval.total_seconds() / 3600,
            "full_reconcile_at_cairo": self.full_reconcile_at.isoformat(timespec="minutes"),
            "test_programmes_configured": bool(self.test_programmes.strip()),
            "discover_calendar": self.discover_calendar,
            "alert_destination_configured": bool(self.alert_destination),
            "alert_delivery_enabled": self.alert_delivery_enabled,
            "present": {name: bool(getattr(self, name)) for name in (
                "database_url", "aptem_database_url", "lms_database_url", "graph_client_secret",
                "staging_api_key", "staging_account_api_key", "production_api_key",
                "production_account_api_key", "cdn_token_key")},
        }

    # -- construction ---------------------------------------------------------------------
    @classmethod
    def from_mapping(cls, env) -> "PublisherConfig":
        def text(name, default=""):
            return str(env.get(name) or default).strip()

        def number(name, default, kind=int):
            raw = text(name)
            try:
                return kind(raw) if raw else default
            except ValueError:
                raise ConfigError([f"{name} must be a number"]) from None

        def flag(name, default=False):
            raw = text(name).lower()
            return default if not raw else raw in ("1", "true", "yes", "on")

        def library(name):
            raw = text(name)
            return int(raw) if raw.isdigit() else None

        start, end = text("PUBLISHER_START_DATE"), text("PUBLISHER_END_DATE")
        full_at = text("PUBLISHER_FULL_RECONCILE_AT", "05:00")
        try:
            start_date = date.fromisoformat(start) if start else None
            end_date = date.fromisoformat(end) if end else None
            full_reconcile_at = time.fromisoformat(full_at)
        except ValueError:
            raise ConfigError(["PUBLISHER_START_DATE / PUBLISHER_END_DATE must be YYYY-MM-DD and "
                               "PUBLISHER_FULL_RECONCILE_AT HH:MM"]) from None
        return cls(
            mode=text("PUBLISHER_MODE", SHADOW).upper(),
            # The publisher's own (direct) URL first; the generic name only as a fallback.
            database_url=text("PUBLISHER_DATABASE_URL") or text("DATABASE_URL"),
            aptem_database_url=text("APTEM_DATABASE_URL"),
            lms_database_url=text("LMS_DATABASE_URL"),
            graph_tenant_id=text("MICROSOFT_GRAPH_TENANT_ID"), graph_client_id=text("MICROSOFT_GRAPH_CLIENT_ID"),
            graph_client_secret=text("MICROSOFT_GRAPH_CLIENT_SECRET"),
            graph_scope=text("MICROSOFT_GRAPH_SCOPE", "https://graph.microsoft.com/.default"),
            graph_base_url=text("MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"),
            calendar_user_upn=text("KBC_LECTURE_CALENDAR_USER_UPN"),
            recording_link_search_region=text("RECORDING_LINK_SEARCH_REGION").upper(),
            start_date=start_date, end_date=end_date, lookback_days=number("PUBLISHER_LOOKBACK_DAYS", 3),
            test_programmes=text("PUBLISHER_LMS_TEST_PROGRAMMES"),
            discover_calendar=flag("PUBLISHER_DISCOVER_CALENDAR", True),
            staging_library_id=library("BUNNY_STAGING_LIBRARY_ID"), staging_api_key=text("BUNNY_STAGING_API_KEY"),
            staging_account_api_key=text("BUNNY_STAGING_ACCOUNT_API_KEY"),
            staging_cdn_hostname=text("BUNNY_STAGING_CDN_HOSTNAME"),
            production_library_id=library("BUNNY_PRODUCTION_LIBRARY_ID"),
            production_api_key=text("BUNNY_PRODUCTION_API_KEY"),
            production_account_api_key=text("BUNNY_PRODUCTION_ACCOUNT_API_KEY"),
            production_write_enabled=flag("PUBLISHER_PRODUCTION_WRITE_ENABLED"),
            production_cdn_hostname=text("BUNNY_PRODUCTION_CDN_HOSTNAME"),
            cdn_token_key=text("BUNNY_CDN_TOKEN_KEY"),
            cdn_token_auth_policy=text("PUBLISHER_CDN_TOKEN_AUTH", CDN_DEFERRED).upper(),
            approved_referrers=tuple(sorted({d.strip().lower() for d in text(
                "PUBLISHER_APPROVED_REFERRERS", APPROVED_REFERRERS_DEFAULT).split(",") if d.strip()})),
            require_block_none_referrer=flag("PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER", True),
            auto_release=flag("PUBLISHER_AUTO_RELEASE"),
            workers=number("PUBLISHER_WORKERS", 2),
            max_imports_in_flight=number("PUBLISHER_MAX_IMPORTS_IN_FLIGHT", 2),
            poll_seconds=number("PUBLISHER_POLL_SECONDS", 5.0, float),
            discovery_interval=timedelta(seconds=number("PUBLISHER_DISCOVERY_INTERVAL_SECONDS", 300)),
            light_reconcile_interval=timedelta(hours=number("PUBLISHER_LIGHT_RECONCILE_HOURS", 6)),
            full_reconcile_at=full_reconcile_at,
            job_lease_seconds=number("PUBLISHER_JOB_LEASE_SECONDS", 900),
            encoding_stall_after=timedelta(hours=number("PUBLISHER_ENCODING_STALL_HOURS", 6)),
            max_import_generations=number("PUBLISHER_MAX_IMPORT_GENERATIONS", 3),
            backfill_max_days=number("PUBLISHER_BACKFILL_MAX_DAYS", 31),
            backfill_batch_size=number("PUBLISHER_BACKFILL_BATCH_SIZE", 10),
            backfill_max_active=number("PUBLISHER_BACKFILL_MAX_ACTIVE", 20),
            backfill_max_imports=number("PUBLISHER_BACKFILL_MAX_IMPORTS", 1),
            backfill_interval=timedelta(seconds=number("PUBLISHER_BACKFILL_INTERVAL_SECONDS", 60)),
            alert_destination=text("PUBLISHER_ALERT_DESTINATION"),
            alert_delivery_enabled=flag("PUBLISHER_ALERT_DELIVERY_ENABLED"))

    @classmethod
    def from_environment(cls, env_file: str | None = None) -> "PublisherConfig":
        """
        Process environment first; then the given env file (the systemd
        EnvironmentFile on the VPS). Exception: PUBLISHER_* names in the env file
        win, so a pooled DATABASE_URL / PUBLISHER_* left in a shell can never
        override the publisher's own file.
        """
        values = dict(os.environ)
        if env_file:
            from dotenv import dotenv_values
            for key, value in dotenv_values(env_file).items():
                if key.startswith("PUBLISHER_") and value:
                    values[key] = value
                else:
                    values.setdefault(key, value)
        return cls.from_mapping(values)
