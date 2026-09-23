"""
`manage.py test` can no longer reach a production server.

The hazard was concrete: `config.settings` built DATABASES from DATABASE_URL,
so Django's test runner created - and dropped - `test_AiTeamKBC` on the
production server. These tests are offline: every refusal happens before a
connection is opened, and no real credential appears here.
"""
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from config.test_database import (  # noqa: E402
    UnsafeDjangoTestDatabase,
    is_test_command,
    test_database_config as build_test_database_config,
    verify_connection_is_not_production,
)
from tools import integration_db_guard as guard  # noqa: E402


PROD = "postgresql://svc_user:prod-secret-pw@db.prod.example.com:5432/AiTeamKBC"
TEST = "postgresql://kbc_test:test-secret-pw@127.0.0.1:55432/kbc_qa_integration_test"


def postgres_config(url: str) -> dict:
    """The real helper from config.settings, copied to keep this test offline."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""), "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "", "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60, "OPTIONS": {},
    }
    if query.get("sslmode"):
        config["OPTIONS"]["sslmode"] = query["sslmode"][0]
    return config


@pytest.fixture(autouse=True)
def no_env_files(monkeypatch):
    """Production URLs come from the environment under test, not this machine."""
    monkeypatch.setattr(guard, "PRODUCTION_ENV_FILES", ())


def env(**overrides):
    base = {"TEST_DATABASE_URL": TEST, "APP_ENV": "test", "DATABASE_URL": PROD}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def refused(environ):
    with pytest.raises(UnsafeDjangoTestDatabase) as error:
        build_test_database_config(postgres_config, environ)
    message = str(error.value)
    for secret in ("prod-secret-pw", "test-secret-pw", "svc_user"):
        assert secret not in message
    return message


# --- which commands are guarded --------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["manage.py", "test"], True),
    (["manage.py", "test", "positive_mentions"], True),
    (["manage.py", "runserver"], False),
    (["manage.py", "migrate"], False),
    (["manage.py"], False),
])
def test_only_a_test_command_is_guarded(argv, expected):
    assert is_test_command(argv) is expected


def test_an_explicit_test_mode_flag_also_guards(monkeypatch):
    monkeypatch.setenv("DJANGO_TEST_MODE", "1")
    assert is_test_command(["manage.py", "runserver"]) is True


# --- the refusals ----------------------------------------------------------

def test_a_test_run_without_a_test_database_url_is_refused():
    assert "never falls back to DATABASE_URL" in refused(env(TEST_DATABASE_URL=None))


def test_a_test_run_pointed_at_the_production_url_is_refused():
    assert "no 'test' marker" in refused(env(TEST_DATABASE_URL=PROD))


def test_a_test_prefixed_database_on_the_production_server_is_still_refused():
    """Django's own `test_` prefix is not a safety property."""
    sneaky = "postgresql://u:p@db.prod.example.com:5432/test_AiTeamKBC"
    assert "same server" in refused(
        env(TEST_DATABASE_URL=sneaky, KBC_TEST_DB_ALLOW_REMOTE_HOST="1"))


def test_a_local_database_without_a_test_name_is_refused():
    local = "postgresql://u:p@127.0.0.1:55432/kbcqa"
    assert "no 'test' marker" in refused(env(TEST_DATABASE_URL=local))


def test_a_test_run_without_app_env_test_is_refused():
    assert "APP_ENV=test" in refused(env(APP_ENV=None))


# --- the accepted case -----------------------------------------------------

def test_an_explicit_local_test_database_is_accepted_and_named():
    config = build_test_database_config(postgres_config, env())
    assert config["NAME"] == "kbc_qa_integration_test"
    assert config["HOST"] == "127.0.0.1" and config["PORT"] == 55432
    assert config["TEST"] == {"NAME": "test_kbc_qa_integration_test"}
    assert config["PASSWORD"]                     # carried, never logged


# --- the runner's own last check -------------------------------------------

def test_the_runner_refuses_a_connection_on_the_production_server():
    settings_dict = {"HOST": "db.prod.example.com", "PORT": 5432,
                     "NAME": "test_kbc_qa_integration_test"}
    with pytest.raises(UnsafeDjangoTestDatabase, match="approved test server"):
        verify_connection_is_not_production(settings_dict, env())


def test_the_runner_refuses_a_different_database_on_the_test_server():
    settings_dict = {"HOST": "127.0.0.1", "PORT": 55432, "NAME": "something_else"}
    with pytest.raises(UnsafeDjangoTestDatabase, match="not the approved test database"):
        verify_connection_is_not_production(settings_dict, env())


def test_the_runner_accepts_djangos_derived_test_database():
    settings_dict = {"HOST": "127.0.0.1", "PORT": 55432,
                     "NAME": "test_kbc_qa_integration_test"}
    verify_connection_is_not_production(settings_dict, env())


def test_the_runner_refuses_when_production_shares_the_test_server_address():
    """Even a loopback production tunnel on the same host:port is refused."""
    tunnelled = "postgresql://u:p@127.0.0.1:55432/AiTeamKBC"
    settings_dict = {"HOST": "127.0.0.1", "PORT": 55432,
                     "NAME": "test_kbc_qa_integration_test"}
    with pytest.raises(UnsafeDjangoTestDatabase):
        verify_connection_is_not_production(settings_dict,
                                            env(DATABASE_URL=tunnelled))


# --- the settings module itself --------------------------------------------

def test_the_settings_module_wires_the_safe_runner_and_never_falls_back():
    source = (Path(__file__).resolve().parents[2] / "backend" / "config"
              / "settings.py").read_text(encoding="utf-8")
    assert 'TEST_RUNNER = "config.test_runner.SafeDatabaseTestRunner"' in source
    assert "if is_test_command():" in source
    assert "test_database_config(postgres_config)" in source
