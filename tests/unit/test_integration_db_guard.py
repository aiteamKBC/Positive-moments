"""
The integration-database gate fails CLOSED.

Every test here is offline: the refusals happen before any socket is opened.
Fake URLs only - no real credential appears in this file.
"""
import socket

import psycopg
import pytest
from dotenv import load_dotenv

from tools import integration_db_guard as guard
from tools.integration_db_guard import UnsafeTestDatabase


PROD = "postgresql://svc_user:prod-secret-pw@db.prod.example.com:5432/kbc_production"
TEST = "postgresql://kbc_test:test-secret-pw@127.0.0.1:55432/kbc_qa_integration_test"


def _env(**overrides):
    env = {"TEST_DATABASE_URL": TEST, "APP_ENV": "test", "DATABASE_URL": PROD}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _refused(env, production=(PROD,)):
    with pytest.raises(UnsafeTestDatabase) as error:
        guard.check_static(env, list(production))
    message = str(error.value)
    # Never leak a credential or a full URL in a refusal.
    for secret in ("prod-secret-pw", "test-secret-pw", "svc_user", "db.prod.example.com"):
        assert secret not in message
    return message


# --- signal 1: an explicit TEST_DATABASE_URL, never a fallback ------------------

def test_missing_test_url_is_refused_even_when_database_url_is_set():
    message = _refused(_env(TEST_DATABASE_URL=None))
    assert "never falls back to DATABASE_URL" in message


def test_blank_test_url_is_refused():
    _refused(_env(TEST_DATABASE_URL="   "))


# --- signal 2: APP_ENV=test -----------------------------------------------------

@pytest.mark.parametrize("app_env", [None, "", "production", "dev", "testing"])
def test_app_env_must_be_exactly_test(app_env):
    assert "APP_ENV=test" in _refused(_env(APP_ENV=app_env))


# --- signal 3: a test marker in the database name -------------------------------

@pytest.mark.parametrize("dbname", ["kbc_production", "postgres", "latest", "contest",
                                    "attestation", "kbcqa"])
def test_database_name_without_a_test_token_is_refused(dbname):
    url = f"postgresql://u:p@127.0.0.1:55432/{dbname}"
    assert "no 'test' marker" in _refused(_env(TEST_DATABASE_URL=url))


def test_the_approved_test_database_name_is_accepted():
    url = "postgresql://u:p@127.0.0.1:55432/kbc_qa_integration_test"
    assert guard.check_static(_env(TEST_DATABASE_URL=url), [PROD]).dbname == \
        guard.APPROVED_TEST_DATABASE


@pytest.mark.parametrize("dbname", ["test_kbc", "qa-test-db", "kbc_other_test",
                                    "kbc_qa_integration_test_2"])
def test_a_test_token_is_not_enough_only_the_approved_name_is_accepted(dbname):
    """TEST_DATABASE_URL is not trusted merely because it is present and 'looks'
    like a test database: the name must be exactly the approved one."""
    url = f"postgresql://u:p@127.0.0.1:55432/{dbname}"
    assert "not the approved isolated test database" in _refused(_env(TEST_DATABASE_URL=url))


# --- signal 4: loopback unless explicitly allowed -------------------------------

def test_remote_host_is_refused_without_the_explicit_opt_in():
    url = "postgresql://u:p@10.0.0.9:5432/kbc_qa_integration_test"
    assert "not loopback" in _refused(_env(TEST_DATABASE_URL=url))


def test_remote_host_needs_the_opt_in_and_still_every_other_check():
    url = "postgresql://u:p@ci-db.internal:5432/kbc_qa_integration_test"
    env = _env(TEST_DATABASE_URL=url, KBC_TEST_DB_ALLOW_REMOTE_HOST="1")
    assert guard.check_static(env, [PROD]).host == "ci-db.internal"


# --- signal 5: never production -------------------------------------------------

def test_test_url_identical_to_database_url_is_refused():
    # A production database renamed *_test is still production.
    same = "postgresql://u:p@127.0.0.1:5432/kbc_qa_integration_test"
    assert "identical to a production URL" in _refused(
        _env(TEST_DATABASE_URL=same, DATABASE_URL=same), production=[same])


def test_a_test_database_on_the_production_server_is_refused():
    prod = "postgresql://u:p@db.prod.example.com:5432/kbc_production"
    other = "postgresql://u:p@db.prod.example.com:5432/kbc_qa_integration_test"
    env = _env(TEST_DATABASE_URL=other, KBC_TEST_DB_ALLOW_REMOTE_HOST="1")
    assert "same server" in _refused(env, production=[prod])


def test_loopback_production_is_distinguished_by_port():
    # Production reached through a local tunnel on 5432 is refused; a test
    # container on another loopback port is not the same server.
    tunnel = "postgresql://u:p@localhost:5432/kbc_production"
    same_port = "postgresql://u:p@127.0.0.1:5432/kbc_qa_integration_test"
    assert "same server" in _refused(_env(TEST_DATABASE_URL=same_port), production=[tunnel])
    assert guard.check_static(_env(), [tunnel]).port == "55432"


def test_production_urls_are_read_from_the_backend_env_files(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={PROD}\nAPTEM_DATABASE_URL=postgresql://a@aptem/x\n")
    monkeypatch.setattr(guard, "PRODUCTION_ENV_FILES", (env_file,))
    found = guard.production_urls({})
    assert PROD in found and len(found) == 2


def test_the_accepted_target_is_described_without_credentials():
    target = guard.check_static(_env(), [PROD])
    assert target.describe() == "127.0.0.1:55432/kbc_qa_integration_test"


# --- signal 6: the live marker --------------------------------------------------

class _FakeConnection:
    autocommit = True
    closed = False

    def __init__(self, dbname, marker=True, purpose=True):
        self.answers = {"current_database": dbname, "to_regclass": marker,
                        "count": 1 if purpose else 0}

    def execute(self, sql, params=None):
        key = next(k for k in self.answers if k in sql)
        answer = self.answers[key]
        return type("R", (), {"fetchone": lambda _self: (answer,)})()


def test_live_check_refuses_a_database_without_the_marker():
    target = guard.check_static(_env(), [PROD])
    with pytest.raises(UnsafeTestDatabase, match="kbc_test_database_marker"):
        guard.check_live(_FakeConnection(target.dbname, marker=False), target)


def test_live_check_refuses_a_marker_that_declares_another_purpose():
    target = guard.check_static(_env(), [PROD])
    with pytest.raises(UnsafeTestDatabase, match="does not declare"):
        guard.check_live(_FakeConnection(target.dbname, purpose=False), target)


def test_live_check_refuses_a_different_connected_database():
    target = guard.check_static(_env(), [PROD])
    with pytest.raises(UnsafeTestDatabase, match="not the approved isolated test database"):
        guard.check_live(_FakeConnection("kbc_production"), target, production=[])


def test_live_check_accepts_the_marked_test_database():
    target = guard.check_static(_env(), [PROD])
    guard.check_live(_FakeConnection(target.dbname), target, production=[PROD])


def test_live_check_refuses_a_production_database_name_even_if_marked():
    target = guard.check_static(_env(), [PROD])
    with pytest.raises(UnsafeTestDatabase, match="PRODUCTION database name"):
        guard.check_live(_FakeConnection("kbc_production"), target, production=[PROD])


def test_live_check_refuses_another_marked_test_token_database():
    target = guard.check_static(_env(), [PROD])
    with pytest.raises(UnsafeTestDatabase, match="not the approved isolated test database"):
        guard.check_live(_FakeConnection("kbc_other_test"), target, production=[PROD])


# --- the per-connection guard every writing test helper calls first -------------

class _Info:
    def __init__(self, url):
        target = guard.parse_target(url)
        self.dbname, self.host, self.port = target.dbname, target.host, target.port


class _UntouchableConnection:
    """A connection that must be refused before any statement is sent."""
    autocommit = True
    closed = False

    def __init__(self, url):
        self.info = _Info(url)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        raise AssertionError("a statement reached a connection the guard must refuse")


class _MarkedConnection(_FakeConnection):
    def __init__(self, url):
        super().__init__(guard.parse_target(url).dbname)
        self.info = _Info(url)
        self.statements = 0

    def execute(self, sql, params=None):
        self.statements += 1
        return super().execute(sql, params)


def _backend_production_url():
    from dotenv import dotenv_values

    values = dotenv_values(guard.REPO_ROOT / "backend" / ".env")
    return (values.get("DATABASE_URL") or "").strip()


def test_a_production_connection_is_refused_before_any_statement():
    connection = _UntouchableConnection(PROD)
    with pytest.raises(UnsafeTestDatabase, match="PRODUCTION database name") as error:
        guard.assert_isolated(connection, production=[PROD])
    assert connection.statements == []
    assert "prod-secret-pw" not in str(error.value)


def test_the_real_backend_env_production_database_is_refused_before_any_statement():
    """The regression that matters: a helper handed a connection opened from the
    operator's real DATABASE_URL (backend/.env) refuses it - by name, before a
    single statement, without the URL ever being printed."""
    production = _backend_production_url()
    if not production:
        pytest.skip("no backend/.env production URL on this machine")
    connection = _UntouchableConnection(production)
    with pytest.raises(UnsafeTestDatabase) as error:
        guard.assert_isolated(connection)          # production read from backend/.env
    assert connection.statements == []
    assert production not in str(error.value)
    assert "PRODUCTION database name" in str(error.value)


@pytest.mark.parametrize("url", [
    "postgresql://u:p@127.0.0.1:55432/kbc_other_test",
    "postgresql://u:p@127.0.0.1:55432/postgres",
    "postgresql://u:p@db.prod.example.com:5432/kbc_qa_integration_test",
])
def test_any_non_approved_connection_is_refused_before_any_statement(url, monkeypatch):
    monkeypatch.delenv(guard.ALLOW_REMOTE_ENV, raising=False)
    connection = _UntouchableConnection(url)
    with pytest.raises(UnsafeTestDatabase):
        guard.assert_isolated(connection, production=[PROD])
    assert connection.statements == []


def test_the_approved_connection_is_verified_live_once_then_cached():
    connection = _MarkedConnection(TEST)
    guard.assert_isolated(connection, production=[PROD])
    after_first = connection.statements
    assert after_first >= 2                        # current_database() + marker
    guard.assert_isolated(connection, production=[PROD])
    assert connection.statements == after_first


def test_seeding_helpers_refuse_a_production_connection_before_any_statement():
    """How the 2026-09-24 debug scripts reached production: they imported these
    helpers and handed them a DATABASE_URL connection. That now fails closed."""
    from tests.integration import seeding

    connection = _UntouchableConnection(PROD)
    with pytest.raises(UnsafeTestDatabase):
        seeding.insert(connection, "lecture_sessions", subject="x")
    with pytest.raises(UnsafeTestDatabase):
        seeding.seed_lecture(connection)
    assert connection.statements == []


def test_connect_test_database_never_falls_back_to_database_url(monkeypatch):
    monkeypatch.delenv(guard.TEST_URL_ENV, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", PROD)
    with pytest.raises(UnsafeTestDatabase, match="never falls back"):
        guard.connect_test_database()


def test_connect_test_database_refuses_a_non_approved_test_url(monkeypatch):
    monkeypatch.setenv(guard.TEST_URL_ENV, "postgresql://u:p@127.0.0.1:55432/kbc_other_test")
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(UnsafeTestDatabase, match="not the approved"):
        guard.connect_test_database()


# --- the process-wide connect guard ---------------------------------------------

def test_connect_guard_without_an_approved_target_refuses_every_connection():
    connect_guard = guard.ConnectGuard(None, "TEST_DATABASE_URL is not set")
    connect_guard.install()
    try:
        with pytest.raises(UnsafeTestDatabase, match="disabled in this test session"):
            psycopg.connect(PROD, connect_timeout=1)
        with pytest.raises(UnsafeTestDatabase):
            psycopg.Connection.connect(TEST, connect_timeout=1)
    finally:
        connect_guard.uninstall()
    assert len(connect_guard.refused) == 2 and not connect_guard.opened


def test_connect_guard_refuses_anything_but_the_approved_target():
    approved = guard.check_static(_env(), [PROD])
    connect_guard = guard.ConnectGuard(approved)
    connect_guard.install()
    try:
        for url in (PROD,
                    "postgresql://kbc_test:x@127.0.0.1:55432/postgres",
                    "postgresql://kbc_test:x@127.0.0.1:5432/kbc_qa_integration_test"):
            with pytest.raises(UnsafeTestDatabase, match="other than the approved"):
                psycopg.connect(url, connect_timeout=1)
        with pytest.raises(UnsafeTestDatabase):
            psycopg.connect(host="db.prod.example.com", dbname="kbc_qa_integration_test")
    finally:
        connect_guard.uninstall()
    assert not connect_guard.opened


# --- the network guard ----------------------------------------------------------

@pytest.mark.parametrize("host", ["graph.microsoft.com", "api.openai.com",
                                  "kbc.sharepoint.com", "n8n.example.com"])
def test_network_guard_blocks_external_services(host):
    network = guard.NetworkGuard()
    network.install()
    try:
        with pytest.raises(OSError):
            socket.getaddrinfo(host, 443)
        with pytest.raises(OSError):
            socket.create_connection((host, 443), timeout=1)
    finally:
        network.uninstall()
    assert network.blocked.count(host) == 2


def test_network_guard_lets_loopback_through():
    network = guard.NetworkGuard()
    network.install()
    try:
        assert socket.getaddrinfo("127.0.0.1", 80)
    finally:
        network.uninstall()
    assert not network.blocked


# --- the environment scrub ------------------------------------------------------

def test_scrub_blanks_secrets_so_dotenv_cannot_reload_production(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={PROD}\nQA_MODEL_API_KEY=sk-live\n"
                        "N8N_API_KEY=n8n-live\nMICROSOFT_GRAPH_CLIENT_SECRET=g\n"
                        "SCHEDULER_ENABLED=false\n")
    monkeypatch.setattr(guard, "PRODUCTION_ENV_FILES", (env_file,))
    for key in ("DATABASE_URL", "QA_MODEL_API_KEY", "N8N_API_KEY",
                "MICROSOFT_GRAPH_CLIENT_SECRET", "SCHEDULER_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", TEST)

    import os
    scrubbed = guard.scrub_environment(os.environ)
    load_dotenv(env_file, override=False)       # exactly what Settings does

    assert {"DATABASE_URL", "QA_MODEL_API_KEY", "N8N_API_KEY",
            "MICROSOFT_GRAPH_CLIENT_SECRET"} <= set(scrubbed)
    assert os.environ["DATABASE_URL"] == ""
    assert os.environ["QA_MODEL_API_KEY"] == ""
    assert os.environ["TEST_DATABASE_URL"] == TEST       # never scrubbed
    assert "SCHEDULER_ENABLED" not in scrubbed           # not a secret


def test_this_session_itself_is_isolated(isolation_state):
    """The session guards are installed for every run, unit runs included."""
    # Asserted structurally: a live probe here would add a deliberate entry to
    # the session's blocked-attempts evidence, which must stay a real signal.
    assert isolation_state["connect"]._original is not None
    assert isolation_state["network"]._saved is not None
    assert psycopg.Connection.connect.__func__ is not isolation_state["connect"]._original
