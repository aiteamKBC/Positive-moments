"""
The integration database gate, proven live against the isolated test server.

Every refusal below happens in-process before libpq is asked to connect, or in
a child process that exits before it connects. The production connection
string is read from backend/.env into memory only and is never printed.
"""
import os
import subprocess
import sys

import psycopg
import pytest
from dotenv import dotenv_values

from app.config.settings import Settings
from tools import integration_db_guard as guard


pytestmark = pytest.mark.usefixtures("approved")


@pytest.fixture
def approved(isolation_state):
    if isolation_state["target"] is None:
        pytest.skip("no approved test database")
    return isolation_state["target"]


def _url_for(dbname):
    base = os.environ[guard.TEST_URL_ENV]
    return base.rsplit("/", 1)[0] + "/" + dbname


def _child(env_overrides, *args):
    env = {k: v for k, v in os.environ.items()}
    # This session pointed DATABASE_URL at the test database; a child must see
    # the operator's view, where DATABASE_URL is production (or absent), so the
    # refusal it reports is the one under test and not that coincidence.
    env["DATABASE_URL"] = ""
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-m", "tools.bootstrap_integration_db", *args],
                          cwd=guard.REPO_ROOT, env=env, capture_output=True, text=True,
                          timeout=120)


def test_settings_resolve_to_the_approved_test_database_only(approved):
    target = guard.parse_target(Settings.from_environment().database_url)
    assert (target.server, target.dbname) == (approved.server, approved.dbname)
    for key in ("QA_MODEL_API_KEY", "N8N_API_KEY", "MICROSOFT_GRAPH_CLIENT_SECRET",
                "APTEM_DATABASE_URL"):
        assert os.environ.get(key, "") == "", f"{key} was not scrubbed"


def test_the_approved_database_carries_the_marker(approved):
    with psycopg.connect(Settings.from_environment().database_url) as connection:
        purpose = connection.execute(
            f"SELECT purpose FROM public.{guard.MARKER_TABLE}").fetchall()
        assert purpose == [(guard.MARKER_PURPOSE,)]


def test_another_database_on_the_same_test_server_is_refused(isolation_state):
    before = len(isolation_state["connect"].opened)
    with pytest.raises(guard.UnsafeTestDatabase, match="other than the approved"):
        psycopg.connect(_url_for("postgres"), connect_timeout=2)
    assert len(isolation_state["connect"].opened) == before


def test_the_real_production_url_is_refused_in_process(isolation_state):
    production = dotenv_values(guard.REPO_ROOT / "backend" / ".env").get("DATABASE_URL")
    if not production:
        pytest.skip("no backend/.env production URL on this machine")
    before = len(isolation_state["connect"].opened)
    with pytest.raises(guard.UnsafeTestDatabase) as error:
        psycopg.connect(production, connect_timeout=2)
    assert production not in str(error.value)
    assert len(isolation_state["connect"].opened) == before


def test_bootstrap_refuses_the_production_url_before_connecting():
    production = dotenv_values(guard.REPO_ROOT / "backend" / ".env").get("DATABASE_URL")
    if not production:
        pytest.skip("no backend/.env production URL on this machine")
    result = _child({guard.TEST_URL_ENV: production, "APP_ENV": "test"})
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert production not in result.stdout + result.stderr


def test_bootstrap_refuses_to_rebuild_the_marked_database_without_reset():
    result = _child({})
    assert result.returncode == 2
    assert "already built" in result.stderr


def test_bootstrap_refuses_an_unmarked_database_that_already_holds_tables(approved):
    scratch = "kbc_unmarked_scratch_test"
    admin = Settings.from_environment().database_url
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f"DROP DATABASE IF EXISTS {scratch}")
        connection.execute(f"CREATE DATABASE {scratch}")
    try:
        # A child process: this session's guard (rightly) refuses the scratch db.
        seed = subprocess.run(
            [sys.executable, "-c",
             "import os, psycopg; "
             "c = psycopg.connect(os.environ['U'], autocommit=True); "
             "c.execute('CREATE TABLE someone_elses_data (x int)')"],
            env={**os.environ, "U": _url_for(scratch)}, capture_output=True, text=True,
            timeout=60)
        assert seed.returncode == 0, seed.stderr[-300:]
        result = _child({guard.TEST_URL_ENV: _url_for(scratch)}, "--reset")
        assert result.returncode == 2
        assert "did not build" in result.stderr
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
