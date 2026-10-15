"""
Against real PostgreSQL: the idle grant keeps a transaction alive past the
server's idle-in-transaction limit, and is gone once the transaction ends.

The limit is shrunk to 1 second here so the test reproduces the 2026-10-15
failure (a session killed while waiting for the model) in seconds, not minutes.
"""
from __future__ import annotations

import time

import psycopg
import pytest

from app.config.settings import Settings
from app.orchestration.runner import allow_idle_through_provider_call
from app.qa.provider import OpenAIChatProvider


@pytest.fixture
def url():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    return url


def _connect(url):
    connection = psycopg.connect(url, options="-c idle_in_transaction_session_timeout=1000")
    assert "test" in connection.execute("SELECT current_database()").fetchone()[0]
    return connection


def _wait_like_a_slow_model_call():
    time.sleep(2.5)


def test_without_the_grant_the_server_kills_the_waiting_transaction(url):
    connection = _connect(url)
    try:
        connection.execute("SELECT 1")          # opens the transaction
        _wait_like_a_slow_model_call()
        with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
            connection.execute("SELECT 1")
    finally:
        connection.close()


def test_with_the_grant_the_transaction_survives_the_wait(url):
    connection = _connect(url)
    try:
        connection.execute("SELECT 1")
        seconds = allow_idle_through_provider_call(
            connection, OpenAIChatProvider(api_key="k", model="gpt-5.2"))
        assert connection.execute(
            "SHOW idle_in_transaction_session_timeout").fetchone()[0] == f"{seconds}s"
        _wait_like_a_slow_model_call()
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        connection.rollback()
        # Transaction-local: the session's own limit is back afterwards.
        assert connection.execute(
            "SHOW idle_in_transaction_session_timeout").fetchone()[0] == "1s"
        connection.rollback()
    finally:
        connection.close()
