"""
The one runtime environment file: `backend/.env`.

Every backend process - Django / Operations, QA Core, the nightly scheduler, the
background QA / backfill runner, the media runner and the Bunny publisher - reads
its configuration from the same two places, in this order:

  1. the process environment (on the VPS: systemd `EnvironmentFile=backend/.env`);
  2. `backend/.env` itself, filling ONLY names the process environment does not
     already have (python-dotenv, `override=False`).

So a value exported in a shell, or set by systemd, always wins over the file, and
the file can never silently replace it. Under systemd both layers are the same
file, so they cannot disagree.

The files that held production configuration before the consolidation
(`backend/config/.env`, `backend/qa-core.runtime.env`,
`backend/config/publisher.env`) are no longer read at runtime. They are listed in
LEGACY_ENV_FILES only so safety checks (the integration-test database guard) keep
treating whatever they still contain as production. Nothing here deletes them.

This module deliberately imports nothing from the application: the publisher
(which must not import `app.config.settings`) and Django settings both use it.

COMPONENT_VARIABLES is the complete list of names each component reads. The VPS
migration (`deploy/consolidate_env.py`) uses it to prove that merging the old
per-service files changes no value any service actually reads, and
tests/unit/test_environment_consolidation.py fails if code starts reading a name
that is missing here.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

CANONICAL_ENV_FILE = REPO_ROOT / "backend" / ".env"

# Pre-consolidation production env files. Never read at runtime; never deleted by code.
LEGACY_ENV_FILES = (
    REPO_ROOT / "backend" / "config" / ".env",
    REPO_ROOT / "backend" / "qa-core.runtime.env",
    REPO_ROOT / "backend" / "config" / "publisher.env",
)


def load_canonical_env(path: Path | None = None) -> None:
    """Fill names missing from os.environ from backend/.env. Never overrides a set name."""
    from dotenv import load_dotenv

    load_dotenv(path or CANONICAL_ENV_FILE, override=False)


def canonical_values(path: Path | None = None) -> dict[str, str]:
    """The names and values in backend/.env (empty if the file is absent). Never printed."""
    path = path or CANONICAL_ENV_FILE
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(path).items() if value is not None}


# --- who reads what ---------------------------------------------------------------------------

DJANGO_VARIABLES = frozenset({
    "CLIP_ON_DEMAND_WEBHOOK_URL", "CORS_ALLOWED_ORIGINS", "DASHBOARD_PASSWORD",
    "DASHBOARD_USERNAME", "DATABASE_URL", "DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS",
    "DJANGO_DEBUG", "DJANGO_SECRET_KEY", "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_CLIENT_SECRET", "MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_SSO_CLIENT_ID",
    "MICROSOFT_SSO_CLIENT_SECRET", "MICROSOFT_SSO_EMAIL_DOMAINS", "MICROSOFT_SSO_REDIRECT_URI",
    "MICROSOFT_SSO_TENANT_ID", "PUBLISHER_API_DOCS_ENABLED", "PUBLISHER_BACKFILL_MAX_DAYS",
    "PUBLISHER_LMS_CAPTION_LANGUAGE", "PUBLISHER_LMS_LIBRARY_ID", "PUBLISHER_LMS_PLAYBACK_ENABLED",
    "PUBLISHER_LMS_RATE", "PUBLISHER_LMS_SERVICE_TOKENS", "SESSION_COOKIE_SECURE",
    "SOCIAL_MEDIA_MODEL_PRICES", "SOCIAL_MEDIA_REANALYSIS_WEBHOOK_URL",
    "SOCIAL_MEDIA_SERVICE_TOKEN",
})

QA_CORE_VARIABLES = frozenset({          # app.config.settings.Settings (QA, Graph, media, recordings)
    "APTEM_DATABASE_URL", "CREATOMATE_API_KEY", "DATABASE_URL", "DJANGO_SECRET_KEY",
    "KBC_LECTURE_CALENDAR_USER_UPN", "LMS_DATABASE_URL", "MEDIA_POSITIVE_MOMENTS_FOLDER_ITEM_ID",
    "MEDIA_RENDER_FPS", "MEDIA_RENDER_MAX_HEIGHT", "MEDIA_RENDER_MAX_WIDTH",
    "MEDIA_RENDER_PROVIDER", "MEDIA_RUNNER_CONCURRENCY", "MEDIA_SHAREPOINT_DRIVE_ID",
    "MEDIA_WEBHOOK_BASE_URL", "MEDIA_WEBHOOK_SECRET", "MICROSOFT_GRAPH_BASE_URL",
    "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET", "MICROSOFT_GRAPH_SCOPE",
    "MICROSOFT_GRAPH_TENANT_ID", "POSITIVE_MOMENTS_MODEL_NAME",
    "POSITIVE_MOMENT_PADDING_AFTER_SECONDS", "POSITIVE_MOMENT_PADDING_BEFORE_SECONDS",
    "QA_MODEL_API_KEY", "QA_MODEL_BASE_URL", "QA_MODEL_NAME",
    "RECORDING_FULL_MIN_EXPECTED_DURATION_RATIO", "RECORDING_LINK_CREATE_ORG_LINKS",
    "RECORDING_LINK_MODE", "RECORDING_LINK_SEARCH_REGION",
    "RECORDING_SHORT_FRAGMENT_MAX_EXPECTED_RATIO", "RECORDING_SIZE_DOMINANCE_RATIO",
})

SCHEDULER_VARIABLES = frozenset({        # app.orchestration.scheduler.ENVIRONMENT_VARIABLES
    "SCHEDULER_ENABLED", "SCHEDULER_TIMEZONE", "SCHEDULER_CRON", "SCHEDULER_LOOKBACK_DAYS",
    "SCHEDULER_DISCOVER", "SCHEDULER_ALLOW_GRAPH", "SCHEDULER_ALLOW_PROVIDER",
    "SCHEDULER_ALLOW_LEGACY_WRITES", "SCHEDULER_MAX_PASSES",
})

N8N_VARIABLES = frozenset({"N8N_BASE_URL", "N8N_API_KEY"})

PUBLISHER_VARIABLES = frozenset({        # app.publisher.config.PublisherConfig
    "APTEM_DATABASE_URL", "BUNNY_CDN_TOKEN_KEY", "BUNNY_PRODUCTION_ACCOUNT_API_KEY",
    "BUNNY_PRODUCTION_API_KEY", "BUNNY_PRODUCTION_CDN_HOSTNAME", "BUNNY_PRODUCTION_LIBRARY_ID",
    "BUNNY_STAGING_ACCOUNT_API_KEY", "BUNNY_STAGING_API_KEY", "BUNNY_STAGING_CDN_HOSTNAME",
    "BUNNY_STAGING_LIBRARY_ID", "KBC_LECTURE_CALENDAR_USER_UPN", "LMS_DATABASE_URL",
    "MICROSOFT_GRAPH_BASE_URL", "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET",
    "MICROSOFT_GRAPH_SCOPE", "MICROSOFT_GRAPH_TENANT_ID", "PUBLISHER_ALERT_DELIVERY_ENABLED",
    "PUBLISHER_ALERT_DESTINATION", "PUBLISHER_APPROVED_REFERRERS", "PUBLISHER_AUTO_RELEASE",
    "PUBLISHER_BACKFILL_BATCH_SIZE", "PUBLISHER_BACKFILL_DISCOVERY_DAYS",
    "PUBLISHER_BACKFILL_INTERVAL_SECONDS", "PUBLISHER_BACKFILL_MAX_ACTIVE",
    "PUBLISHER_BACKFILL_MAX_DAYS", "PUBLISHER_BACKFILL_MAX_IMPORTS", "PUBLISHER_CDN_TOKEN_AUTH",
    "PUBLISHER_DATABASE_URL", "PUBLISHER_DISCOVERY_INTERVAL_SECONDS",
    "PUBLISHER_DISCOVER_CALENDAR", "PUBLISHER_ENCODING_STALL_HOURS", "PUBLISHER_END_DATE",
    "PUBLISHER_FULL_RECONCILE_AT", "PUBLISHER_JOB_LEASE_SECONDS",
    "PUBLISHER_LIGHT_RECONCILE_HOURS", "PUBLISHER_LMS_TEST_PROGRAMMES", "PUBLISHER_LOOKBACK_DAYS",
    "PUBLISHER_MAX_IMPORTS_IN_FLIGHT", "PUBLISHER_MAX_IMPORT_GENERATIONS", "PUBLISHER_MODE",
    "PUBLISHER_POLL_SECONDS", "PUBLISHER_PRODUCTION_WRITE_ENABLED",
    "PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER", "PUBLISHER_START_DATE", "PUBLISHER_WORKERS",
    "RECORDING_LINK_SEARCH_REGION",
})

BUNNY_CLI_VARIABLES = frozenset({        # app.cli.bunny (operator pilot / lms-match only)
    "BUNNY_STAGING_LIBRARY_ID", "BUNNY_STAGING_API_KEY", "BUNNY_STAGING_CDN_HOSTNAME",
    "BUNNY_STAGING_ACCOUNT_API_KEY", "MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_CLIENT_SECRET", "MICROSOFT_GRAPH_SCOPE", "MICROSOFT_GRAPH_BASE_URL",
    "LMS_DATABASE_URL", "PUBLISHER_LMS_TEST_PROGRAMMES",
})

COMPONENT_VARIABLES = {
    "django": DJANGO_VARIABLES,
    "qa_core": QA_CORE_VARIABLES,
    "scheduler": SCHEDULER_VARIABLES,
    "n8n": N8N_VARIABLES,
    "publisher": PUBLISHER_VARIABLES,
    "bunny_cli": BUNNY_CLI_VARIABLES,
}

# The VPS units and the components each one runs. The four QA units share one
# process image and import graph, so each is treated as reading every QA-side name
# (conservative: a change to any of them is checked for every QA unit).
QA_SIDE_VARIABLES = DJANGO_VARIABLES | QA_CORE_VARIABLES | SCHEDULER_VARIABLES | N8N_VARIABLES

UNIT_VARIABLES = {
    "positive-moments": QA_SIDE_VARIABLES,
    "positive-moments-media": QA_SIDE_VARIABLES,
    "positive-moments-scheduler": QA_SIDE_VARIABLES,
    "positive-moments-backfill": QA_SIDE_VARIABLES,
    "bunny-publisher": PUBLISHER_VARIABLES,
}

# Units whose Python code ALSO fills gaps from backend/.env (load_dotenv, override=False).
# The publisher's systemd ExecStart passes no --env-file, and before the consolidation it
# read no file at all; it now fills gaps from backend/.env like everything else.
QA_UNITS = ("positive-moments", "positive-moments-media",
            "positive-moments-scheduler", "positive-moments-backfill")
PUBLISHER_UNIT = "bunny-publisher"

# A unit refuses to be migrated when any of these would be absent or empty afterwards.
CRITICAL_VARIABLES = {
    "positive-moments": ("DATABASE_URL", "DJANGO_SECRET_KEY", "MICROSOFT_GRAPH_TENANT_ID",
                         "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET"),
    "positive-moments-media": ("DATABASE_URL", "MICROSOFT_GRAPH_TENANT_ID",
                               "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET"),
    "positive-moments-scheduler": ("DATABASE_URL", "MICROSOFT_GRAPH_TENANT_ID",
                                   "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET",
                                   "QA_MODEL_API_KEY", "SCHEDULER_ENABLED"),
    "positive-moments-backfill": ("DATABASE_URL", "MICROSOFT_GRAPH_TENANT_ID",
                                  "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET",
                                  "QA_MODEL_API_KEY"),
    "bunny-publisher": ("PUBLISHER_DATABASE_URL", "PUBLISHER_MODE", "PUBLISHER_START_DATE",
                        "LMS_DATABASE_URL", "MICROSOFT_GRAPH_TENANT_ID",
                        "MICROSOFT_GRAPH_CLIENT_ID", "MICROSOFT_GRAPH_CLIENT_SECRET"),
}
