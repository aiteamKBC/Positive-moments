"""
Apply proposed migrations 024-028 to the PRODUCTION Lecture Intelligence database - guarded.

    python tools/publisher_apply_migrations.py --env-file backend/config/publisher.pilot-2026-10-22.env \
        --expect-database AiTeamKBC --before-audit output/publisher_pilot/audit-before-<ts>.json            (dry run)
    ... the same ... --apply                                                                               (writes)

(psql is not installed on the operator workstation; this is the same SQL, file by file.)

Refuses unless ALL hold:
  * the env file's PUBLISHER_DATABASE_URL is a Neon DIRECT endpoint;
  * current_database() equals --expect-database (typed by the operator);
  * --before-audit is a `before` snapshot of THIS database taken less than 2 hours ago (its restore target
    - server time + WAL LSN - is what a Neon point-in-time restore would use);
  * none of the 46 objects 024-028 create exists; every dependency (tables, columns, keys) exists.
Each file is one BEGIN ... COMMIT and is purely additive. After applying: 15 tables + 5 views present,
every new table empty. Prints kinds, counts and booleans only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.publisher.config import PublisherConfig, database_endpoint  # noqa: E402
from tools.publisher_replay_branch import ORDER, PROPOSED, _dependencies, _objects  # noqa: E402


def _connect(url: str, *, read_only: bool):
    options = "-c statement_timeout=120000 -c lock_timeout=10000" + (
        " -c default_transaction_read_only=on" if read_only else "")
    return psycopg.connect(url, prepare_threshold=None, autocommit=True, connect_timeout=15,
                           application_name="kbc_publisher_migrations", options=options)


def preflight(config, expect_database: str, before_audit: str) -> tuple[dict, bool]:
    report: dict = {"database_endpoint": database_endpoint(config.database_url)}
    problems = []
    if report["database_endpoint"] != "neon-direct":
        problems.append("PUBLISHER_DATABASE_URL is not a Neon DIRECT endpoint")
    audit_path = Path(before_audit)
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else None
    if audit is None or audit.get("label") != "before" or not (audit.get("restore_target") or {}).get("wal_lsn"):
        problems.append("--before-audit must be a `before` snapshot carrying a restore target")
    elif datetime.now(timezone.utc) - datetime.fromisoformat(audit["taken_at"]) > timedelta(hours=2):
        problems.append("the before snapshot is older than 2 hours: take a fresh one")
    if problems:
        return {**report, "problems": problems}, False
    names = _objects("table") + _objects("view") + _objects("index")
    with _connect(config.database_url, read_only=True) as c:
        database = c.execute("SELECT current_database()").fetchone()[0]
        present = c.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s)", (names,)).fetchone()[0]
        deps = _dependencies(c)
        lecture_count = c.execute("SELECT count(*) FROM public.lecture_sessions").fetchone()[0]
    report.update(database_matches=database == expect_database, objects_already_present=present,
                  dependencies=deps, audit_lecture_count_matches=lecture_count == audit["table_counts"]["lecture_sessions"],
                  restore_target=audit["restore_target"], order=list(ORDER))
    if database != expect_database:
        problems.append("current_database() does not match --expect-database")
    if present:
        problems.append(f"{present} of the objects already exist")
    if deps["missing_columns"] or deps["missing_keys"]:
        problems.append("a dependency is missing")
    if not report["audit_lecture_count_matches"]:
        problems.append("lecture_sessions changed since the before snapshot: take a fresh one")
    report["problems"] = problems
    return report, not problems


def apply(config) -> dict:
    tables, views = _objects("table"), _objects("view")
    applied = []
    with _connect(config.database_url, read_only=False) as c:
        for name in ORDER:
            c.execute((PROPOSED / f"{name}.sql").read_text(encoding="utf-8"))      # BEGIN ... COMMIT
            applied.append(name)
        have_tables = c.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
                                (tables,)).fetchone()[0]
        have_views = c.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'public' AND viewname = ANY(%s)",
                               (views,)).fetchone()[0]
        empty = all(c.execute(f"SELECT NOT EXISTS (SELECT 1 FROM public.{t})").fetchone()[0] for t in tables)
    return {"applied": applied, "tables": f"{have_tables}/{len(tables)}", "views": f"{have_views}/{len(views)}",
            "new_tables_empty": empty,
            "ok": have_tables == len(tables) and have_views == len(views) and empty}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/publisher_apply_migrations.py")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expect-database", required=True)
    parser.add_argument("--before-audit", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    config = PublisherConfig.from_environment(args.env_file)
    try:
        report, ok = preflight(config, args.expect_database, args.before_audit)
        if ok and args.apply:
            report["result"] = apply(config)
            ok = report["result"]["ok"]
        elif ok:
            report["plan"] = "DRY RUN: every check passes; rerun with --apply to create the 46 objects"
    except psycopg.Error as exc:
        report, ok = {"error": type(exc).__name__}, False
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
