"""
Session-wide isolation for every pytest run in this repository.

Before any test module can open a connection:

  * every secret / endpoint variable is blanked - including the ones only
    present in backend/.env - so `Settings.from_environment()` cannot load the
    production DATABASE_URL, Graph, OpenAI or n8n credentials;
  * psycopg may connect ONLY to the database approved by
    tools/integration_db_guard (TEST_DATABASE_URL + APP_ENV=test + test name +
    loopback + not a production server + live marker). Anything else raises;
  * every non-loopback socket and DNS lookup raises, so an accidental Graph,
    OpenAI, n8n or SharePoint call fails the test instead of leaving the box.

When TEST_DATABASE_URL is absent, integration tests are SKIPPED with that
reason (nothing can connect anyway). When it is present but fails any check, the
whole session aborts: a misconfigured test target is never a skip.
"""
import os

import pytest

from tools import integration_db_guard as guard


_STATE = {"target": None, "refusal": "", "scrubbed": [], "connect": None, "network": None}


def pytest_configure(config):
    production = guard.production_urls()          # read BEFORE the scrub
    _STATE["scrubbed"] = guard.scrub_environment()
    try:
        target = guard.check_static(production=production)
    except guard.UnsafeTestDatabase as exc:
        if (os.environ.get(guard.TEST_URL_ENV) or "").strip():
            raise pytest.UsageError(f"integration database REFUSED: {exc}")
        target = None
        _STATE["refusal"] = str(exc)

    if target is not None:
        import psycopg

        url = os.environ[guard.TEST_URL_ENV].strip()
        try:
            with psycopg.connect(url, connect_timeout=10) as connection:
                guard.check_live(connection, target)
        except guard.UnsafeTestDatabase as exc:
            raise pytest.UsageError(f"integration database REFUSED: {exc}")
        except psycopg.Error as exc:
            raise pytest.UsageError(
                f"integration database {target.describe()} is unreachable "
                f"({type(exc).__name__})")
        # The ONLY value DATABASE_URL can hold for the rest of the session.
        os.environ["DATABASE_URL"] = url
        _STATE["target"] = target

    _STATE["connect"] = guard.ConnectGuard(_STATE["target"], _STATE["refusal"])
    _STATE["connect"].install()
    _STATE["network"] = guard.NetworkGuard()
    _STATE["network"].install()


def pytest_collection_modifyitems(config, items):
    if _STATE["target"] is not None:
        return
    reason = f"integration suite disabled: {_STATE['refusal']}"
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        if "tests/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(marker)


def pytest_report_header(config):
    target = _STATE["target"]
    return [
        "integration database: " + (target.describe() if target else "NONE (integration tests skipped)"),
        f"environment scrubbed: {len(_STATE['scrubbed'])} secret/endpoint variables blanked",
        "external network: blocked (loopback only)",
    ]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    connect, network = _STATE["connect"], _STATE["network"]
    if connect is None:
        return
    terminalreporter.section("database / network isolation")
    target = _STATE["target"]
    terminalreporter.line(
        f"approved database: {target.describe() if target else 'none'}")
    terminalreporter.line(f"psycopg connections opened: {len(connect.opened)} "
                          f"(distinct targets: {sorted(set(connect.opened))})")
    terminalreporter.line(f"psycopg connections refused: {len(connect.refused)}")
    terminalreporter.line(f"external network attempts blocked: {len(network.blocked)} "
                          f"{sorted(set(network.blocked))}")


@pytest.fixture(scope="session")
def isolation_state():
    """The live guards, for tests that assert on the isolation itself."""
    return _STATE
