"""
The Positive Moments media platform's own entrypoint.

    python -m app.cli.media media-runner              the long-running service
    python -m app.cli.media positive-moments-preview  read-only readiness check

Deliberately NOT a sub-command of `app.cli.main`: the QA Core scheduler image
is built around that entrypoint's import closure, and the deployment contract
(tests/unit/test_deployment_contract.py) keeps `app.media` out of it. The
media runner is a separate service with a separate entrypoint.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date

from app.common.logging import configure_logging
from app.config.settings import Settings
from app.db.connection import database_connection, readonly_database_connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.media")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser(
        "media-runner",
        help="claim and process Positive Moments analysis runs and media jobs until stopped")
    runner.add_argument("--poll-seconds", type=int, default=10)
    runner.add_argument("--max-iterations", type=int, default=None,
                        help="stop after N loop iterations; omit to run until stopped")
    preview = commands.add_parser(
        "positive-moments-preview",
        help="read-only: transcript, recording and alignment readiness")
    preview.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    preview.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    return parser


def run_media_runner(settings: Settings, args) -> dict:
    """
    Concurrency 1. Restarting never queues a render: money is spent only on
    jobs an operator moved to QUEUED from the website.
    """
    settings.require_database()
    from app.media.factory import build_media_runner

    def _open():
        return database_connection(settings.database_url)

    runner = build_media_runner(settings, connection_factory=_open)
    import signal
    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            # Finish the unit in flight, then exit. Leases and run checkpoints
            # make the next start resume safely.
            signal.signal(handler, runner.request_stop)
    return {"service": "positive_moments_media_runner", "media": settings.media_readiness(),
            **runner.run_forever(poll_seconds=args.poll_seconds,
                                 max_iterations=args.max_iterations)}


def positive_moments_preview(settings: Settings, args) -> dict:
    """
    Read-only by construction, like `recording-links-preview`: a PostgreSQL
    READ ONLY transaction asserted before the first query, no prepared
    statements, Graph GETs only (DriveItem facts and the recordings listing),
    NO model call and NO render provider. Nothing is persisted.
    """
    from app.media.factory import build_media_service
    from app.media.repository_view import PreMigrationMedia, PreMigrationMoments, alignment_row

    settings.require_database()
    service = build_media_service(settings, model=False)
    service.analyzer = None
    rows = []
    with readonly_database_connection(settings.database_url) as connection:
        connection.prepare_threshold = None
        read_only = connection.execute("SHOW transaction_read_only").fetchone()[0]
        if read_only != "on":
            raise RuntimeError("refusing to preview: transaction_read_only is not on")
        migrated = connection.execute(
            "SELECT to_regclass('public.positive_moment_analyses') IS NOT NULL").fetchone()[0]
        if not migrated:
            # Before migration 023: transcript, recording and alignment are all
            # read from existing tables; only the (empty) media tables are skipped.
            service.moments = PreMigrationMoments(service.moments)
            service.media = PreMigrationMedia(service.media)
        for lecture_id in service.media.lectures_in_range(connection, args.from_date,
                                                          args.to_date):
            outcome = service.process(connection, lecture_id, analyze=False,
                                      allow_model=False, persist_jobs=False,
                                      probe_alignment=True)
            rows.append(alignment_row(outcome))
    return {"read_only": True, "database_writes": 0, "provider_calls": 0,
            "render_calls": 0, "transaction_read_only": read_only,
            "media_tables_present": bool(migrated),
            "by_recording_state": dict(Counter(r["recording_state"] for r in rows)),
            "by_alignment": dict(Counter(r["alignment_status"] or "NOT_ATTEMPTED"
                                         for r in rows)),
            "lectures": rows}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    settings = Settings.from_environment()
    try:
        if args.command == "media-runner":
            summary = run_media_runner(settings, args)
        else:
            summary = positive_moments_preview(settings, args)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 2
    print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
