"""Shared helpers for the P0-A publisher-foundation integration tests (isolated test DB only)."""
from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

from tests.integration.seeding import insert
from tools.integration_db_guard import assert_isolated

PROPOSED = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations_proposed"
MIGRATIONS = [PROPOSED / "024_create_bunny_stream_tables.sql",
              PROPOSED / "025_create_lms_matching_tables.sql",
              PROPOSED / "026_create_publisher_queue_tables.sql"]
ROLLBACKS = [PROPOSED / "026_create_publisher_queue_tables.rollback.sql",
             PROPOSED / "025_create_lms_matching_tables.rollback.sql",
             PROPOSED / "024_create_bunny_stream_tables.rollback.sql"]
# P0-C's 027 is independent of 024-026 and is applied by its own module only.
RECORDING_MIGRATION = PROPOSED / "027_create_recording_source_tables.sql"
RECORDING_ROLLBACK = PROPOSED / "027_create_recording_source_tables.rollback.sql"
RECORDING_TABLES = ("bunny_recording_discoveries", "bunny_recording_sources")
# P0-D's 028 depends on 024-027 and is removed first.
RUNTIME_MIGRATION = PROPOSED / "028_create_publisher_runtime_tables.sql"
RUNTIME_ROLLBACK = PROPOSED / "028_create_publisher_runtime_tables.rollback.sql"
RUNTIME_TABLES = ("bunny_publisher_part_decisions", "bunny_publisher_schedule", "bunny_publisher_alerts",
                  "bunny_publisher_reconciliation_runs")
# Historical Backfill's 029 depends only on lecture_sessions; removed before everything else.
BACKFILL_MIGRATION = PROPOSED / "029_create_publisher_backfill_tables.sql"
BACKFILL_ROLLBACK = PROPOSED / "029_create_publisher_backfill_tables.rollback.sql"
BACKFILL_TABLES = ("bunny_backfill_items", "bunny_backfill_runs")
NEW_TABLES = ("bunny_videos", "bunny_caption_publications", "bunny_caption_current",
              "bunny_lms_match_evaluations", "bunny_lms_occurrence_links", "bunny_lms_occurrence_coverage",
              "bunny_publisher_jobs", "bunny_publisher_events", "bunny_publisher_heartbeats")
MARKER_UPN = "p0a-foundation@example.invalid"


def body(path: Path) -> str:
    """A migration without its own BEGIN/COMMIT, to run inside a test transaction."""
    return re.sub(r"^\s*(BEGIN|COMMIT);\s*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)


def lecture_row(connection, start) -> str:
    """One lecture_sessions row - the only LI table the matcher's FKs need."""
    assert_isolated(connection)
    lecture_id = uuid.uuid4()
    insert(connection, "lecture_sessions", lecture_id=lecture_id, calendar_user_upn=MARKER_UPN,
           calendar_event_id=f"evt-{lecture_id}", join_url=f"https://example.invalid/{lecture_id}",
           subject="Synthetic P0-A Lecture", normalized_subject="synthetic p0-a lecture",
           module="Synthetic P0-A Lecture", scheduled_start=start, scheduled_end=start + timedelta(hours=2),
           session_date=start.date(), calendar_mapping_status="RESOLVED", group_match_status="MATCHED",
           discovery_status="READY", meeting_id=f"MTG-{lecture_id.hex[:12]}", downstream_ready=True,
           meeting_lookup_user_id="organizer", meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
    return str(lecture_id)


def applied(connection) -> list[str]:
    return [t for t in NEW_TABLES
            if connection.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0] is not None]


# Children before parents, for emptying whatever a killed run left behind.
_DELETE_ORDER = ("bunny_publisher_events", "bunny_publisher_jobs", "bunny_publisher_heartbeats", "bunny_lms_occurrence_coverage",
                 "bunny_lms_occurrence_links", "bunny_lms_match_evaluations", "bunny_caption_current",
                 "bunny_caption_publications", "bunny_videos")


def backfill_applied(connection) -> list[str]:
    return [t for t in BACKFILL_TABLES
            if connection.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0] is not None]


def remove_backfill_migration(admin) -> list[str]:
    """Empty and roll back 029 on the isolated test database (its rollback refuses while runs exist)."""
    assert_isolated(admin)
    found = backfill_applied(admin)
    if found:
        for table in BACKFILL_TABLES:
            if table in found:
                admin.execute(f"DELETE FROM public.{table}")
        admin.execute(BACKFILL_ROLLBACK.read_text(encoding="utf-8"))
        assert backfill_applied(admin) == []
    return found


def runtime_applied(connection) -> list[str]:
    return [t for t in RUNTIME_TABLES
            if connection.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0] is not None]


def recording_applied(connection) -> list[str]:
    return [t for t in RECORDING_TABLES
            if connection.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0] is not None]


def reset_proposed_migrations(url: str) -> list[str]:
    """
    Remove proposed migrations 024-028 a killed run left COMMITTED on the
    isolated test database: empty their tables, then run their own rollback
    scripts. Returns the tables it found. Refuses anything but the marked
    test database. Modules that apply proposed migrations call this first, so
    one interrupted run cannot fail the next.
    """
    import psycopg
    with psycopg.connect(url, autocommit=True) as admin:
        assert_isolated(admin)
        backfill = remove_backfill_migration(admin)
        runtime = runtime_applied(admin)
        if runtime:
            for table in RUNTIME_TABLES:
                if table in runtime:
                    admin.execute(f"DELETE FROM public.{table}")
            admin.execute(RUNTIME_ROLLBACK.read_text(encoding="utf-8"))
            assert runtime_applied(admin) == []
        recording = recording_applied(admin)
        if recording:
            for table in ("bunny_recording_sources", "bunny_recording_discoveries"):
                if table in recording:
                    admin.execute(f"DELETE FROM public.{table}")
            admin.execute(RECORDING_ROLLBACK.read_text(encoding="utf-8"))
            assert recording_applied(admin) == []
        found = applied(admin)
        if found:
            for table in _DELETE_ORDER:
                if table in found:
                    admin.execute(f"DELETE FROM public.{table}")
            for path in ROLLBACKS:
                admin.execute(path.read_text(encoding="utf-8"))
            assert applied(admin) == []
        return backfill + runtime + recording + found
