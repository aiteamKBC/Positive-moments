"""
Verify, READ-ONLY, that a Neon restore branch of production exists and holds production's current data.

    python tools/publisher_verify_restore_point.py --pilot-env backend/config/publisher.pilot-2026-10-22.env \
        --restore-env backend/config/publisher.restore-point.env

PASS requires, on two default_transaction_read_only=on sessions:
  * both URLs are Neon DIRECT endpoints and differ;
  * same neon.tenant_id (same project), different neon.timeline_id and neon.endpoint_id (a separate branch);
  * the same current_database();
  * identical row counts in every table the pilot may touch (so the branch was taken from production's
    current head, before any pilot write), and none of migrations 024-028's objects on either side.
Prints booleans and counts only - never a URL, host or id value.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.publisher.config import database_endpoint  # noqa: E402
from tools.publisher_pilot_audit import EXISTING  # noqa: E402
from tools.publisher_replay_branch import _identity, _objects  # noqa: E402


def _facts(url: str) -> dict:
    names = _objects("table") + _objects("view") + _objects("index")
    with psycopg.connect(url, prepare_threshold=None, autocommit=True, connect_timeout=15,
                         application_name="kbc_publisher_restore_check",
                         options="-c default_transaction_read_only=on -c statement_timeout=60000") as c:
        present = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                           "AND tablename = ANY(%s)", (list(EXISTING),)).fetchall()}
        return {"identity": _identity(c), "read_only": c.execute("SHOW transaction_read_only").fetchone()[0] == "on",
                "database": c.execute("SELECT current_database()").fetchone()[0],
                "counts": {t: c.execute(f"SELECT count(*) FROM public.{t}").fetchone()[0] for t in sorted(present)},
                "proposed_objects": c.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s)",
                                              (names,)).fetchone()[0]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/publisher_verify_restore_point.py")
    parser.add_argument("--pilot-env", required=True)
    parser.add_argument("--restore-env", required=True)
    args = parser.parse_args(argv)
    prod_url = (dotenv_values(args.pilot_env).get("PUBLISHER_DATABASE_URL") or "").strip()
    restore_url = (dotenv_values(args.restore_env).get("PUBLISHER_DATABASE_URL") or "").strip() \
        if Path(args.restore_env).exists() else ""
    report: dict = {"endpoints": {"production": database_endpoint(prod_url), "restore_branch": database_endpoint(restore_url)}}
    if report["endpoints"] != {"production": "neon-direct", "restore_branch": "neon-direct"} or prod_url == restore_url:
        report["verdict"] = "FAIL: both must be distinct Neon DIRECT URLs (restore URL missing or pooled?)"
        print(json.dumps(report, indent=2))
        return 2
    try:
        prod, restore = _facts(prod_url), _facts(restore_url)
    except psycopg.Error as exc:
        report["verdict"] = f"FAIL: {type(exc).__name__}"
        print(json.dumps(report, indent=2))
        return 2
    p, r = prod["identity"], restore["identity"]
    checks = {
        "both_sessions_read_only": prod["read_only"] and restore["read_only"],
        "same_project": bool(p["tenant"]) and p["tenant"] == r["tenant"],
        "separate_branch": bool(p["timeline"]) and p["timeline"] != r["timeline"],
        "separate_endpoint": p["endpoint"] != r["endpoint"],
        "same_database_name": prod["database"] == restore["database"],
        "identical_row_counts": prod["counts"] == restore["counts"] and bool(prod["counts"]),
        "pre_migration_on_both": prod["proposed_objects"] == 0 and restore["proposed_objects"] == 0,
    }
    report.update(checks=checks, row_counts={"production": prod["counts"], "restore_branch": restore["counts"]})
    ok = all(checks.values())
    report["verdict"] = "PASS: a recoverable restore branch of production's current state exists" if ok else "FAIL"
    print(json.dumps(report, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
