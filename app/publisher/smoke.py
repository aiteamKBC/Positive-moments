"""
Database smoke test for the publisher's own connection (PUBLISHER_DATABASE_URL).

    python -m app.cli.publisher --env-file F db-smoke

Proves, before the daemon is ever started, the two things the publisher's
locking depends on - and writes nothing:

  1. SESSION STABILITY. Twenty consecutive autocommit statements on one
     connection reach ONE server backend. Through Neon's transaction pooler
     they would not, and a session advisory lock would leak.
  2. SESSION ADVISORY LOCKS. A lock taken on connection A is still held by
     A's backend three statements later, is refused to a second connection B
     (cluster-wide exclusion), and is released by A's own unlock, after which
     B can take and release it.

Both sessions run with default_transaction_read_only=on, and a write probe
(CREATE TEMP TABLE) must be REFUSED, so no application data can be written.
The lock key is a dedicated smoke key in the publisher's task namespace; both
sessions call pg_advisory_unlock_all() (which only affects their own session)
before closing. Reports kinds, counts and booleans - never a host, user or URL.
"""
from __future__ import annotations

import zlib

from app.publisher.config import database_endpoint
from app.publisher.daemon import TASK_LOCK_NAMESPACE

SMOKE_KEY = zlib.crc32(b"PUBLISHER_DB_SMOKE_TEST") & 0x7FFFFFFF
PUBLISHER_TABLES = (
    "bunny_videos", "bunny_caption_publications", "bunny_caption_current",
    "bunny_lms_match_evaluations", "bunny_lms_occurrence_links", "bunny_lms_occurrence_coverage",
    "bunny_publisher_jobs", "bunny_publisher_events", "bunny_publisher_heartbeats",
    "bunny_recording_discoveries", "bunny_recording_sources",
    "bunny_publisher_part_decisions", "bunny_publisher_schedule", "bunny_publisher_alerts", "bunny_publisher_reconciliation_runs")
HELD = """
SELECT count(*) FROM pg_locks
 WHERE locktype = 'advisory' AND granted AND objsubid = 2
   AND classid = %s::bigint::oid AND objid = %s::bigint::oid AND pid = pg_backend_pid()
"""
ANY_HELD = """
SELECT count(*) FROM pg_locks
 WHERE locktype = 'advisory' AND granted AND objsubid = 2
   AND classid = %s::bigint::oid AND objid = %s::bigint::oid
"""


def _open(url):
    import psycopg
    connection = psycopg.connect(url, prepare_threshold=None, connect_timeout=15, autocommit=True,
                                 application_name="kbc_publisher_db_smoke",
                                 options="-c default_transaction_read_only=on -c statement_timeout=15000")
    return connection


def _write_refused(connection) -> bool:
    import psycopg
    try:
        connection.execute("CREATE TEMP TABLE _publisher_smoke_write_probe (x int)")
    except psycopg.errors.ReadOnlySqlTransaction:
        return True
    return False


def run_db_smoke(url: str, *, attempts: int = 20) -> dict:
    report: dict = {"database_endpoint": database_endpoint(url), "checks": {}}
    checks = report["checks"]
    if report["database_endpoint"] in ("missing", "neon-pooled"):
        report["ok"] = False
        report["refused"] = ("PUBLISHER_DATABASE_URL is not set" if report["database_endpoint"] == "missing"
                             else "PUBLISHER_DATABASE_URL is Neon's POOLED endpoint; use the DIRECT one")
        return report
    a = b = None
    try:
        a = _open(url)
        b = _open(url)
        checks["read_only_session"] = a.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        checks["write_probe_refused"] = _write_refused(a)
        pids = {a.execute("SELECT pg_backend_pid()").fetchone()[0] for _ in range(attempts)}
        checks["one_backend_per_session"] = len(pids) == 1
        report["distinct_backends_over_statements"] = {"statements": attempts, "backends": len(pids)}

        ns, key = TASK_LOCK_NAMESPACE, SMOKE_KEY
        checks["lock_free_before"] = a.execute(ANY_HELD, (ns, key)).fetchone()[0] == 0
        checks["a_acquires"] = bool(a.execute("SELECT pg_try_advisory_lock(%s, %s)", (ns, key)).fetchone()[0])
        checks["a_still_holds_after_3_statements"] = all(
            a.execute(HELD, (ns, key)).fetchone()[0] == 1 for _ in range(3))
        checks["b_is_refused_while_a_holds"] = not b.execute("SELECT pg_try_advisory_lock(%s, %s)",
                                                             (ns, key)).fetchone()[0]
        checks["a_releases_its_own_lock"] = bool(a.execute("SELECT pg_advisory_unlock(%s, %s)",
                                                           (ns, key)).fetchone()[0])
        checks["b_acquires_after_release"] = bool(b.execute("SELECT pg_try_advisory_lock(%s, %s)",
                                                            (ns, key)).fetchone()[0])
        checks["b_releases"] = bool(b.execute("SELECT pg_advisory_unlock(%s, %s)", (ns, key)).fetchone()[0])
        checks["lock_free_after"] = a.execute(ANY_HELD, (ns, key)).fetchone()[0] == 0

        report["server_version"] = a.execute("SHOW server_version").fetchone()[0]
        report["deadlock_timeout"] = a.execute("SHOW deadlock_timeout").fetchone()[0]
        present = {r[0] for r in a.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(PUBLISHER_TABLES),)).fetchall()}
        report["publisher_tables_present"] = f"{len(present)}/{len(PUBLISHER_TABLES)}"
        report["migrations_024_028_applied"] = len(present) == len(PUBLISHER_TABLES)
    except Exception as exc:  # noqa: BLE001 - reported by class only (a DETAIL can carry values)
        report["error"] = type(exc).__name__
    finally:
        for connection in (b, a):
            if connection is not None:
                try:
                    connection.execute("SELECT pg_advisory_unlock_all()")
                except Exception:  # noqa: BLE001
                    pass
                connection.close()
    report["ok"] = "error" not in report and bool(checks) and all(checks.values())
    return report
