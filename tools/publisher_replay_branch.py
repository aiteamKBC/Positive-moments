"""
Prepare a Neon BRANCH for the publisher replay - never production.

    python tools/publisher_replay_branch.py check              read-only, both databases
    python tools/publisher_replay_branch.py migrate            the same checks, then a dry-run plan
    python tools/publisher_replay_branch.py migrate --apply    apply 024 -> 028 to the BRANCH only

Two env files, never mixed:
    backend/config/publisher.env         production DIRECT URL (read here, read-only, never written to)
    backend/config/publisher.replay.env  the branch's DIRECT URL (the only database this tool may change)

BRANCH IDENTITY (checked before anything is applied; any failure aborts):
    * both URLs are Neon DIRECT endpoints (no "-pooler");
    * same neon.tenant_id      - the branch belongs to the production project;
    * different neon.timeline_id and neon.endpoint_id - it is a DIFFERENT branch;
    * none of the 46 objects 024-028 create exists on the branch yet;
    * every table, column and key the migrations depend on exists on the branch.
The production connection is opened with default_transaction_read_only=on.
Prints kinds, counts and booleans only - never a host, user, id value or URL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.publisher.config import database_endpoint  # noqa: E402

PROD_ENV = ROOT / "backend" / "config" / "publisher.env"
REPLAY_ENV = ROOT / "backend" / "config" / "publisher.replay.env"
PROPOSED = ROOT / "app" / "db" / "migrations_proposed"
ORDER = ("024_create_bunny_stream_tables", "025_create_lms_matching_tables", "026_create_publisher_queue_tables",
         "027_create_recording_source_tables", "028_create_publisher_runtime_tables")
REQUIRED_COLUMNS = {
    "lecture_sessions": ("lecture_id", "session_date", "scheduled_start", "scheduled_end", "subject", "is_cancelled",
                         "metadata", "join_url", "meeting_id", "normalized_subject", "module"),
    "lecture_transcript_artifacts": ("artifact_id", "content_version"),
    "lecture_transcript_selections": ("selection_id", "lecture_id", "selection_version", "selection_status",
                                      "updated_at"),
    "lecture_transcript_artifact_contents": ("artifact_id", "version_number"),
}
REQUIRED_KEYS = {"lecture_sessions": "lecture_id", "lecture_transcript_artifacts": "artifact_id",
                 "lecture_transcript_selections": "selection_id"}
EXPECTED_TABLES, EXPECTED_VIEWS = 15, 5


def _objects(kind: str) -> list[str]:
    pattern = {"table": r"CREATE TABLE (?:IF NOT EXISTS )?public\.([a-z0-9_]+)",
               "view": r"CREATE (?:OR REPLACE )?VIEW public\.([a-z0-9_]+)",
               "index": r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?([a-z0-9_]+)"}[kind]
    found: list[str] = []
    for name in ORDER:
        found += re.findall(pattern, (PROPOSED / f"{name}.sql").read_text(encoding="utf-8"))
    return sorted(set(found))


def _url(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"{path.relative_to(ROOT)} does not exist")
    return (dotenv_values(path).get("PUBLISHER_DATABASE_URL") or "").strip()


def _connect(url: str, *, read_only: bool):
    options = "-c statement_timeout=120000" + (" -c default_transaction_read_only=on" if read_only else "")
    return psycopg.connect(url, prepare_threshold=None, autocommit=True, connect_timeout=15,
                           application_name="kbc_publisher_replay_setup", options=options)


def _identity(connection) -> dict:
    get = lambda name: connection.execute("SELECT current_setting(%s, true)", (name,)).fetchone()[0]  # noqa: E731
    return {"tenant": get("neon.tenant_id"), "timeline": get("neon.timeline_id"), "endpoint": get("neon.endpoint_id")}


def _present(connection, names) -> set[str]:
    rows = connection.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                              "WHERE n.nspname = 'public' AND c.relname = ANY(%s)", (list(names),)).fetchall()
    return {r[0] for r in rows}


def _facts(connection) -> dict:
    proposed = _objects("table") + _objects("view") + _objects("index")
    return {
        "read_only": connection.execute("SHOW transaction_read_only").fetchone()[0] == "on",
        "public_tables": connection.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()[0],
        "lecture_sessions": connection.execute("SELECT count(*) FROM public.lecture_sessions").fetchone()[0],
        "lectures_2026_10_21": connection.execute(
            "SELECT count(*) FROM public.lecture_sessions WHERE session_date = '2026-10-21'").fetchone()[0],
        "proposed_objects_present": len(_present(connection, proposed)),
        "proposed_objects_total": len(proposed),
    }


def _dependencies(connection) -> dict:
    missing_columns = {}
    for table, columns in REQUIRED_COLUMNS.items():
        have = {r[0] for r in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
            (table,)).fetchall()}
        gap = [c for c in columns if c not in have]
        if gap:
            missing_columns[table] = gap
    missing_keys = []
    for table, column in REQUIRED_KEYS.items():
        ok = connection.execute("""
            SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                             JOIN information_schema.key_column_usage k
                               ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema
                            WHERE tc.table_schema = 'public' AND tc.table_name = %s
                              AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') AND k.column_name = %s)""",
                                (table, column)).fetchone()[0]
        if not ok:
            missing_keys.append(f"{table}({column})")
    return {"missing_columns": missing_columns, "missing_keys": missing_keys}


def check() -> tuple[dict, bool, str]:
    prod_url, replay_url = _url(PROD_ENV), _url(REPLAY_ENV)
    report: dict = {"endpoints": {"production": database_endpoint(prod_url), "replay": database_endpoint(replay_url)}}
    if report["endpoints"] != {"production": "neon-direct", "replay": "neon-direct"}:
        report["abort"] = "both PUBLISHER_DATABASE_URL values must be Neon DIRECT endpoints (and the replay one set)"
        return report, False, replay_url
    if prod_url == replay_url:
        report["abort"] = "the replay URL is the production URL"
        return report, False, replay_url
    with _connect(prod_url, read_only=True) as prod, _connect(replay_url, read_only=True) as replay:
        p_id, r_id = _identity(prod), _identity(replay)
        identity = {
            "identity_settings_available": all(p_id.values()) and all(r_id.values()),
            "same_project (tenant)": p_id["tenant"] == r_id["tenant"],
            "different_branch (timeline)": p_id["timeline"] != r_id["timeline"],
            "different_endpoint": p_id["endpoint"] != r_id["endpoint"],
        }
        report["identity"] = identity
        report["production"] = _facts(prod)
        report["replay_branch"] = _facts(replay)
        report["replay_branch_dependencies"] = _dependencies(replay)
    deps = report["replay_branch_dependencies"]
    ok = (all(identity.values())
          and report["production"]["read_only"]
          and report["replay_branch"]["proposed_objects_present"] == 0
          and not deps["missing_columns"] and not deps["missing_keys"])
    if not ok:
        report["abort"] = "identity, isolation or dependency check failed - nothing applied"
    return report, ok, replay_url


def migrate(apply: bool) -> tuple[dict, bool]:
    report, ok, replay_url = check()
    report["order"] = list(ORDER)
    if not ok:
        return report, False
    if not apply:
        report["plan"] = "DRY RUN: all checks pass; rerun with --apply to apply 024-028 to the BRANCH only"
        return report, True
    applied = []
    with _connect(_url(PROD_ENV), read_only=True) as prod:
        production = _identity(prod)
    with _connect(replay_url, read_only=False) as branch:
        branch_id = _identity(branch)
        # Re-verified on the very connection that will run the DDL.
        if branch_id["timeline"] == production["timeline"] or branch_id["endpoint"] == production["endpoint"] \
                or branch_id["tenant"] != production["tenant"]:
            report["abort"] = "the write connection is not a branch of the production project - nothing applied"
            return report, False
        prod_timeline_check = branch_id["timeline"]
        for name in ORDER:
            branch.execute((PROPOSED / f"{name}.sql").read_text(encoding="utf-8"))   # each file is BEGIN ... COMMIT
            applied.append(name)
        tables, views = _objects("table"), _objects("view")
        present_tables = _present(branch, tables)
        present_views = {r[0] for r in branch.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND viewname = ANY(%s)", (views,)).fetchall()}
        empty = all(branch.execute(f"SELECT NOT EXISTS (SELECT 1 FROM public.{t})").fetchone()[0] for t in tables)
        branch_still_branch = _identity(branch)["timeline"] == prod_timeline_check
    report["applied"] = applied
    report["verify"] = {"tables": f"{len(present_tables)}/{EXPECTED_TABLES}", "views": f"{len(present_views)}/{EXPECTED_VIEWS}",
                        "new_tables_empty": empty, "same_branch_throughout": branch_still_branch}
    with _connect(_url(PROD_ENV), read_only=True) as prod:      # production must be exactly as before
        after = _facts(prod)
    report["production_after"] = after
    report["production_unchanged"] = (after["proposed_objects_present"] == 0
                                      and after["public_tables"] == report["production"]["public_tables"])
    done = (len(present_tables) == EXPECTED_TABLES and len(present_views) == EXPECTED_VIEWS and empty
            and branch_still_branch and report["production_unchanged"])
    return report, done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/publisher_replay_branch.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    m = sub.add_parser("migrate")
    m.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report, ok = (check()[:2] if args.command == "check" else migrate(args.apply))
    except psycopg.Error as exc:
        report, ok = {"error": type(exc).__name__}, False          # class only: a DETAIL can carry values
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
