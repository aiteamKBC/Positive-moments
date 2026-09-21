#!/usr/bin/env python
"""
Controlled runner for the positive-clip media-job producer.

Composes automation/positive_clips/sql/010 + 011 and executes them against the
AiTeamKBC database using the Django backend's DATABASE_URL. The URL is read from
backend/.env and is never printed. The media worker itself never sees it.

  --preview                   show queueable clips, insert nothing (default)
  --insert                    insert the selected clips
  --mode live|history         persisted as qa_doctors_sessions.clips_media_origin;
                              live => priority 100, history => priority 10 (required for --insert)
  --allow-origin-override     deliberate admin override of a conflicting stored origin
  --limit N                   cap the number of clips
  --one-per-lecture           prefer breadth: at most one clip per lecture
  --clip-key K                restrict to explicit clip_key(s); repeatable
  --session-id S              restrict to explicit session_id(s); repeatable

In production this logic runs inside n8n (see n8n/ folder); this runner exists
for controlled/manual execution and testing.
"""
import argparse, json, sys
from pathlib import Path
from decimal import Decimal
from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[2]
SQL_DIR = Path(__file__).resolve().parent / "sql"
PRODUCER_DIR = SQL_DIR / "producer"

MODE_PRIORITY = {"live": 100, "history": 10}

# SharePoint destination for positive clips (IDs, not secrets).
DEST = json.loads((Path(__file__).resolve().parent / "destination.json").read_text())


def db_url():
    url = dotenv_values(REPO / "backend" / ".env").get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not found in backend/.env")
    return url


def build_sql(insert: bool) -> str:
    cte = (SQL_DIR / "010_positive_clip_candidates.sql").read_text()
    if not insert:
        return cte
    # Replace the CTE's trailing plain SELECT with the INSERT ... SELECT.
    marker = "SELECT * FROM candidates"
    assert marker in cte, "candidate CTE marker not found"
    return cte.replace(marker, "") + "\n" + (SQL_DIR / "011_insert_positive_clip_jobs.sql").read_text()


def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--insert", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--one-per-lecture", action="store_true")
    ap.add_argument("--clip-key", action="append", dest="clip_keys")
    ap.add_argument("--session-id", action="append", dest="session_ids")
    ap.add_argument("--mode", choices=sorted(MODE_PRIORITY), help="live (priority 100) or history (priority 10)")
    ap.add_argument("--allow-origin-override", action="store_true",
                    help="deliberately replace a conflicting stored origin")
    args = ap.parse_args()

    if args.insert:
        if not args.mode:
            ap.error("--insert requires --mode live|history")
        # Guard rail: an unscoped --insert would queue every eligible clip.
        if not args.clip_keys and not args.session_ids:
            ap.error("--insert must be scoped with --clip-key or --session-id")

    params = {
        "dest_drive_id": DEST["destination_drive_id"],
        "dest_folder_item_id": DEST["destination_folder_item_id"],
        "session_ids": args.session_ids or [],
        "clip_keys": args.clip_keys or [],
        "produced_by": "positive-clip-producer-v3",
    }

    with psycopg.connect(db_url(), connect_timeout=20, row_factory=dict_row) as conn:
        if args.insert:
            # Step 1: persist origin first, so a later failure is recoverable.
            targets = args.session_ids or []
            if not targets and args.clip_keys:
                with conn.cursor() as cur:
                    cur.execute("""select distinct split_part(k, ':', 1) sid
                                   from unnest(%s::text[]) k""", (args.clip_keys,))
                    targets = [r["sid"] for r in cur.fetchall()]
            origin_rows = assert_origin(conn, targets, args.mode, args.allow_origin_override)
            conflicts = [r for r in origin_rows if r["outcome"] in ("conflict", "not_found")]
            for r in origin_rows:
                print(f"origin: ...{r['session_id'][-18:]} requested={r['requested_mode']} "
                      f"previous={r['previous_origin']} -> {r['outcome']}")
            if conflicts:
                conn.rollback()
                print(json.dumps({"error": "origin_conflict", "inserted": 0,
                                  "conflicts": [{"session_id": r["session_id"],
                                                 "stored_origin": r["previous_origin"],
                                                 "requested_mode": r["requested_mode"],
                                                 "outcome": r["outcome"]} for r in conflicts]}, indent=2))
                sys.exit(2)

            # Step 2: create jobs; priority is derived from the persisted origin.
            with conn.cursor() as cur:
                cur.execute(build_sql(insert=True), params)
                rows = cur.fetchall()
            conn.commit()
            print(json.dumps({"mode": args.mode, "inserted": len(rows),
                              "rows": [{k: jsonable(v) for k, v in r.items()} for r in rows]},
                             indent=2, default=str))
            return

        # preview
        with conn.cursor() as cur:
            cur.execute(build_sql(insert=False), params)
            rows = cur.fetchall()

        queueable = [r for r in rows
                     if r["existing_asset_status"] != "completed"
                     and r["existing_job_status"] not in ("pending", "processing", "completed")]

        if args.one_per_lecture:
            seen, picked = set(), []
            for r in sorted(queueable, key=lambda r: (str(r["lecture_date"]), r["clip_index"])):
                if r["session_id"] in seen:
                    continue
                seen.add(r["session_id"])
                picked.append(r)
            queueable = picked
        if args.limit:
            queueable = queueable[: args.limit]

        print(f"total candidate clips evaluated : {len(rows)}")
        print(f"blocked (asset/job already)     : {len(rows) - len([r for r in rows if r['existing_asset_status'] != 'completed' and r['existing_job_status'] not in ('pending','processing','completed')])}")
        print(f"selected for queueing           : {len(queueable)}\n")

        for r in queueable:
            print("-" * 78)
            print(f"  session_id       : ...{r['session_id'][-28:]}")
            print(f"  lecture_date     : {r['lecture_date']}")
            print(f"  subject          : {r['subject']}")
            print(f"  trainer          : {r['trainer']}")
            print(f"  clip_index       : {r['clip_index']}")
            print(f"  clip_key         : ...{r['clip_key'][-34:]}")
            print(f"  job_key          : {r['job_key']}")
            print(f"  original start   : {r['original_start']}  ({r['orig_start_seconds']}s)")
            print(f"  original end     : {r['original_end']}  ({r['orig_end_seconds']}s)")
            print(f"  padded start_sec : {r['start_seconds']}")
            print(f"  padded end_sec   : {r['end_seconds']}")
            print(f"  requested dur    : {r['requested_duration_seconds']}s")
            print(f"  output_filename  : {r['output_filename']}")
            print(f"  source ids ok    : {bool(r['recording_drive_id']) and bool(r['recording_item_id'])}")
            print(f"  existing asset   : {r['existing_asset_status'] or 'none'}")
            print(f"  existing job     : {r['existing_job_status'] or 'none'}")
        if queueable:
            print("-" * 78)
            print("\nclip_keys selected (pass these to --insert):")
            for r in queueable:
                print(f"  --clip-key '{r['clip_key']}'")


if __name__ == "__main__":
    main()
