"""
The QA call must not outlive the database transaction it runs in.

Production ends a session idle in a transaction after 5 minutes. On
2026-10-15 a gpt-5.2 call took longer, the server closed the backfill's
connection mid-call, and the paid answer was lost with the whole day.
"""
import pytest

from app.common.errors import DATABASE_ERROR, PlatformError
from app.orchestration.runner import (
    PROVIDER_IDLE_MARGIN_SECONDS,
    allow_idle_through_provider_call,
)
from app.qa.provider import OpenAIChatProvider


class RecordingConnection:
    def __init__(self, fail=False):
        self.statements = []
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("server closed the connection")
        self.statements.append((sql, params))


def test_the_grant_covers_every_try_timing_out_plus_the_waits():
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2")
    connection = RecordingConnection()
    seconds = allow_idle_through_provider_call(connection, provider)
    worst_case = provider.timeout * provider.max_tries + (
        provider.wait_between_tries_ms * (provider.max_tries - 1)) // 1000
    assert seconds == worst_case + PROVIDER_IDLE_MARGIN_SECONDS
    # Comfortably above production's 5-minute limit.
    assert seconds > 5 * 60
    ((sql, params),) = connection.statements
    assert "set_config('idle_in_transaction_session_timeout'" in sql
    # Transaction-local (SET LOCAL), never session-wide on a pooled backend.
    assert sql.rstrip().endswith("%s, true)")
    assert params == (f"{seconds}s",)


def test_it_follows_the_provider_s_own_timeout():
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2", timeout=600, max_tries=2)
    seconds = allow_idle_through_provider_call(RecordingConnection(), provider)
    assert seconds == 600 * 2 + 5 + PROVIDER_IDLE_MARGIN_SECONDS


def test_an_in_memory_run_has_no_session_to_extend():
    assert allow_idle_through_provider_call(None, object()) == 0


def test_a_broken_connection_is_a_database_error_before_any_money_is_spent():
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2")
    with pytest.raises(PlatformError) as raised:
        allow_idle_through_provider_call(RecordingConnection(fail=True), provider)
    assert raised.value.code == DATABASE_ERROR
