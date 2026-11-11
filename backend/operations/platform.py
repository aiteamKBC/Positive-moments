"""
Phase 5A: the bridge between Django and the coded platform.

WHY THIS FILE IS SO SMALL
-------------------------
Every pipeline rule already has exactly one home: `app.orchestration`. The
Operations API's job is to expose those answers over HTTP, not to re-derive
any of them. So this module opens a connection, hands it to the existing
`OperationsService`, and gets out of the way.

The temptation this file exists to resist is writing "just one" query in a
view - the day's lecture count, say, or whether a lecture looks finished.
Every such query is a second implementation of a rule that already exists, and
the second implementation is the one that drifts. If the Operations API needs
an answer the platform does not already give, the answer belongs in
`app.orchestration`, with the tests that live there.

READ-ONLY IS ENFORCED BY POSTGRESQL, NOT BY INTENTION
-----------------------------------------------------
Every read endpoint runs inside `SET TRANSACTION READ ONLY`. A view that
somehow reached a writing code path would be refused by the database rather
than by a convention somebody could forget. That is the whole reason the read
and write helpers are two separate functions with two different names.

The Django ORM connection is NOT used for platform data. Django owns its own
auth tables; the lecture platform owns its schema through psycopg, and mixing
the two would put pipeline reads behind Django's transaction handling for no
benefit.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg

from app.config.settings import Settings
from app.db.connection import database_connection
from app.orchestration.factory import build_operations


def settings() -> Settings:
    return Settings.from_environment()


# --- read connections are reused ---------------------------------------------
#
# The database is remote, so opening a connection (TLS + auth) costs several
# network round trips - more than most of the queries a page then runs. Read
# connections are therefore kept for a short while and handed to the next
# request. Every one is still read-only (`read_only` makes psycopg open each
# transaction with BEGIN READ ONLY), and every request still gets its own
# transaction, rolled back at the end, exactly as before.
#
# Statements are never server-prepared: the database sits behind a pooler, and
# a prepared statement made on one backend does not exist on the next.

READ_POOL_SIZE = 12
READ_IDLE_SECONDS = 60
_idle: list[tuple[float, psycopg.Connection]] = []
_idle_lock = threading.Lock()


def _usable(connection) -> bool:
    return not connection.closed and not connection.broken


def _checkout(database_url) -> psycopg.Connection:
    now = time.monotonic()
    while True:
        with _idle_lock:
            if not _idle:
                break
            parked_at, connection = _idle.pop()
        if now - parked_at < READ_IDLE_SECONDS and _usable(connection):
            return connection
        connection.close()
    connection = psycopg.connect(database_url, prepare_threshold=None)
    connection.read_only = True
    return connection


def _checkin(connection) -> None:
    try:
        connection.rollback()
    except psycopg.Error:
        connection.close()
        return
    if not _usable(connection):
        return
    with _idle_lock:
        if len(_idle) < READ_POOL_SIZE:
            _idle.append((time.monotonic(), connection))
            return
    connection.close()


@contextmanager
def reading():
    """
    A connection PostgreSQL itself will not let anybody write through.

    Used by every GET. Nothing in the read path calls Microsoft Graph, calls a
    model provider, runs a scheduler cycle or touches a legacy production
    table - and this is the backstop that makes that true rather than
    intended.
    """
    config = settings()
    config.require_database()
    connection = _checkout(config.database_url)
    try:
        yield connection
    finally:
        _checkin(connection)


# --- the day report, one lecture per thread ---------------------------------
#
# Resolving a lecture is ~20 small sequential queries, and a day is resolved
# one lecture after another, so a day's cost is (lectures x 20) round trips.
# Each lecture's answer depends on that lecture's rows only, so the lectures
# are resolved side by side, each on its own read connection, with the same
# resolver and the same day summary - the report is identical, just sooner.

_day_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="day-report")


def day_report(session_date) -> dict:
    """What `OperationsService.day_reconciliation` returns, resolved in parallel."""
    reconciliation = operations().reconciliation
    resolver = reconciliation.resolver
    with reading() as connection:
        lecture_ids = resolver.lecture_ids_for_day(connection, session_date)

    def resolve(lecture_id):
        with reading() as connection:
            return resolver.for_lecture(connection, lecture_id)

    states = list(_day_workers.map(resolve, lecture_ids))
    return reconciliation.from_states(session_date, states)


@contextmanager
def writing():
    """
    A writable connection, for the guarded actions only.

    Deliberately a different function with a different name, so that reaching
    write capability is a visible decision in the diff rather than a default
    that leaked.
    """
    config = settings()
    config.require_database()
    with database_connection(config.database_url) as connection:
        yield connection


def operations():
    """
    The existing Operations facade. No caching: it holds no state worth reusing.

    Built in the configured RECORDING_LINK_MODE, the same switch the scheduler,
    the backfill runner and the guarded actions read - so the console never
    describes a recording stage the pipeline is not actually running.
    """
    return build_operations(recording_link_mode=settings().recording_link_mode)
