"""
Before/after audit for the publisher's production pilot. READ-ONLY.

    python tools/publisher_pilot_audit.py snapshot --env-file backend/config/publisher.pilot-2026-10-22.env --label before
    python tools/publisher_pilot_audit.py diff output/publisher_pilot/audit-before-*.json output/publisher_pilot/audit-after-*.json

A snapshot records, on a default_transaction_read_only=on session of the
publisher's DIRECT connection:
  * a restore target: the server clock and pg_current_wal_lsn() (Neon PITR accepts a timestamp or LSN);
  * the row count of every table the pilot may write, existing and proposed (024-028);
  * for the pilot day: every lecture_sessions row in full (the pre-image of any planned update),
    and per-lecture counts of transcript candidates / artifacts / content versions / selections;
  * QA activity: lecture_pipeline_runs started on the pilot day.
Files go to output/publisher_pilot/ (git-ignored) and hold production rows - keep them local.
`diff` prints per-table deltas and changed pilot-day lectures; it never connects anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.publisher.config import PublisherConfig, database_endpoint  # noqa: E402

EXISTING = ("lecture_sessions", "lecture_discovery_runs", "lecture_transcript_artifacts",
            "lecture_transcript_candidates", "lecture_transcript_artifact_contents", "lecture_transcript_selections",
            "lecture_transcript_selection_parts", "lecture_combined_transcripts", "lecture_pipeline_runs",
            "qa_doctors_sessions")
PROPOSED = ("bunny_videos", "bunny_caption_publications", "bunny_caption_current",
            "bunny_lms_match_evaluations", "bunny_lms_occurrence_links", "bunny_lms_occurrence_coverage",
            "bunny_publisher_jobs", "bunny_publisher_events", "bunny_publisher_heartbeats", "bunny_recording_discoveries",
            "bunny_recording_sources", "bunny_publisher_part_decisions", "bunny_publisher_schedule", "bunny_publisher_alerts",
            "bunny_publisher_reconciliation_runs")
PER_LECTURE = """
SELECT l.lecture_id::text,
       (SELECT count(*) FROM public.lecture_transcript_candidates c WHERE c.lecture_id = l.lecture_id),
       (SELECT count(*) FROM public.lecture_transcript_artifact_contents v
          JOIN public.lecture_transcript_candidates c ON c.artifact_id = v.artifact_id WHERE c.lecture_id = l.lecture_id),
       (SELECT count(*) FROM public.lecture_transcript_selections s WHERE s.lecture_id = l.lecture_id),
       (SELECT count(*) FROM public.lecture_combined_transcripts t WHERE t.lecture_id = l.lecture_id)
  FROM public.lecture_sessions l WHERE l.session_date = %s ORDER BY l.lecture_id
"""


def snapshot(env_file: str, label: str) -> Path:
    config = PublisherConfig.from_environment(env_file)
    if database_endpoint(config.database_url) != "neon-direct":
        raise SystemExit("refused: the env file's PUBLISHER_DATABASE_URL is not a Neon DIRECT endpoint")
    day = config.start_date
    if day is None or config.end_date != day:
        raise SystemExit("refused: a pilot snapshot needs PUBLISHER_START_DATE == PUBLISHER_END_DATE")
    with psycopg.connect(config.database_url, prepare_threshold=None, autocommit=True, connect_timeout=15,
                         application_name="kbc_publisher_pilot_audit",
                         options="-c default_transaction_read_only=on -c statement_timeout=60000") as c:
        assert c.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        present = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                           "AND tablename = ANY(%s)", (list(EXISTING + PROPOSED),)).fetchall()}
        counts = {t: (c.execute(f"SELECT count(*) FROM public.{t}").fetchone()[0] if t in present else None)
                  for t in EXISTING + PROPOSED}
        clock, lsn = c.execute("SELECT now(), pg_current_wal_lsn()::text").fetchone()
        cursor = c.execute("SELECT * FROM public.lecture_sessions WHERE session_date = %s ORDER BY lecture_id", (day,))
        names = [d.name for d in cursor.description]
        lectures = [dict(zip(names, r)) for r in cursor.fetchall()]
        per_lecture = {r[0]: {"candidates": r[1], "content_versions": r[2], "selections": r[3], "combined": r[4]}
                       for r in c.execute(PER_LECTURE, (day,)).fetchall()}
        qa_runs = [{"run_type": r[0], "started_at": r[1], "status": r[2]} for r in c.execute(
            "SELECT run_type, started_at, status FROM public.lecture_pipeline_runs WHERE started_at::date = %s "
            "ORDER BY started_at", (day,)).fetchall()]
    data = {"label": label, "day": day.isoformat(), "taken_at": datetime.now(timezone.utc).isoformat(),
            "restore_target": {"server_time": clock, "wal_lsn": lsn}, "table_counts": counts,
            "pilot_day_lectures": lectures, "pilot_day_transcripts": per_lecture, "qa_runs_on_day": qa_runs}
    target = ROOT / "output" / "publisher_pilot"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"audit-{label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"written": str(path.relative_to(ROOT)), "restore_target": data["restore_target"],
                      "table_counts": counts, "pilot_day_lectures": len(lectures),
                      "pilot_day_transcripts": per_lecture, "qa_runs_on_day": qa_runs}, indent=2, default=str))
    return path


def diff(before: str, after: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (before, after))
    deltas = {t: {"before": a["table_counts"].get(t), "after": b["table_counts"].get(t)}
              for t in b["table_counts"] if a["table_counts"].get(t) != b["table_counts"].get(t)}
    before_rows = {r["lecture_id"]: r for r in a["pilot_day_lectures"]}
    changed = {}
    for row in b["pilot_day_lectures"]:
        old = before_rows.get(row["lecture_id"])
        if old is None:
            changed[row["lecture_id"][:8]] = "INSERTED"
        else:
            fields = sorted(k for k in row if k not in ("updated_at", "last_discovered_at") and row[k] != old.get(k))
            if fields:
                changed[row["lecture_id"][:8]] = {"updated_fields": fields}
    print(json.dumps({"table_deltas": deltas, "pilot_day_lectures": changed,
                      "transcripts_after": {k[:8]: v for k, v in b["pilot_day_transcripts"].items()},
                      "qa_runs_after": b["qa_runs_on_day"]}, indent=2, default=str))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/publisher_pilot_audit.py")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--env-file", required=True)
    s.add_argument("--label", required=True, choices=("before", "after", "check"))
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        snapshot(args.env_file, args.label)
    else:
        diff(args.before, args.after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
