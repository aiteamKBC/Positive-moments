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
from contextlib import contextmanager

from app.config.settings import Settings
from app.db.connection import database_connection, readonly_database_connection
from app.orchestration.factory import build_operations


def settings() -> Settings:
    return Settings.from_environment()


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
    with readonly_database_connection(config.database_url) as connection:
        yield connection


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
