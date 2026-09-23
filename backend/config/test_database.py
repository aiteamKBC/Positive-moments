"""
Fail-closed database selection for Django's test commands.

THE HAZARD THIS CLOSES
----------------------
`config.settings` builds `DATABASES["default"]` from `DATABASE_URL`, which on
an operator machine is PRODUCTION. Django's test runner then CREATEs (and later
DROPs) `test_<NAME>` on whatever server that points at, so `manage.py test`
created and dropped a database on the production server. The `test_` prefix is
no protection at all: it is the SERVER that must not be production.

THE RULE
--------
A Django test command uses `TEST_DATABASE_URL` and nothing else. It never falls
back to `DATABASE_URL`. The target must pass the same six-signal gate the
integration suite uses (tools/integration_db_guard), so a production host, a
production server with a `test_`-prefixed database name, a non-test database
name, or a missing configuration are all refused before Django connects.

Nothing here logs a URL, a user or a password.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.integration_db_guard import (  # noqa: E402
    TEST_URL_ENV,
    UnsafeTestDatabase,
    check_static,
    parse_target,
    production_urls,
)


# Commands that create or use a test database.
TEST_COMMANDS = {"test"}


class UnsafeDjangoTestDatabase(RuntimeError):
    """Refuse: the Django test database would be derived from production."""


def is_test_command(argv=None) -> bool:
    """True when this process is running Django's test command."""
    argv = sys.argv if argv is None else argv
    if os.environ.get("DJANGO_TEST_MODE") == "1":
        return True
    return any(arg in TEST_COMMANDS for arg in argv[1:2])


def approved_target(environ=None):
    """The approved test target, or raise UnsafeDjangoTestDatabase."""
    environ = os.environ if environ is None else environ
    try:
        return check_static(environ, production_urls(environ))
    except UnsafeTestDatabase as exc:
        raise UnsafeDjangoTestDatabase(
            f"refusing to run Django tests: {exc}") from None


def test_database_config(postgres_config, environ=None) -> dict:
    """
    The DATABASES["default"] entry a Django test run may use.

    `postgres_config` is settings.postgres_config, passed in so this module
    never imports Django settings (and stays unit-testable on its own).
    """
    environ = os.environ if environ is None else environ
    target = approved_target(environ)
    config = postgres_config(environ[TEST_URL_ENV].strip())
    # Django will create test_<NAME> on this server. Pin it explicitly so the
    # name is visible in the settings rather than derived silently.
    config["TEST"] = {"NAME": f"test_{target.dbname}"}
    config["CONN_MAX_AGE"] = 0
    return config


def verify_connection_is_not_production(settings_dict, environ=None) -> None:
    """
    Last line of defence, called once the connection settings are final.

    Re-checks the server Django is actually about to use - including the test
    database name Django derived - against the approved target and against
    every production URL this machine knows about.
    """
    environ = os.environ if environ is None else environ
    target = approved_target(environ)
    host = str(settings_dict.get("HOST") or "")
    port = str(settings_dict.get("PORT") or "5432")
    name = str(settings_dict.get("NAME") or "")
    if (host, port) != (target.host, target.port):
        raise UnsafeDjangoTestDatabase(
            "refusing to run Django tests: the configured server is not the approved "
            f"test server {target.describe()}")
    for candidate in production_urls(environ):
        other = parse_target(candidate)
        if (host or "localhost", port) == (other.host or "localhost", other.port):
            raise UnsafeDjangoTestDatabase(
                "refusing to run Django tests: that server is a production database "
                "server, whatever the database is called")
    if target.dbname not in name:
        raise UnsafeDjangoTestDatabase(
            f"refusing to run Django tests: {name!r} is not the approved test database")
