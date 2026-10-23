"""
Bunny Stream single-recording pilot.

    python -m app.cli.bunny pilot --lecture-id <uuid> --library-id <id> [--dry-run]
    python -m app.cli.bunny lms-match --date-from 2026-10-01 --date-to 2026-10-20   (read-only)

Reads production PostgreSQL ONLY through tools.audit_qa_core_production.
connect_read_only (proven read-only by a refused write probe), and only long
enough to load the lecture's source rows. State lives in a git-ignored local
manifest (output/bunny_pilot/manifest.json) until migration 024 is approved.

Settings come from the process environment, then backend/.env, and only these
names are read: BUNNY_STAGING_LIBRARY_ID, BUNNY_STAGING_API_KEY,
BUNNY_STAGING_CDN_HOSTNAME, optional BUNNY_STAGING_ACCOUNT_API_KEY (needed to
verify library encoding/transcribing settings), MICROSOFT_GRAPH_*. No value is
ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NAMES = ("BUNNY_STAGING_LIBRARY_ID", "BUNNY_STAGING_API_KEY", "BUNNY_STAGING_CDN_HOSTNAME",
         "BUNNY_STAGING_ACCOUNT_API_KEY", "MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
         "MICROSOFT_GRAPH_CLIENT_SECRET", "MICROSOFT_GRAPH_SCOPE", "MICROSOFT_GRAPH_BASE_URL")
FORBIDDEN_LIBRARY_IDS = frozenset({785080})


def _settings() -> dict:
    values = {name: (os.environ.get(name) or "").strip() or None for name in NAMES}
    if not all(values[n] for n in NAMES if n != "BUNNY_STAGING_ACCOUNT_API_KEY"):
        try:
            from dotenv import dotenv_values
            file_values = dotenv_values(REPO_ROOT / "backend" / ".env")
        except ImportError:
            file_values = {}
        for name in NAMES:
            values[name] = values[name] or (str(file_values.get(name) or "").strip() or None)
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.bunny")
    sub = parser.add_subparsers(dest="command", required=True)
    pilot = sub.add_parser("pilot", help="preflight + import + captions for ONE recording")
    pilot.add_argument("--lecture-id", required=True)
    pilot.add_argument("--library-id", required=True, type=int,
                       help="must equal BUNNY_STAGING_LIBRARY_ID (a typo guard)")
    pilot.add_argument("--dry-run", action="store_true", help="preflight only; no Bunny writes")
    pilot.add_argument("--output-dir", default=str(REPO_ROOT / "output" / "bunny_pilot"))
    pilot.add_argument("--poll-interval", type=float, default=30.0)
    pilot.add_argument("--identify-timeout", type=float, default=900.0)
    pilot.add_argument("--encoding-timeout", type=float, default=3 * 3600.0)

    match = sub.add_parser("lms-match", help="READ-ONLY dry run of LMS matching v2 for a date range")
    match.add_argument("--date-from", required=True, type=date.fromisoformat)
    match.add_argument("--date-to", required=True, type=date.fromisoformat)
    match.add_argument("--test-programmes", default=None,
                       help="';'-separated exact programme names (default: PUBLISHER_LMS_TEST_PROGRAMMES)")
    match.add_argument("--output-dir", default=str(REPO_ROOT / "output" / "lms_matching"))
    return parser


def _lms_settings() -> dict:
    """Only these two names are read; values are never printed."""
    names = ("LMS_DATABASE_URL", "PUBLISHER_LMS_TEST_PROGRAMMES")
    values = {name: (os.environ.get(name) or "").strip() or None for name in names}
    if not all(values.values()):
        try:
            from dotenv import dotenv_values
            file_values = dotenv_values(REPO_ROOT / "backend" / ".env")
        except ImportError:
            file_values = {}
        for name in names:
            values[name] = values[name] or (str(file_values.get(name) or "").strip() or None)
    return values


def run_lms_match(args) -> dict:
    """
    Reads Lecture Intelligence through connect_read_only (refused write probe)
    and the LMS inside SET TRANSACTION READ ONLY. Writes nothing to either
    database; the report (identifiers and counts only) goes to --output-dir.
    """
    import collections

    import psycopg

    from app.lms import service
    from app.lms.matching import MatchConfig
    from app.lms.sources import LmsOccurrenceSource, load_lectures, window_for
    from tools.audit_qa_core_production import connect_read_only

    settings = _lms_settings()
    config = MatchConfig.from_text(args.test_programmes if args.test_programmes is not None
                                   else settings["PUBLISHER_LMS_TEST_PROGRAMMES"])
    connection, read_only = connect_read_only()
    try:
        if read_only != "on":
            raise SystemExit("REFUSED: Lecture Intelligence connection is not read-only")
        connection.prepare_threshold = None
        lectures = load_lectures(connection, args.date_from, args.date_to)
    finally:
        connection.close()
    snapshot = LmsOccurrenceSource(settings["LMS_DATABASE_URL"]).load(*window_for(args.date_from, args.date_to))
    results = service.evaluate(lectures, snapshot, config)
    owners = {link.occurrence_id: r.lecture_id for r in results if r.status == "VERIFIED" for link in r.links}
    in_range = type(snapshot)(
        occurrences=tuple(o for o in snapshot.occurrences
                          if args.date_from <= o.scheduled_start.date() <= args.date_to),
        shares=snapshot.shares, window_start=snapshot.window_start, window_end=snapshot.window_end)
    coverage = service.reconcile_coverage(None, in_range, config, datetime.now(timezone.utc), owners=owners)
    report = {
        "mode": "DRY_RUN_READ_ONLY", "rule_version": results[0].rule_version if results else None,
        "date_from": args.date_from.isoformat(), "date_to": args.date_to.isoformat(),
        "test_programmes": sorted(config.test_programmes),
        "lecture_side": service.summarize(results),
        "lms_side": {"occurrences": len(coverage),
                     "by_coverage": dict(sorted(collections.Counter(r.coverage_status for r in coverage).items())),
                     "by_coverage_completed_only": dict(sorted(collections.Counter(
                         r.coverage_status for r in coverage if r.occurrence.status == "completed").items()))},
        "lectures": [{"lecture_id": r.lecture_id, "status": r.status,
                      "primary_occurrence_id": r.primary.occurrence_id if r.primary else None,
                      "linked_occurrence_ids": [l.occurrence_id for l in r.links],
                      "candidate_occurrence_ids": list(r.candidate_ids),
                      "reason": r.evidence.get("reason")} for r in results],
        "lms_only": [{"occurrence_id": r.occurrence.occurrence_id, "live_session_id": r.occurrence.live_session_id,
                      "meeting_type": r.occurrence.meeting_type, "occurrence_status": r.occurrence.status,
                      "coverage_status": r.coverage_status}
                     for r in coverage if r.coverage_status in ("LMS_ONLY_PENDING", "LMS_SESSION_NOT_DISCOVERED")],
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"match-v2-{args.date_from:%Y%m%d}-{args.date_to:%Y%m%d}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return {k: report[k] for k in ("mode", "rule_version", "date_from", "date_to", "test_programmes",
                                   "lecture_side", "lms_side")} | {"report_path": path.as_posix()}


def run_pilot(args) -> dict:
    import psycopg

    from app.bunny.client import BunnyCoreClient, BunnyStreamClient
    from app.bunny.manifest import PilotManifest
    from app.bunny.pilot import BunnyPilot, PilotConfig
    from app.bunny.source import load_lecture_source
    from app.graph.client import Phase1GraphClient
    from app.media.delivery import SharePointMedia
    from tools.audit_qa_core_production import connect_read_only

    settings = _settings()
    configured = settings["BUNNY_STAGING_LIBRARY_ID"]
    if args.library_id in FORBIDDEN_LIBRARY_IDS or str(args.library_id) != str(configured or ""):
        raise SystemExit("REFUSED: --library-id must equal BUNNY_STAGING_LIBRARY_ID and must not be "
                         "the old account's library")

    def connect(attempts: int = 3):
        # Opening a read-only connection is safe to retry; this machine's DNS is intermittent.
        for attempt in range(1, attempts + 1):
            try:
                return connect_read_only()
            except psycopg.OperationalError:
                if attempt == attempts:
                    raise
                time.sleep(5 * attempt)

    def load_source():
        connection, read_only = connect()
        try:
            if read_only != "on":
                raise SystemExit("REFUSED: production connection is not read-only")
            connection.prepare_threshold = None          # pooled server: no named statements
            connection.cursor_factory = psycopg.ClientCursor
            return load_lecture_source(connection, args.lecture_id)
        finally:
            connection.close()

    graph = Phase1GraphClient.from_mapping(settings)
    account_key = settings["BUNNY_STAGING_ACCOUNT_API_KEY"]
    output_dir = Path(args.output_dir)
    pilot = BunnyPilot(
        config=PilotConfig(lecture_id=args.lecture_id, library_id=args.library_id,
                           cdn_hostname=settings["BUNNY_STAGING_CDN_HOSTNAME"],
                           poll_interval_seconds=args.poll_interval,
                           identify_timeout_seconds=args.identify_timeout,
                           encoding_timeout_seconds=args.encoding_timeout,
                           forbidden_library_ids=FORBIDDEN_LIBRARY_IDS,
                           output_dir=str(output_dir), dry_run=args.dry_run),
        load_source=load_source, graph=graph, media=SharePointMedia(graph),
        bunny=BunnyStreamClient(int(configured), settings["BUNNY_STAGING_API_KEY"]),
        core=BunnyCoreClient(account_key) if account_key else None,
        manifest=PilotManifest(output_dir / "manifest.json"),
        credentials={name: bool(settings[name]) for name in
                     ("BUNNY_STAGING_LIBRARY_ID", "BUNNY_STAGING_API_KEY", "BUNNY_STAGING_CDN_HOSTNAME",
                      "BUNNY_STAGING_ACCOUNT_API_KEY")})
    report = pilot.run()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"report-{stamp}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = path.as_posix()
    return report


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "lms-match":
        print(json.dumps(run_lms_match(args), indent=2, default=str))
        return 0
    report = run_pilot(args)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("outcome") in ("COMPLETED", "READY") else 2


if __name__ == "__main__":
    sys.exit(main())
