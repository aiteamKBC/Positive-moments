"""
Build the isolated integration-test database from the repository's migrations.

    APP_ENV=test TEST_DATABASE_URL=postgresql://.../kbc_qa_integration_test \
        python -m tools.bootstrap_integration_db [--reset] [--report out.json]

It refuses to touch anything that `tools/integration_db_guard.check_static`
does not accept, and it refuses a database that already holds tables unless
that database carries the test marker this tool writes. So it can never
migrate, reset or mark a production database.

Order applied:
  1. tests/integration/fixtures/legacy_baseline.sql   (test-only legacy tables)
  2. automation migrations 050, 051, 100, 101        (they ALTER legacy tables)
  3. app/db/migrations/*.sql                          (001 .. latest, in order)
  4. the kbc_test_database_marker row
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from tools.integration_db_guard import (
    MARKER_PURPOSE,
    MARKER_TABLE,
    REPO_ROOT,
    TEST_URL_ENV,
    UnsafeTestDatabase,
    check_static,
)


LEGACY_BASELINE = REPO_ROOT / "tests" / "integration" / "fixtures" / "legacy_baseline.sql"
AUTOMATION_MIGRATIONS = (
    REPO_ROOT / "automation" / "positive_clips" / "sql" / "migrations" / "050_add_queue_priority.sql",
    REPO_ROOT / "automation" / "positive_clips" / "sql" / "migrations" / "051_add_clips_media_origin.sql",
    REPO_ROOT / "automation" / "lecture_parts" / "sql" / "migrations" / "100_create_lecture_split_tables.sql",
    REPO_ROOT / "automation" / "lecture_parts" / "sql" / "migrations" / "101_add_recording_duration_seconds.sql",
)
APP_MIGRATIONS = REPO_ROOT / "app" / "db" / "migrations"

OBJECT_COUNTS = """
SELECT
  (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'),
  (SELECT count(*) FROM pg_views WHERE schemaname = 'public'),
  (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'),
  (SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE n.nspname = 'public'),
  (SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid
     JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE n.nspname = 'public' AND NOT t.tgisinternal),
  (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public')
"""
OBJECT_KINDS = ("tables", "views", "indexes", "constraints", "triggers", "functions")


def _marker_present(connection) -> bool:
    return connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{MARKER_TABLE}",)).fetchone()[0]


def _public_table_count(connection) -> int:
    return connection.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()[0]


def bootstrap(url: str, *, reset: bool) -> dict:
    target = check_static()
    report = {"target": target.describe(), "steps": []}
    with psycopg.connect(url, autocommit=True) as connection:
        if connection.execute("SELECT current_database()").fetchone()[0] != target.dbname:
            raise UnsafeTestDatabase("connected database is not the named test database")
        report["server_version"] = connection.execute("SHOW server_version").fetchone()[0]
        tables = _public_table_count(connection)
        marked = _marker_present(connection)
        if tables and not marked:
            raise UnsafeTestDatabase(
                f"{target.describe()} already holds {tables} tables and no {MARKER_TABLE}; "
                "refusing to migrate or reset a database this tool did not build")
        if reset and tables:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            report["reset"] = True
        elif tables:
            raise UnsafeTestDatabase(
                f"{target.describe()} is already built; pass --reset to rebuild it")

        files = [("legacy_baseline", LEGACY_BASELINE)]
        files += [("automation", path) for path in AUTOMATION_MIGRATIONS]
        files += [("app", path) for path in sorted(APP_MIGRATIONS.glob("*.sql"))]
        for kind, path in files:
            step = {"kind": kind, "file": path.relative_to(REPO_ROOT).as_posix()}
            try:
                connection.execute(path.read_text(encoding="utf-8"))
                step["status"] = "APPLIED"
            except Exception as exc:
                step["status"] = "FAILED"
                step["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                report["steps"].append(step)
                report["result"] = "FAILED"
                return report
            report["steps"].append(step)

        connection.execute(f"""
            CREATE TABLE public.{MARKER_TABLE} (
                purpose     text PRIMARY KEY,
                built_at    timestamptz NOT NULL DEFAULT now(),
                note        text NOT NULL
            )""")
        connection.execute(
            f"INSERT INTO public.{MARKER_TABLE} (purpose, note) VALUES (%s, %s)",
            (MARKER_PURPOSE, "Disposable integration-test database. Never production."))
        counts = connection.execute(OBJECT_COUNTS).fetchone()
        report["schema_objects"] = dict(zip(OBJECT_KINDS, counts))
        report["app_migrations_applied"] = sum(
            1 for step in report["steps"] if step["kind"] == "app")
        report["result"] = "OK"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--reset", action="store_true",
                        help="drop and rebuild a database this tool built before")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)
    import os
    url = (os.environ.get(TEST_URL_ENV) or "").strip()
    try:
        report = bootstrap(url, reset=args.reset)
    except UnsafeTestDatabase as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, default=str)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report.get("result") == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
