"""
Fail-closed gate that proves a database is an isolated TEST database.

WHY THIS EXISTS
---------------
Every integration test used to reach its database through
`Settings.from_environment()`, which loads `backend/.env`. On an operator
machine that file holds the PRODUCTION `DATABASE_URL`, so `pytest` - or even a
bare `pytest` with the default `testpaths` - ran rolled-back DML against the
production database. Rolled back is not the same as untouched: locks, sequence
advances, trigger side effects and a single missed rollback are all real.

THE RULE
--------
The integration suite runs against `TEST_DATABASE_URL` and nothing else. It
never falls back to `DATABASE_URL`. A target is accepted only when EVERY signal
below agrees; any single failure refuses it:

  1. `TEST_DATABASE_URL` is set explicitly.
  2. `APP_ENV=test` is set explicitly.
  3. The database name is EXACTLY the approved isolated test database,
     `kbc_qa_integration_test`. A name merely containing `test` is not enough,
     and a URL is not trusted merely because it sits in TEST_DATABASE_URL.
  4. The host is loopback, unless `KBC_TEST_DB_ALLOW_REMOTE_HOST=1`.
  5. It is not the same URL, nor the same host:port server, as any
     `DATABASE_URL` / `APTEM_DATABASE_URL` found in the environment or in the
     backend env files. Production is identified at run time, never hardcoded.
  6. LIVE: the connected `current_database()` is exactly the approved name, is
     not the name of any production database, and it holds the
     `kbc_test_database_marker` row that only `tools/bootstrap_integration_db.py`
     writes - and that tool applies checks 1-5 before it will write anything.

The same checks guard every test helper that writes (`assert_isolated`, called
by tests/integration/seeding.py) and the one sanctioned way for a helper or a
debug script to open a connection (`connect_test_database`). A script that
imports the harness but connects through `Settings.from_environment()` - whose
DATABASE_URL is production on an operator machine - is refused before its
first statement.

Nothing here ever prints a URL, a user or a password. `describe()` reports
host:port/dbname only.
"""
from __future__ import annotations

import os
import re
import socket
import weakref
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_URL_ENV = "TEST_DATABASE_URL"
APP_ENV = "APP_ENV"
ALLOW_REMOTE_ENV = "KBC_TEST_DB_ALLOW_REMOTE_HOST"

# The one database the integration harness may ever write to.
APPROVED_TEST_DATABASE = "kbc_qa_integration_test"

MARKER_TABLE = "kbc_test_database_marker"
MARKER_PURPOSE = "kbc-qa-integration-test"

# `test` as a whole token of the database name: kbc_qa_integration_test,
# test_kbc, qa-test-db. Not "latest", "contest" or "attestation".
TEST_NAME = re.compile(r"(^|[_\-.])test($|[_\-.])", re.IGNORECASE)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Env files that may carry a production connection string. Read, never loaded.
PRODUCTION_ENV_FILES = (
    REPO_ROOT / "backend" / ".env",
    REPO_ROOT / "backend" / "qa-core.runtime.env",
    REPO_ROOT / "deploy" / "qa-core.env",
)
PRODUCTION_URL_KEYS = ("DATABASE_URL", "APTEM_DATABASE_URL")

# Anything that can reach an external service or a production database. The
# gate blanks these for the whole pytest session so `load_dotenv(override=False)`
# cannot refill them from backend/.env.
SECRET_NAME = re.compile(
    r"(DATABASE_URL|_URL$|_KEY$|SECRET|TOKEN|PASSWORD|TENANT|CLIENT_ID|UPN|WEBHOOK|DSN)")


class UnsafeTestDatabase(RuntimeError):
    """The target is not provably an isolated test database. Refuse it."""


@dataclass(frozen=True)
class Target:
    host: str
    port: str
    dbname: str

    def describe(self) -> str:
        return f"{self.host or '<local socket>'}:{self.port}/{self.dbname}"

    @property
    def server(self) -> tuple[str, str]:
        return (_normal_host(self.host), self.port)


def _normal_host(host: str) -> str:
    host = (host or "").strip().lower()
    return "localhost" if host in LOOPBACK_HOSTS or host == "" else host


def parse_target(conninfo: str = "", **kwargs) -> Target:
    """host/port/dbname of a libpq URL or key=value string, plus kwargs."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(conninfo or "", **kwargs)
    except Exception as exc:  # never echo the conninfo: it may hold a password
        raise UnsafeTestDatabase(
            f"connection string could not be parsed ({type(exc).__name__})") from None
    host = str(params.get("host") or params.get("hostaddr") or "")
    port = str(params.get("port") or "5432")
    dbname = str(params.get("dbname") or params.get("user") or "")
    return Target(host=host, port=port, dbname=dbname)


def production_urls(environ=None) -> list[str]:
    """Every connection string that must be treated as production."""
    environ = os.environ if environ is None else environ
    found = [environ.get(key, "") for key in PRODUCTION_URL_KEYS]
    for path in PRODUCTION_ENV_FILES:
        if path.is_file():
            values = dotenv_values(path)
            found.extend(values.get(key) or "" for key in PRODUCTION_URL_KEYS)
    return [url.strip() for url in found if url and url.strip()]


def check_static(environ=None, production=None) -> Target:
    """Signals 1-5. Returns the approved target or raises UnsafeTestDatabase."""
    environ = os.environ if environ is None else environ
    production = production_urls(environ) if production is None else production

    url = (environ.get(TEST_URL_ENV) or "").strip()
    if not url:
        raise UnsafeTestDatabase(
            f"{TEST_URL_ENV} is not set. The integration suite never falls back to "
            "DATABASE_URL.")
    if (environ.get(APP_ENV) or "").strip().lower() != "test":
        raise UnsafeTestDatabase(f"{APP_ENV}=test is required to run integration tests")

    target = parse_target(url)
    if not target.dbname or not TEST_NAME.search(target.dbname):
        raise UnsafeTestDatabase(
            f"database name {target.dbname!r} carries no 'test' marker")
    if target.dbname != APPROVED_TEST_DATABASE:
        raise UnsafeTestDatabase(
            f"database name {target.dbname!r} is not the approved isolated test "
            f"database {APPROVED_TEST_DATABASE!r}")
    if (_normal_host(target.host) != "localhost"
            and (environ.get(ALLOW_REMOTE_ENV) or "") != "1"):
        raise UnsafeTestDatabase(
            f"test database host is not loopback; set {ALLOW_REMOTE_ENV}=1 only for "
            "a dedicated, disposable test server")

    for candidate in production:
        if url == candidate:
            raise UnsafeTestDatabase(f"{TEST_URL_ENV} is identical to a production URL")
        try:
            other = parse_target(candidate)
        except UnsafeTestDatabase:
            continue
        if target.server == other.server:
            raise UnsafeTestDatabase(
                f"{TEST_URL_ENV} points at the same server (host:port) as a production "
                "DATABASE_URL; a test database must live on its own server")
        if target.dbname == other.dbname and _normal_host(target.host) == _normal_host(other.host):
            raise UnsafeTestDatabase(f"{TEST_URL_ENV} names a production database")
    return target


def production_database_names(production=None) -> set[str]:
    """The database names of every production connection string, lower-cased."""
    if production is None:
        # Inside a pytest session conftest points DATABASE_URL at the approved
        # test URL (after check_static refused any identical pair), so that one
        # value is not production. backend/.env is still read.
        test_url = (os.environ.get(TEST_URL_ENV) or "").strip()
        production = [url for url in production_urls() if not test_url or url != test_url]
    names = set()
    for candidate in production:
        try:
            names.add(parse_target(candidate).dbname.lower())
        except UnsafeTestDatabase:
            continue
    names.discard("")
    return names


def _refuse_name(name: str, production=None) -> None:
    if name.lower() in production_database_names(production):
        raise UnsafeTestDatabase(
            f"connected to {name!r}, which is a PRODUCTION database name")
    if name != APPROVED_TEST_DATABASE:
        raise UnsafeTestDatabase(
            f"connected to {name!r}, not the approved isolated test database "
            f"{APPROVED_TEST_DATABASE!r}")


def check_live(connection, target: Target, production=None) -> None:
    """Signal 6, on an open connection. Leaves no transaction open."""
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        _refuse_name(name, production)
        if name != target.dbname:
            raise UnsafeTestDatabase(
                f"connected to {name!r}, expected the test database {target.dbname!r}")
        present = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{MARKER_TABLE}",)).fetchone()[0]
        if not present:
            raise UnsafeTestDatabase(
                f"{target.describe()} has no {MARKER_TABLE}; only a database built by "
                "tools/bootstrap_integration_db.py is accepted")
        marked = connection.execute(
            f"SELECT count(*) FROM public.{MARKER_TABLE} WHERE purpose = %s",
            (MARKER_PURPOSE,)).fetchone()[0]
        if not marked:
            raise UnsafeTestDatabase(f"{MARKER_TABLE} does not declare {MARKER_PURPOSE!r}")
    finally:
        if not connection.autocommit and not connection.closed:
            connection.rollback()


# --- per-connection guard for helpers and scripts -----------------------------

_VERIFIED: "weakref.WeakSet" = weakref.WeakSet()


def assert_isolated(connection, production=None) -> None:
    """
    Fail closed unless `connection` is the approved, marked test database.

    First from the connection's own libpq parameters - before a single
    statement is sent, so a production connection runs nothing, not even a
    SELECT - then live: current_database() and the marker. The live half is
    cached per connection object. Test helpers that write call this first.
    """
    info = getattr(connection, "info", None)
    dbname = str(getattr(info, "dbname", "") or "")
    host = str(getattr(info, "host", "") or "")
    if not dbname:
        raise UnsafeTestDatabase("connection does not report its database name")
    _refuse_name(dbname, production)
    if (_normal_host(host) != "localhost" and not host.startswith("/")
            and (os.environ.get(ALLOW_REMOTE_ENV) or "") != "1"):
        raise UnsafeTestDatabase("test helper connection is not on a loopback host")
    try:
        if connection in _VERIFIED:
            return
    except TypeError:  # an object that cannot be weakly referenced: re-check live
        pass
    target = Target(host=host, port=str(getattr(info, "port", "") or ""), dbname=dbname)
    check_live(connection, target, production)
    try:
        _VERIFIED.add(connection)
    except TypeError:
        pass


def connect_test_database(**kwargs):
    """
    The only sanctioned way for a test helper or debug script to connect:
    TEST_DATABASE_URL after every static check, then the live check. Never
    DATABASE_URL, never Settings.from_environment().
    """
    import psycopg

    target = check_static()
    connection = psycopg.connect(os.environ[TEST_URL_ENV].strip(), **kwargs)
    try:
        check_live(connection, target)
        assert_isolated(connection)
    except Exception:
        connection.close()
        raise
    return connection


# --- process-wide guards -------------------------------------------------------

class ConnectGuard:
    """
    Every psycopg connection in the process must go to the approved target and
    pass the live check. With no approved target, every connection is refused.
    """

    def __init__(self, approved: Target | None, refusal: str = ""):
        self.approved = approved
        self.refusal = refusal
        self.opened: list[str] = []
        self.refused: list[str] = []
        self._original = None

    def install(self) -> None:
        import psycopg

        if self._original is not None:
            return
        original = psycopg.Connection.connect.__func__
        guard = self

        def guarded(cls, conninfo: str = "", **kwargs):
            try:
                target = parse_target(conninfo, **kwargs)
            except UnsafeTestDatabase:
                guard.refused.append("<unparseable>")
                raise
            if guard.approved is None:
                guard.refused.append(target.describe() if target.dbname else "<unknown>")
                raise UnsafeTestDatabase(
                    "database connections are disabled in this test session: "
                    + (guard.refusal or "no approved test database"))
            if (target.server != guard.approved.server
                    or target.dbname != guard.approved.dbname):
                guard.refused.append("<non-test target>")
                raise UnsafeTestDatabase(
                    "refused a connection to a database other than the approved test "
                    f"database {guard.approved.describe()}")
            connection = original(cls, conninfo, **kwargs)
            try:
                check_live(connection, guard.approved)
            except Exception:
                connection.close()
                raise
            guard.opened.append(target.describe())
            return connection

        self._original = original
        psycopg.Connection.connect = classmethod(guarded)
        psycopg.connect = psycopg.Connection.connect

    def uninstall(self) -> None:
        import psycopg

        if self._original is None:
            return
        psycopg.Connection.connect = classmethod(self._original)
        psycopg.connect = psycopg.Connection.connect
        self._original = None


class NetworkGuard:
    """
    Refuses every non-loopback socket connection and DNS lookup, so an
    accidental Graph, OpenAI, n8n or SharePoint call fails loudly instead of
    leaving the machine. libpq opens its own sockets in C, so the approved
    loopback test database is unaffected.
    """

    def __init__(self):
        self.blocked: list[str] = []
        self._saved = None

    @staticmethod
    def _is_loopback(host) -> bool:
        host = (host or "").strip().lower().strip("[]")
        return host in LOOPBACK_HOSTS or host.startswith("127.")

    def install(self) -> None:
        if self._saved is not None:
            return
        guard = self
        saved = (socket.socket.connect, socket.socket.connect_ex,
                 socket.getaddrinfo, socket.create_connection)
        self._saved = saved

        def _check(address):
            host = address[0] if isinstance(address, tuple) else None
            if host is not None and not guard._is_loopback(str(host)):
                guard.blocked.append(str(host))
                raise ConnectionRefusedError(
                    f"external network access is blocked in tests (host {host!r})")

        def connect(sock, address):
            _check(address)
            return saved[0](sock, address)

        def connect_ex(sock, address):
            _check(address)
            return saved[1](sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if host is not None and not guard._is_loopback(str(host)):
                guard.blocked.append(str(host))
                raise socket.gaierror(
                    f"external DNS lookup is blocked in tests (host {host!r})")
            return saved[2](host, *args, **kwargs)

        def create_connection(address, *args, **kwargs):
            _check(address)
            return saved[3](address, *args, **kwargs)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = getaddrinfo
        socket.create_connection = create_connection

    def uninstall(self) -> None:
        if self._saved is None:
            return
        (socket.socket.connect, socket.socket.connect_ex,
         socket.getaddrinfo, socket.create_connection) = self._saved
        self._saved = None


def scrub_environment(environ=None) -> list[str]:
    """
    Blank every secret / endpoint variable for this process - including those
    only present in backend/.env - so `load_dotenv(override=False)` finds them
    already set and cannot reload production values. Returns the names only.
    """
    environ = os.environ if environ is None else environ
    names = {key for key in environ if SECRET_NAME.search(key)}
    for path in PRODUCTION_ENV_FILES:
        if path.is_file():
            names.update(key for key in dotenv_values(path) if SECRET_NAME.search(key))
    names.discard(TEST_URL_ENV)
    for key in names:
        environ[key] = ""
    return sorted(names)
