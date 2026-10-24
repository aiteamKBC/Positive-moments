"""
The Continuous Bunny Publisher's own entrypoint (a separate service, like the
media runner; deliberately not a sub-command of app.cli.main).

    python -m app.cli.publisher validate-config [--env-file F] [--check-library]
    python -m app.cli.publisher db-smoke        [--env-file F]   (read-only session + advisory-lock check)
    python -m app.cli.publisher run             [--env-file F] [--once] [--max-iterations N]
    python -m app.cli.publisher status          [--env-file F]
    python -m app.cli.publisher lecture         --lecture-id ID
    python -m app.cli.publisher exceptions
    python -m app.cli.publisher retry           --job-id ID --actor NAME --reason TEXT
    python -m app.cli.publisher cancel          --job-id ID --actor NAME --reason TEXT   (blocked duplicates only)
    python -m app.cli.publisher decide-part     --lecture-id ID --part N --decision INCLUDE|EXCLUDE --actor --reason
    python -m app.cli.publisher security-check  [--caption-readback-video-id GUID]   (read-only Bunny reads)
    python -m app.cli.publisher release         --lecture-id ID --actor NAME --reason TEXT   (PRODUCTION_WRITE only)
    python -m app.cli.publisher withdraw        --lecture-id ID --actor NAME --reason TEXT [--library-id N]
    python -m app.cli.publisher delivery        --occurrence-id OCC-...   (the LMS v1 answer, read-only)
    python -m app.cli.publisher reconcile       --kind light|full
    python -m app.cli.publisher pilot-manifest  --manifest PATH --library-id N [--apply --actor NAME]

Configuration comes from the process environment, then --env-file (the
systemd EnvironmentFile). No secret, key or connection string is ever printed.
Exit codes: 0 ok, 2 refused / configuration problem.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.publisher")
    parser.add_argument("--env-file", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate-config", help="validate configuration (no network unless --check-library)")
    check.add_argument("--check-library", action="store_true",
                       help="read the Bunny library settings (read-only core API) in a write mode")
    sub.add_parser("db-smoke", help="read-only check that PUBLISHER_DATABASE_URL is a direct session "
                                    "where session advisory locks hold (writes nothing)")
    run = sub.add_parser("run", help="the supervised daemon")
    run.add_argument("--once", action="store_true", help="one scheduler tick, then drain the queue, then exit")
    run.add_argument("--max-iterations", type=int, default=None)
    sub.add_parser("status", help="status summary and worker health")
    lecture = sub.add_parser("lecture", help="one lecture's progress, per recording part")
    lecture.add_argument("--lecture-id", required=True)
    sub.add_parser("exceptions", help="the exception queue")
    retry = sub.add_parser("retry", help="operator retry of a FAILED / REVIEW_REQUIRED job (audited)")
    retry.add_argument("--job-id", required=True)
    retry.add_argument("--actor", required=True)
    retry.add_argument("--reason", required=True)
    cancel = sub.add_parser("cancel", help="operator cancel of a waiting job of a conclusively blocked "
                                           "calendar duplicate with no Teams meeting (audited)")
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--actor", required=True)
    cancel.add_argument("--reason", required=True)
    decide = sub.add_parser("decide-part", help="operator INCLUDE / EXCLUDE of one recording part (audited)")
    decide.add_argument("--lecture-id", required=True)
    decide.add_argument("--part", required=True, type=int)
    decide.add_argument("--decision", required=True, choices=("INCLUDE", "EXCLUDE"))
    decide.add_argument("--actor", required=True)
    decide.add_argument("--reason", required=True)
    security = sub.add_parser("security-check", help="read-only learner-delivery security check of the library "
                                                     "and its pull zone (flags only; nothing is changed)")
    security.add_argument("--caption-readback-video-id", default=None,
                          help="also fetch this video's caption from the CDN the way the publisher's read-back "
                               "does, and report only the HTTP status (validates CDN token signing)")
    for name, text in (("release", "RELEASE a verified lecture to the LMS (audited; PRODUCTION_WRITE only; "
                                   "refused unless the library passes the security check)"),
                       ("withdraw", "WITHDRAW a lecture from the LMS immediately (audited)")):
        action = sub.add_parser(name, help=text)
        action.add_argument("--lecture-id", required=True)
        action.add_argument("--actor", required=True)
        action.add_argument("--reason", required=True)
        if name == "withdraw":
            action.add_argument("--library-id", type=int, default=None)
    delivery = sub.add_parser("delivery", help="what the LMS API answers for one occurrence (read-only)")
    delivery.add_argument("--occurrence-id", required=True)
    reconcile = sub.add_parser("reconcile", help="run one reconciliation now")
    reconcile.add_argument("--kind", choices=("light", "full"), default="light")
    pilot = sub.add_parser("pilot-manifest", help="plan (default) or apply the pilot manifest promotion")
    pilot.add_argument("--manifest", required=True)
    pilot.add_argument("--library-id", required=True, type=int)
    pilot.add_argument("--apply", action="store_true")
    pilot.add_argument("--actor", default=None)
    return parser


def _config(args, *, validate=True):
    from app.publisher.config import PublisherConfig
    config = PublisherConfig.from_environment(args.env_file)
    return config.validate() if validate else config


def _connect(config):
    import psycopg

    from app.publisher.config import DB_CONNECT_KWARGS
    return psycopg.connect(config.database_url, prepare_threshold=None, **DB_CONNECT_KWARGS)


def _read(config, fn, *args, **kwargs):
    with _connect(config) as connection:
        connection.read_only = True
        return fn(connection, *args, **kwargs)


def main(argv=None) -> int:
    from app.publisher import operations
    from app.publisher.config import ConfigError
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "validate-config":
            config = _config(args, validate=False)
            report = {"config": config.describe(), "problems": config.problems()}
            if args.check_library and not report["problems"] and config.writes_bunny:
                from app.bunny.client import BunnyCoreClient
                from app.publisher.runtime import check_library_settings
                settings = BunnyCoreClient(config.account_api_key).get_video_library(config.library_id)
                report["library_problems"] = check_library_settings(settings)
            return _print(report, ok=not report["problems"] and not report.get("library_problems"))
        if args.command == "db-smoke":
            from app.publisher.smoke import run_db_smoke
            report = run_db_smoke(_config(args, validate=False).database_url)
            return _print(report, ok=report["ok"])
        config = _config(args)
        if args.command == "run":
            from app.publisher.runtime import build_daemon
            daemon = build_daemon(config)
            for name in ("SIGTERM", "SIGINT"):
                handler = getattr(signal, name, None)
                if handler is not None:
                    signal.signal(handler, daemon.request_stop)
            print(json.dumps({"service": "kbc_bunny_publisher", **config.describe()}, default=str), flush=True)
            result = daemon.run_once() if args.once else daemon.run_forever(max_iterations=args.max_iterations)
            return _print(result)
        if args.command == "status":
            return _print(_read(config, operations.status_summary, library_id=config.library_id))
        if args.command == "lecture":
            return _print(_read(config, operations.lecture_detail, args.lecture_id, library_id=config.library_id,
                                language=config.caption_language))
        if args.command == "exceptions":
            return _print(_read(config, operations.exceptions))
        if args.command == "retry":
            with _connect(config) as connection:
                connection.autocommit = True
                return _print(operations.operator_retry(connection, args.job_id, actor=args.actor, reason=args.reason))
        if args.command == "cancel":
            with _connect(config) as connection:
                connection.autocommit = True        # operator_cancel runs its own transaction
                return _print(operations.operator_cancel(connection, args.job_id, actor=args.actor,
                                                         reason=args.reason))
        if args.command == "decide-part":
            with _connect(config) as connection:
                connection.autocommit = True
                return _print(operations.decide_part(connection, args.lecture_id, args.part, decision=args.decision,
                                                     actor=args.actor, reason=args.reason))
        if args.command == "security-check":
            report = _security(config)
            if args.caption_readback_video_id:
                report["caption_readback"] = _caption_readback(config, args.caption_readback_video_id)
            return _print(report, ok=report["secure"])
        if args.command in ("release", "withdraw"):
            from app.publisher import delivery
            if args.command == "release":
                from app.publisher.config import PRODUCTION_WRITE
                if config.mode != PRODUCTION_WRITE:
                    return _print({"refused": "NOT_PRODUCTION_MODE",
                                   "detail": "release needs PUBLISHER_MODE=PRODUCTION_WRITE"}, ok=False)
                security = _security(config)
                with _connect(config) as connection:
                    connection.autocommit = True
                    return _print(delivery.release(connection, args.lecture_id, library_id=config.library_id,
                                                   actor=args.actor, reason=args.reason, security=security,
                                                   language=config.caption_language))
            library = args.library_id or config.library_id
            if library is None:
                return _print({"refused": "NO_LIBRARY", "detail": "pass --library-id in SHADOW mode"}, ok=False)
            with _connect(config) as connection:
                connection.autocommit = True
                return _print(delivery.withdraw(connection, args.lecture_id, library_id=library, actor=args.actor,
                                                reason=args.reason, language=config.caption_language))
        if args.command == "delivery":
            from app.publisher import delivery
            if config.library_id is None:
                return _print({"refused": "NO_LIBRARY", "detail": "SHADOW mode has no delivery library"}, ok=False)
            answer = _read(config, delivery.occurrence_recording, args.occurrence_id, library_id=config.library_id,
                           playback_enabled=False, language=config.caption_language)
            return _print(answer if answer is not None else {"refused": "OCCURRENCE_NOT_FOUND"}, ok=answer is not None)
        if args.command == "reconcile":
            from app.publisher.runtime import build_daemon
            return _print(build_daemon(config).reconciler.run(args.kind.upper()))
        if args.command == "pilot-manifest":
            from app.publisher import pilot_migration
            with _connect(config) as connection:
                if not args.apply:
                    connection.read_only = True
                    return _print(pilot_migration.plan(connection, args.manifest, library_id=args.library_id))
                if not args.actor:
                    return _print({"refused": "--apply needs --actor"}, ok=False)
                connection.autocommit = True
                return _print(pilot_migration.apply(connection, args.manifest, library_id=args.library_id,
                                                    actor=args.actor))
    except ConfigError as error:
        return _print({"refused": "configuration", "problems": error.problems}, ok=False)
    except operations.OperatorActionRefused as error:
        return _print({"refused": error.code, "detail": error.detail}, ok=False)
    return 2


def _security(config) -> dict:
    """Read-only: Get Video Library + Get Pull Zone with the account key; flags only."""
    from app.bunny.client import BunnyCoreClient
    from app.publisher.library_security import read_delivery_security
    if config.library_id is None or not config.account_api_key:
        return {"secure": False, "library_id": config.library_id,
                "problems": ["NO_LIBRARY: a write mode with its account key is needed for the security check"]}
    return read_delivery_security(BunnyCoreClient(config.account_api_key), config.library_id)


def _caption_readback(config, video_id: str) -> dict:
    """Fetch one caption file from the CDN as the publisher's read-back does. Reports the status only."""
    import re
    import urllib.error
    import urllib.request

    from app.bunny import tokens
    BOM = bytes((0xEF, 0xBB, 0xBF))
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", video_id or "") or not config.cdn_hostname:
        return {"status": "NOT_ATTEMPTED", "detail": "needs a video GUID and a configured CDN hostname"}
    path = f"/{video_id}/captions/{config.caption_language}.vtt"
    url = tokens.signed_cdn_url(config.cdn_hostname, path, config.cdn_token_key, params={"ver": 0})         if config.cdn_token_key else f"https://{config.cdn_hostname}{path}?ver=0"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "text/vtt"}), timeout=30) as r:
            body = r.read()
            return {"status": r.status, "signed": bool(config.cdn_token_key),
                    "is_webvtt": body.lstrip(BOM)[:6] == b"WEBVTT"}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "signed": bool(config.cdn_token_key)}
    except (urllib.error.URLError, OSError) as error:
        return {"status": "UNREACHABLE", "detail": type(error).__name__}


def _print(value, *, ok: bool = True) -> int:
    print(json.dumps(value, indent=2, default=str))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
