import argparse
import json
import re
from datetime import date, datetime, timedelta

from app.common.logging import configure_logging
from app.common.errors import WRITER_GUARD_REFUSED, PlatformError
from app.transcripts.seam import SEAM_PARSER_VERSION
from app.transcripts.seam_service import SeamDedupService
from app.transcripts.webvtt import PARSER_VERSION
from app.config.settings import Settings
from app.db.connection import database_connection, readonly_database_connection
from app.db.repositories.aptem import AptemRepository
from app.db.repositories.discovery_runs import DiscoveryRunRepository
from app.db.repositories.lecture_sessions import LectureSessionRepository
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_validation import QaValidationRepository
from app.db.repositories.ready_lectures import LegacyQaEvidenceRepository, ReadyLectureRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_runs import TranscriptAcquisitionRunRepository
from app.db.repositories.transcript_documents import (
    TranscriptDocumentRepository,
    TranscriptParseRunRepository,
)
from app.db.repositories.transcript_speakers import (
    LegacyTrainerDiagnosticRepository,
    SpeakerInventoryRunRepository,
    TranscriptSpeakerRepository,
)
from app.db.repositories.attendance_resolution import (
    AttendanceSnapshotRepository,
    AttendanceSourceRepository,
    SpeakerIdentityRepository,
    SpeakerInventoryReadRepository,
    SpeakerResolutionRunRepository,
    SpeakerRoleRepository,
)
from app.db.repositories.engagement import (
    EngagementInputRepository,
    EngagementRepository,
    EngagementRunRepository,
    LegacyEngagementParityRepository,
)
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository,
    TranscriptSelectionRunRepository,
)
from app.graph.auth import build_graph_client
from app.graph.calendar import CalendarGateway
from app.graph.meetings import OnlineMeetingGateway
from app.lectures.service import LectureDiscoveryService, select_single_lecture
from app.transcripts.graph import TranscriptGateway
from app.transcripts.service import TranscriptAcquisitionService
from app.transcripts.selection_service import TranscriptSelectionService
from app.transcripts.document_service import CanonicalTranscriptService
from app.transcripts.speaker_service import SpeakerInventoryService
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION, ROSTER_RULES
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.attendance.service import SpeakerResolutionService
from app.engagement.service import EngagementService
from app.db.repositories.qa_shadow import (
    LegacyQaComparisonRepository,
    QaEvaluationRepository,
    QaInputRepository,
    QaRunRepository,
)
from app.qa.provider import OpenAIChatProvider
from app.qa.recovery import recovery_state
from app.qa.backfill import ModelInputFingerprintBackfill
from app.attendance.coverage_backfill import CoverageBackfill
from app.attendance.recovery import AttendanceRecoveryService
from app.qa.structured_output import (
    DEFAULT_PROVIDER_CONTRACT,
    PROVIDER_CONTRACT_VERSIONS,
)
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSIONS,
)
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.qa.service import ShadowQaService
from app.db.repositories.qa_rendering import (
    LegacyRenderComparisonRepository,
    LmsSnapshotRepository,
    LmsSourceRepository,
    RenderInputRepository,
    RenderRunRepository,
    RenderedOutputRepository,
)
from app.rendering.service import QaRenderingService
from app.db.repositories.qa_writer import (
    LegacyQaTargetRepository,
    RenderedPayloadRepository,
    WriterOwnershipRepository,
)
from app.db.repositories.qa_writer import GenerationAttemptRepository
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    WRITE_ENABLED_MODES,
    WRITE_MODES,
)
from app.qa.perfect import may_publish
from app.rendering.evidence import RENDERER_VERSION
from app.writer.perfect_service import PerfectLecturePlanner, plan_perfect_for_day
from app.orchestration.factory import (
    build_operations,
    build_orchestrator,
    build_resolver,
    build_scheduler,
)
from app.orchestration.daemon import SchedulerDaemon, next_fire_time, parse_cron
from app.lectures.duplicate_service import DuplicateResolutionService
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.scheduler import SchedulerConfig, SchedulerDisabled
from app.orchestration.stages import RUN_TYPE_MANUAL, RUN_TYPE_RECONCILE
from app.writer.service import LegacyQaWriter


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_start(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("start must be HH:MM in 24-hour Cairo time")
    return value


def json_default(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def guard_write_command(args) -> None:
    """
    Phase 3C2 human-safe canary guard.

    A write-enabled mode may never run date-wide, may never auto-select its own
    target, and may never write without an explicit confirmation. CANARY_NEW_ONLY
    is additionally restricted to exactly ONE named lecture: a canary that could
    write two lectures is not a canary.
    """
    if args.mode not in WRITE_ENABLED_MODES:
        return
    if not args.lecture_ids:
        raise PlatformError(
            WRITER_GUARD_REFUSED,
            f"{args.mode} requires an explicit --lecture-id; a date-wide write is refused")
    if args.mode == CANARY_NEW_ONLY and len(args.lecture_ids) != 1:
        raise PlatformError(
            WRITER_GUARD_REFUSED,
            "CANARY_NEW_ONLY writes exactly one lecture; "
            f"{len(args.lecture_ids)} --lecture-id values were given")
    if not args.confirm_write:
        raise PlatformError(
            WRITER_GUARD_REFUSED,
            f"{args.mode} requires --confirm-write to perform a real legacy write")
    # F-01. Refused before any connection is opened, and regardless of
    # --skip-perfect-lecture: naming a non-publishable policy in a write mode is
    # a mistake worth stopping, not a flag combination worth interpreting. The
    # Perfect planner enforces the same rule again on its own.
    policy = getattr(args, "perfect_policy", None)
    if policy is not None and not may_publish(policy):
        raise PlatformError(
            WRITER_GUARD_REFUSED,
            f"--perfect-policy {policy} may not be used with {args.mode}; it has no "
            "attendance requirement and is retained for DRY_RUN historical "
            "reproduction only")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kbc-lecture-intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover-day")
    discover.add_argument("--date", required=True, type=parse_date)
    discover.add_argument("--dry-run", action="store_true")
    discover.add_argument("--json", action="store_true")
    acquire = commands.add_parser("acquire-transcripts")
    acquire.add_argument("--date", required=True, type=parse_date)
    acquire.add_argument("--dry-run", action="store_true")
    acquire.add_argument("--list-only", action="store_true",
                         help="list transcript artifacts without fetching any content")
    acquire.add_argument("--json", action="store_true")
    choose = commands.add_parser("select-transcripts")
    choose.add_argument("--date", required=True, type=parse_date)
    choose.add_argument("--dry-run", action="store_true")
    choose.add_argument("--json", action="store_true")
    parse = commands.add_parser("parse-transcripts")
    parse.add_argument("--date", required=True, type=parse_date)
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("--json", action="store_true")
    speakers = commands.add_parser("build-speaker-inventory")
    speakers.add_argument("--date", required=True, type=parse_date)
    speakers.add_argument("--dry-run", action="store_true")
    speakers.add_argument("--parser-version", default=PARSER_VERSION,
                          help="canonical document version to build speakers from")
    speakers.add_argument("--json", action="store_true")
    resolve = commands.add_parser("resolve-speakers")
    resolve.add_argument("--date", required=True, type=parse_date)
    resolve.add_argument("--dry-run", action="store_true")
    resolve.add_argument("--json", action="store_true")
    resolve.add_argument("--parser-version", default=PARSER_VERSION,
                         help="canonical document version to resolve speakers for")
    resolve.add_argument("--roster-version", default=ATTENDANCE_RESOLUTION_VERSION,
                         choices=sorted(ROSTER_RULES))
    engage = commands.add_parser("calculate-engagement")
    # Phase 3C3B: exactly one scope, chosen explicitly. --date is no longer
    # required-and-therefore-default, so a lecture-scoped run can never widen
    # to the whole date by omission.
    engage_scope = engage.add_mutually_exclusive_group(required=True)
    engage_scope.add_argument("--date", type=parse_date,
                              help="batch scope: every lecture on this date")
    engage_scope.add_argument("--lecture-id",
                              help="lecture scope: calculate exactly this lecture")
    engage.add_argument("--dry-run", action="store_true")
    engage.add_argument("--json", action="store_true")
    engage.add_argument("--roster-version", default=ATTENDANCE_RESOLUTION_VERSION,
                        choices=sorted(ROSTER_RULES))
    qa = commands.add_parser("run-qa-shadow")
    qa.add_argument("--date", required=True, type=parse_date)
    qa.add_argument("--execute", action="store_true",
                    help="make real model calls; without it the run is a no-cost preview")
    qa.add_argument("--force", action="store_true",
                    help="re-evaluate even when an identical successful evaluation exists")
    qa.add_argument("--parser-version", default=PARSER_VERSION,
                    help="canonical document version the QA inputs must come from")
    qa.add_argument("--lecture-id", action="append", dest="lecture_ids",
                    help="restrict the run to these lecture ids")
    # Phase 3C3D. Defaults to provider-enforced strict JSON Schema. The old
    # plain-JSON contract stays selectable BY NAME so a historical evaluation
    # can be reproduced deliberately rather than by accident.
    qa.add_argument("--provider-contract", default=DEFAULT_PROVIDER_CONTRACT,
                    choices=sorted(PROVIDER_CONTRACT_VERSIONS),
                    help="which response contract the provider must enforce")
    qa.add_argument("--json", action="store_true")
    render = commands.add_parser("render-qa-output")
    render.add_argument("--date", required=True, type=parse_date)
    render.add_argument("--refresh-lms", action="store_true",
                        help="re-read the live LMS roster even when a snapshot exists")
    render.add_argument("--dry-run", action="store_true")
    render.add_argument("--lecture-id", action="append", dest="lecture_ids",
                        help="restrict the run to these lecture ids")
    render.add_argument("--json", action="store_true")
    write = commands.add_parser("write-legacy-qa")
    write.add_argument("--date", required=True, type=parse_date)
    # Defaults to DRY_RUN: a write-enabled mode is never the default.
    write.add_argument("--mode", default=DRY_RUN, choices=sorted(WRITE_MODES))
    write.add_argument("--allow-update-existing", action="store_true",
                       help="EXPLICIT_BACKFILL only: authorise updating coded-owned rows")
    write.add_argument("--confirm-write", action="store_true",
                       help="required to actually write in a write-enabled mode")
    write.add_argument("--lecture-id", action="append", dest="lecture_ids",
                       help="restrict the run to specific lectures (required for backfill)")
    # Phase 3C2.3D: the derived Perfect Lecture output is planned alongside the
    # QA rows by default, so one canary plan shows both targets. It obeys the
    # same mode and the same confirmation.
    write.add_argument("--skip-perfect-lecture", action="store_true",
                       help="plan the QA rows only, without the derived Perfect Lecture output")
    write.add_argument("--perfect-policy", default=DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                       choices=sorted(PERFECT_ELIGIBILITY_VERSIONS),
                       help="Perfect Lecture eligibility policy. The default requires "
                            "authoritative attendance coverage before a lecture may be "
                            "published; legacy_qa_v8_perfect_v1 reproduces the legacy "
                            "rule exactly and exists for historical reproduction")
    write.add_argument("--json", action="store_true")
    perfect = commands.add_parser("evaluate-perfect-lecture")
    perfect.add_argument("--date", required=True, type=parse_date)
    perfect.add_argument("--lecture-id", action="append", dest="lecture_ids",
                         help="restrict the run to specific lectures")
    perfect.add_argument("--persist", action="store_true",
                         help="store the coded shadow result; writes no legacy row in any case")
    perfect.add_argument("--perfect-policy", default=DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                         choices=sorted(PERFECT_ELIGIBILITY_VERSIONS),
                         help="Perfect Lecture eligibility policy. The default requires "
                              "authoritative attendance coverage before a lecture may be "
                              "published; legacy_qa_v8_perfect_v1 reproduces the legacy "
                              "rule exactly and exists for historical reproduction")
    perfect.add_argument("--json", action="store_true")
    recover = commands.add_parser("recover-attendance")
    recover.add_argument("--lecture-id", required=True,
                         help="the single lecture to recover; never widens to a date")
    recover.add_argument("--dry-run", action="store_true",
                         help="report what the source now says without writing anything")
    recover.add_argument("--json", action="store_true")
    coverage = commands.add_parser("backfill-coverage-metadata")
    coverage.add_argument("--dry-run", action="store_true",
                          help="report the derived coverage without stamping it")
    coverage.add_argument("--skip-model-input-fingerprints", action="store_true",
                          help="stamp attendance coverage only, not QA reusability")
    coverage.add_argument("--json", action="store_true")
    recovery = commands.add_parser("qa-recovery-state")
    recovery.add_argument("--lecture-id", required=True,
                          help="the single lecture to report recovery state for")
    recovery.add_argument("--reconcile-attempts", action="store_true",
                          help="re-derive the evaluation attempt aggregate from the "
                               "authoritative attempts table; deletes no attempt")
    recovery.add_argument("--json", action="store_true")
    # --- Phase 4A orchestration ------------------------------------------
    run_pipeline = commands.add_parser(
        "run-pipeline",
        help="reconcile one day (plus optional lookback) once, stage-aware")
    run_pipeline.add_argument("--date", required=True, type=parse_date)
    run_pipeline.add_argument("--lookback-days", type=int, default=0)
    run_pipeline.add_argument("--once", action="store_true",
                              help="accepted for symmetry; a run is always one cycle")
    run_pipeline.add_argument("--dry-run", action="store_true")
    run_pipeline.add_argument("--discover", action="store_true",
                              help="also run Graph calendar discovery for the window")
    run_pipeline.add_argument("--no-provider", action="store_true",
                              help="refuse any action that would buy a generation")
    run_pipeline.add_argument("--no-graph", action="store_true",
                              help="refuse any action that would call Microsoft Graph")
    run_pipeline.add_argument("--max-passes", type=int, default=8)
    run_pipeline.add_argument("--json", action="store_true")

    reconcile = commands.add_parser(
        "reconcile-day", help="read-only day reconciliation report")
    reconcile.add_argument("--date", required=True, type=parse_date)
    reconcile.add_argument("--lookback-days", type=int, default=0)
    reconcile.add_argument("--execute", action="store_true",
                           help="also perform the required stages before reporting")
    reconcile.add_argument("--probe-attendance", action="store_true",
                           help="ask the attendance source whether waiting lectures can now recover")
    reconcile.add_argument("--json", action="store_true")

    duplicates = commands.add_parser(
        "resolve-duplicates",
        help="deterministic duplicate calendar event resolution; DRY RUN unless --execute")
    duplicates.add_argument("--date", type=parse_date,
                            help="one business day; omit with --all for the whole registry")
    duplicates.add_argument("--all", action="store_true",
                            help="scan every duplicate group in the registry (read-only)")
    duplicates.add_argument("--lecture-id",
                            help="resolve one occurrence only")
    duplicates.add_argument("--execute", action="store_true",
                            help="persist suppressions; without it nothing is written")
    duplicates.add_argument("--json", action="store_true")

    pipeline_state = commands.add_parser(
        "pipeline-state", help="the full stage matrix and next action for one lecture")
    pipeline_state.add_argument("--lecture-id", required=True)
    pipeline_state.add_argument("--probe-attendance", action="store_true")
    pipeline_state.add_argument("--json", action="store_true")

    cycle = commands.add_parser(
        "scheduler-cycle",
        help="run ONE cycle through the scheduled entrypoint")
    cycle.add_argument("--dry-run", action="store_true")
    cycle.add_argument("--force", action="store_true",
                       help="run the cycle while SCHEDULER_ENABLED is false; enables nothing")
    cycle.add_argument("--date", type=parse_date,
                       help="override the business day the cycle would pick")
    cycle.add_argument("--json", action="store_true")

    daemon = commands.add_parser(
        "scheduler-daemon",
        help="run cycles on the configured cron until stopped (the container option)")
    daemon.add_argument("--max-cycles", type=int, default=None,
                        help="stop after N cycles; omit to run until stopped")
    daemon.add_argument("--json", action="store_true")

    # QA Core RC2: Operations Backfill. The runner is a SECOND long-running
    # service rather than a branch inside the scheduler daemon: that daemon's
    # whole design is "sleep until the next cron instant", and a backfill run
    # must start within seconds of an operator pressing Start. Rewriting its
    # sleep into a poll loop would put the nightly 21:00/23:00 timing at risk
    # to save one container.
    backfill_runner = commands.add_parser(
        "backfill-runner",
        help="claim and process durable Operations Backfill runs until stopped")
    backfill_runner.add_argument(
        "--max-runs", type=int, default=None,
        help="stop after N backfill runs; omit to run until stopped")
    backfill_runner.add_argument(
        "--poll-seconds", type=int, default=20,
        help="how often to look for a newly requested backfill run")
    backfill_runner.add_argument("--json", action="store_true")

    backfill_preview = commands.add_parser(
        "backfill-preview",
        help="read-only: what a backfill of this date range would find")
    backfill_preview.add_argument("--from", dest="from_date", type=parse_date,
                                  required=True)
    backfill_preview.add_argument("--to", dest="to_date", type=parse_date,
                                  required=True)
    backfill_preview.add_argument(
        "--no-discover", action="store_true",
        help="skip the calendar read and report registry state only")
    backfill_preview.add_argument("--json", action="store_true")

    scheduler_status = commands.add_parser(
        "scheduler-status", help="print the scheduler configuration and safety precheck")
    scheduler_status.add_argument("--json", action="store_true")

    ops = commands.add_parser(
        "operations", help="read-only Operations interfaces (future UI backend)")
    ops.add_argument("--view", required=True, choices=(
        "recent-runs", "run-detail", "day", "lecture", "pending-attendance",
        "review-required", "errors", "lecture-history"))
    ops.add_argument("--date", type=parse_date)
    ops.add_argument("--lecture-id")
    ops.add_argument("--run-id")
    ops.add_argument("--limit", type=int, default=20)
    ops.add_argument("--json", action="store_true")

    seam = commands.add_parser("rebuild-seam-document")
    seam.add_argument("--lecture-id", required=True,
                      help="the single lecture to reparse from its selected parts")
    seam.add_argument("--dry-run", action="store_true")
    seam.add_argument("--json", action="store_true")
    withdraw = commands.add_parser(
        "withdraw-legacy-qa",
        help="withdraw ONE coded-owned legacy QA row contradicted by a coverage review")
    withdraw.add_argument("--lecture-id", required=True,
                          help="the single lecture whose coded-owned row is withdrawn")
    withdraw.add_argument("--confirm-write", action="store_true",
                          help="actually delete; without it the run only plans")
    withdraw.add_argument("--json", action="store_true")
    select = commands.add_parser("select-lecture")
    select.add_argument("--date", required=True, type=parse_date)
    select.add_argument("--subject", required=True)
    select.add_argument("--start", type=parse_start)
    select.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    settings = Settings.from_environment()
    try:
        if args.command == "discover-day":
            settings.require_discovery()
            client = build_graph_client(settings)
            service = LectureDiscoveryService(
                calendar=CalendarGateway(client, settings.calendar_user_upn),
                meetings=OnlineMeetingGateway(client, settings.calendar_user_upn),
                aptem_repository=AptemRepository(),
                lecture_repository=LectureSessionRepository(),
                run_repository=DiscoveryRunRepository(),
                qa_validation_repository=QaValidationRepository(),
            )
            with database_connection(settings.database_url) as kbc_connection:
                with readonly_database_connection(settings.aptem_database_url) as aptem_connection:
                    summary = service.discover_day(
                        kbc_connection, aptem_connection, args.date, persist=not args.dry_run
                    )
        elif args.command == "acquire-transcripts":
            settings.require_discovery()
            client = build_graph_client(settings)
            service = TranscriptAcquisitionService(
                gateway=TranscriptGateway(client),
                lecture_repository=ReadyLectureRepository(),
                artifact_repository=TranscriptArtifactRepository(),
                run_repository=TranscriptAcquisitionRunRepository(),
                qa_evidence_repository=LegacyQaEvidenceRepository(),
            )
            with database_connection(settings.database_url) as connection:
                summary = service.acquire_day(
                    connection, args.date, persist=not args.dry_run,
                    fetch_content=not args.list_only,
                )
                if args.dry_run:
                    connection.rollback()
        elif args.command == "select-transcripts":
            # Phase 2B is entirely DB-backed: no Graph client is built at all.
            settings.require_database()
            service = TranscriptSelectionService(
                lecture_repository=ReadyLectureRepository(),
                selection_repository=TranscriptSelectionRepository(),
                run_repository=TranscriptSelectionRunRepository(),
                qa_evidence_repository=LegacyQaEvidenceRepository(),
            )
            with database_connection(settings.database_url) as connection:
                summary = service.select_day(
                    connection, args.date, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "parse-transcripts":
            # Phase 2C1 is entirely DB-backed: no Graph client is built at all.
            settings.require_database()
            service = CanonicalTranscriptService(
                document_repository=TranscriptDocumentRepository(),
                run_repository=TranscriptParseRunRepository(),
            )
            with database_connection(settings.database_url) as connection:
                summary = service.parse_day(
                    connection, args.date, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "build-speaker-inventory":
            # Phase 2C2 is entirely DB-backed: no Graph client is built at all.
            settings.require_database()
            service = SpeakerInventoryService(
                speaker_repository=TranscriptSpeakerRepository(),
                run_repository=SpeakerInventoryRunRepository(),
                legacy_diagnostic_repository=LegacyTrainerDiagnosticRepository(),
                parser_version=args.parser_version,
            )
            with database_connection(settings.database_url) as connection:
                summary = service.build_day(
                    connection, args.date, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "resolve-speakers":
            # Phase 2C3 is entirely DB-backed: no Graph client is built at all.
            settings.require_database()
            service = SpeakerResolutionService(
                inventory_repository=SpeakerInventoryReadRepository(),
                attendance_repository=AttendanceSourceRepository(),
                snapshot_repository=AttendanceSnapshotRepository(),
                identity_repository=SpeakerIdentityRepository(),
                role_repository=SpeakerRoleRepository(),
                run_repository=SpeakerResolutionRunRepository(),
                legacy_diagnostic_repository=LegacyTrainerDiagnosticRepository(),
                attendance_resolution_version=args.roster_version,
                parser_version=args.parser_version,
            )
            with database_connection(settings.database_url) as connection:
                summary = service.resolve_day(
                    connection, args.date, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "calculate-engagement":
            # Phase 2C4 reads only persisted Phase 2C3 evidence: no Graph
            # client and no live attendance query.
            settings.require_database()
            service = EngagementService(
                input_repository=EngagementInputRepository(),
                engagement_repository=EngagementRepository(),
                run_repository=EngagementRunRepository(),
                legacy_parity_repository=LegacyEngagementParityRepository(),
                attendance_resolution_version=args.roster_version,
            )
            with database_connection(settings.database_url) as connection:
                if args.lecture_id:
                    summary = service.calculate_lecture(
                        connection, args.lecture_id, persist=not args.dry_run)
                else:
                    summary = service.calculate_day(
                        connection, args.date, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "run-qa-shadow":
            # Phase 3A reads only persisted platform evidence. No Graph client,
            # no live attendance query, and no legacy QA write.
            settings.require_database()
            provider = None
            if args.execute and settings.qa_model_api_key:
                provider = OpenAIChatProvider(
                    api_key=settings.qa_model_api_key, model=settings.qa_model_name,
                    base_url=settings.qa_model_base_url,
                    response_contract=args.provider_contract)
            service = ShadowQaService(
                input_repository=QaInputRepository(),
                evaluation_repository=QaEvaluationRepository(),
                run_repository=QaRunRepository(),
                legacy_repository=LegacyQaComparisonRepository(),
                provider=provider,
                attempt_repository=GenerationAttemptRepository(),
                resolver_version=RESOLVER_VERSION,
                role_algorithm_version=ROLE_ALGORITHM_VERSION,
                engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
                model_name=settings.qa_model_name,
                parser_version=args.parser_version,
                provider_contract_version=args.provider_contract,
                lecture_ids=args.lecture_ids,
            )
            with database_connection(settings.database_url) as connection:
                summary = service.run_day(connection, args.date,
                                          execute=args.execute, force=args.force)
                if not args.execute:
                    connection.rollback()
        elif args.command == "render-qa-output":
            # Phase 3B renders persisted evidence. No Graph client, no model
            # provider, and no legacy QA write.
            settings.require_database()
            service = QaRenderingService(
                input_repository=RenderInputRepository(),
                lms_source_repository=LmsSourceRepository(),
                lms_snapshot_repository=LmsSnapshotRepository(),
                output_repository=RenderedOutputRepository(),
                run_repository=RenderRunRepository(),
                legacy_repository=LegacyRenderComparisonRepository(),
                refresh_lms=args.refresh_lms,
                lecture_ids=args.lecture_ids,
            )
            with database_connection(settings.database_url) as connection:
                summary = service.render_day(connection, args.date)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "withdraw-legacy-qa":
            # Plans by default. Only an explicit confirmation reaches a
            # write-enabled mode, and the writer still refuses unless its own
            # ownership ledger, the current evaluation and the other systems'
            # columns all agree the row may go.
            settings.require_database()
            writer = LegacyQaWriter(
                payload_repository=RenderedPayloadRepository(),
                legacy_repository=LegacyQaTargetRepository(),
                ownership_repository=WriterOwnershipRepository(),
                mode=EXPLICIT_BACKFILL if args.confirm_write else DRY_RUN,
                lecture_ids=[args.lecture_id], confirmed=args.confirm_write)
            with database_connection(settings.database_url) as connection:
                summary = writer.withdraw_contradicted_session(connection, args.lecture_id)
                if not writer.writes_enabled:
                    connection.rollback()
        elif args.command == "rebuild-seam-document":
            # Phase 3C2.3B: reparse ONE lecture from its Phase 2B selected parts
            # under the seam-dedup version. Reads raw artifacts; writes only a
            # new versioned document and its cues.
            settings.require_database()
            service = SeamDedupService(document_repository=TranscriptDocumentRepository())
            with database_connection(settings.database_url) as connection:
                summary = service.rebuild_lecture(
                    connection, args.lecture_id, persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
        elif args.command == "write-legacy-qa":
            # Phase 3C1 is dry-run by default; a write-enabled mode must be
            # chosen explicitly and, for backfill, scoped and authorised.
            settings.require_database()
            # Phase 3C2 canary guard: a write-enabled mode must name exactly
            # one lecture and carry an explicit confirmation. Without both, the
            # run is downgraded to a plan rather than refused silently.
            guard_write_command(args)
            perfect_planner = None if args.skip_perfect_lecture else PerfectLecturePlanner(
                result_repository=PerfectLectureResultRepository(),
                ownership_repository=PerfectLectureOwnershipRepository(),
                legacy_repository=LegacyPerfectLectureRepository(),
                mode=args.mode, lecture_ids=args.lecture_ids,
                confirmed=args.confirm_write,
                eligibility_version=args.perfect_policy,
                coverage_repository=AttendanceCoverageRepository(),
                allow_update_existing=args.allow_update_existing,
                # The coded shadow result is persisted only when the same run
                # is authorised to write, so a dry run stays a pure read.
                persist_shadow_result=False)
            writer = LegacyQaWriter(
                payload_repository=RenderedPayloadRepository(),
                legacy_repository=LegacyQaTargetRepository(),
                ownership_repository=WriterOwnershipRepository(),
                mode=args.mode, allow_update_existing=args.allow_update_existing,
                lecture_ids=args.lecture_ids, confirmed=args.confirm_write,
                perfect_planner=perfect_planner)
            with database_connection(settings.database_url) as connection:
                summary = writer.plan_day(connection, args.date)
                if not writer.writes_enabled:
                    # A planning run must leave no trace at all.
                    connection.rollback()
        elif args.command == "recover-attendance":
            # Phase 3C3E. Lecture-scoped, deterministic, and free: it re-reads
            # the external attendance source through the existing validated
            # contract and resumes only the stages that source can invalidate.
            # No Graph call, no provider call, no transcript read, and nothing
            # is ever written to public.kbc_attendance.
            settings.require_database()
            resolution = SpeakerResolutionService(
                inventory_repository=SpeakerInventoryReadRepository(),
                attendance_repository=AttendanceSourceRepository(),
                snapshot_repository=AttendanceSnapshotRepository(),
                identity_repository=SpeakerIdentityRepository(),
                role_repository=SpeakerRoleRepository(),
                run_repository=SpeakerResolutionRunRepository(),
                legacy_diagnostic_repository=None)
            engagement = EngagementService(
                input_repository=EngagementInputRepository(),
                engagement_repository=EngagementRepository(),
                run_repository=EngagementRunRepository(),
                legacy_parity_repository=None,
                attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
                resolver_version=RESOLVER_VERSION,
                role_algorithm_version=ROLE_ALGORITHM_VERSION)
            qa = ShadowQaService(
                input_repository=QaInputRepository(),
                evaluation_repository=QaEvaluationRepository(),
                run_repository=QaRunRepository(),
                legacy_repository=None,
                # Deliberately no provider: attendance-only recovery must be
                # structurally incapable of buying a generation, not merely
                # instructed not to.
                provider=None,
                attempt_repository=GenerationAttemptRepository(),
                resolver_version=RESOLVER_VERSION,
                role_algorithm_version=ROLE_ALGORITHM_VERSION,
                engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
                model_name=settings.qa_model_name)
            planner = PerfectLecturePlanner(
                result_repository=PerfectLectureResultRepository(),
                ownership_repository=PerfectLectureOwnershipRepository(),
                legacy_repository=LegacyPerfectLectureRepository(),
                coverage_repository=AttendanceCoverageRepository(),
                mode=DRY_RUN, persist_shadow_result=not args.dry_run)
            def _render_for(lecture_id):
                # Scoped at construction, so Phase 3B cannot reach a sibling
                # even though its entry point takes a date.
                return QaRenderingService(
                    input_repository=RenderInputRepository(),
                    lms_source_repository=LmsSourceRepository(),
                    lms_snapshot_repository=LmsSnapshotRepository(),
                    output_repository=RenderedOutputRepository(),
                    run_repository=RenderRunRepository(),
                    legacy_repository=None,
                    refresh_lms=False, lecture_ids=[lecture_id])

            service = AttendanceRecoveryService(
                resolution_service=resolution, engagement_service=engagement,
                qa_service=qa, coverage_repository=AttendanceCoverageRepository(),
                perfect_planner=planner,
                payload_repository=RenderedPayloadRepository(),
                renderer_version=RENDERER_VERSION,
                rendering_service_factory=_render_for)
            with database_connection(settings.database_url) as connection:
                summary = service.recover(connection, args.lecture_id,
                                          persist=not args.dry_run)
                if args.dry_run:
                    connection.rollback()
                else:
                    connection.commit()
        elif args.command == "backfill-coverage-metadata":
            # Coded metadata hardening only. It derives conclusions from counts
            # already frozen on the rows, and touches no legacy table.
            settings.require_database()

            def _qa_service(punctuality_version, contract_version, parser_version):
                return ShadowQaService(
                    input_repository=QaInputRepository(),
                    evaluation_repository=QaEvaluationRepository(),
                    run_repository=QaRunRepository(), provider=None,
                    resolver_version=RESOLVER_VERSION,
                    role_algorithm_version=ROLE_ALGORITHM_VERSION,
                    engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
                    model_name=settings.qa_model_name,
                    punctuality_source_version=punctuality_version,
                    provider_contract_version=contract_version,
                    parser_version=parser_version)

            with database_connection(settings.database_url) as connection:
                summary = CoverageBackfill().run(connection, persist=not args.dry_run)
                if not args.skip_model_input_fingerprints:
                    summary["model_input_fingerprints"] = (
                        ModelInputFingerprintBackfill(service_factory=_qa_service)
                        .run(connection, persist=not args.dry_run))
                if args.dry_run:
                    connection.rollback()
                else:
                    connection.commit()
        elif args.command == "qa-recovery-state":
            # Read-only, except for the explicitly requested aggregate repair,
            # which never touches an attempt row or any legacy table.
            settings.require_database()
            with database_connection(settings.database_url) as connection:
                reconciled = []
                if args.reconcile_attempts:
                    before = recovery_state(connection, args.lecture_id)
                    repository = QaEvaluationRepository()
                    for evaluation in before["evaluations"]:
                        outcome = repository.sync_provider_attempts(
                            connection, evaluation["source_fingerprint"])
                        if outcome is not None:
                            reconciled.append({
                                "evaluation_id": str(outcome["evaluation_id"]),
                                "source_fingerprint_prefix":
                                    evaluation["source_fingerprint"][:16],
                                "provider_attempts_before": outcome["previous"],
                                "provider_attempts_after": outcome["current"]})
                summary = recovery_state(connection, args.lecture_id)
                summary["attempts_reconciled"] = reconciled
                if args.reconcile_attempts:
                    connection.commit()
                else:
                    connection.rollback()
        elif args.command == "run-pipeline":
            # Phase 4A. Stage-aware, RETRY semantics only: it resumes from the
            # earliest missing or stale stage and can never force-reprocess a
            # stage that is already COMPLETE.
            settings.require_database()
            if args.discover and not args.dry_run:
                settings.require_discovery()
            orchestrator = build_orchestrator(
                settings, dry_run=args.dry_run,
                allow_graph=not args.no_graph, allow_provider=not args.no_provider,
                discover=args.discover, max_passes=args.max_passes)

            def _aptem():
                return readonly_database_connection(settings.aptem_database_url)

            with database_connection(settings.database_url) as connection:
                summary = orchestrator.run_window(
                    connection, args.date, lookback_days=args.lookback_days,
                    run_type=RUN_TYPE_MANUAL, dry_run=args.dry_run,
                    discover=args.discover and not args.dry_run,
                    aptem_connection_factory=(
                        _aptem if args.discover and not args.dry_run else None))
                if args.dry_run:
                    connection.rollback()
                else:
                    connection.commit()
        elif args.command == "resolve-duplicates":
            settings.require_database()
            service = DuplicateResolutionService()
            if not args.execute:
                # `--all` is a read-only historical scan on purpose: the
                # decision to change historical rows is a person's, taken
                # after reading what would change.
                with readonly_database_connection(settings.database_url) as connection:
                    if args.lecture_id:
                        summary = service.resolve_lecture(
                            connection, args.lecture_id, persist=False)
                    elif args.all:
                        summary = service.plan_all(connection)
                    else:
                        summary = service.plan_day(connection, args.date)
                summary["persisted"] = False
            elif args.all:
                raise SystemExit(
                    "--all is read-only; suppress a specific day or lecture instead")
            else:
                with database_connection(settings.database_url) as connection:
                    if args.lecture_id:
                        summary = service.resolve_lecture(
                            connection, args.lecture_id, persist=True)
                    else:
                        summary = service.resolve_day(
                            connection, args.date, persist=True)
                    connection.commit()
        elif args.command == "reconcile-day":
            settings.require_database()
            if args.execute:
                orchestrator = build_orchestrator(settings, dry_run=False)
                with database_connection(settings.database_url) as connection:
                    summary = orchestrator.run_window(
                        connection, args.date, lookback_days=args.lookback_days,
                        run_type=RUN_TYPE_RECONCILE)
                    connection.commit()
                    reconciliation = DayReconciliation(
                        resolver=orchestrator.resolver)
                    summary["reconciliation"] = reconciliation.for_window(
                        connection, [parse_date(day)
                                     for day in summary["window_days"]])
            else:
                resolver = build_resolver(probe=args.probe_attendance)
                with readonly_database_connection(settings.database_url) as connection:
                    days = [args.date - timedelta(days=offset)
                            for offset in range(args.lookback_days, -1, -1)]
                    summary = DayReconciliation(resolver=resolver).for_window(
                        connection, days)
        elif args.command == "pipeline-state":
            settings.require_database()
            resolver = build_resolver(probe=args.probe_attendance)
            with readonly_database_connection(settings.database_url) as connection:
                summary = resolver.for_lecture(connection, args.lecture_id)
        elif args.command == "scheduler-cycle":
            settings.require_database()
            scheduler = build_scheduler(settings, dry_run=args.dry_run)
            now = (datetime.combine(args.date, datetime.min.time(),
                                    tzinfo=scheduler.config.zone)
                   if args.date else None)
            # Discovery reads the separate Aptem source database. A scheduled
            # cycle that cannot open it would fail at the first new lecture, so
            # the connection is opened up front rather than discovered missing.
            discovering = scheduler.config.discover and not args.dry_run
            if discovering:
                settings.require_discovery()

            def _aptem():
                return readonly_database_connection(settings.aptem_database_url)

            with database_connection(settings.database_url) as connection:
                try:
                    summary = scheduler.run_cycle(
                        connection, now=now,
                        aptem_connection_factory=_aptem if discovering else None,
                        dry_run=args.dry_run, force=args.force)
                except SchedulerDisabled as exc:
                    summary = {"status": "SCHEDULER_DISABLED", "error": str(exc),
                               "scheduler": scheduler.config.describe(),
                               "lectures_processed": 0}
                if args.dry_run:
                    connection.rollback()
                else:
                    connection.commit()
        elif args.command == "scheduler-daemon":
            # The long-running option. It owns no orchestration logic: every
            # cycle goes through the same SchedulerService the CLI and the
            # validated pilots use.
            settings.require_database()
            scheduler = build_scheduler(settings)
            if scheduler.config.discover:
                settings.require_discovery()

            def _open():
                return database_connection(settings.database_url)

            def _aptem():
                return readonly_database_connection(settings.aptem_database_url)

            service = SchedulerDaemon(
                scheduler=scheduler, connection_factory=_open,
                aptem_connection_factory=_aptem if scheduler.config.discover else None)
            service.install_signal_handlers()
            summary = {"scheduler": scheduler.config.describe(),
                       **service.run_forever(max_cycles=args.max_cycles)}
        elif args.command == "backfill-runner":
            # The durable historical-recovery loop. It owns no pipeline logic:
            # every day goes through the same PipelineOrchestrator.run_window
            # the nightly scheduler calls.
            settings.require_database()
            settings.require_discovery()
            from app.orchestration.factory import build_backfill_runner

            def _open():
                return database_connection(settings.database_url)

            def _aptem():
                return readonly_database_connection(settings.aptem_database_url)

            def _read_only():
                return readonly_database_connection(settings.database_url)

            runner = build_backfill_runner(
                settings, connection_factory=_open,
                readonly_connection_factory=_read_only,
                aptem_connection_factory=_aptem)
            import signal as _signal
            for _name in ("SIGTERM", "SIGINT"):
                _handler = getattr(_signal, _name, None)
                if _handler is not None:
                    # Finish the day in flight, then exit. The run stays
                    # claimable and resumes from its persisted checkpoint.
                    _signal.signal(_handler, runner.request_stop)
            summary = {"service": "backfill_runner",
                       "runner_version": runner.runner_version,
                       **runner.run_forever(poll_seconds=args.poll_seconds,
                                            max_runs=args.max_runs)}
        elif args.command == "backfill-preview":
            settings.require_database()
            from app.orchestration.backfill import validate_range
            from app.orchestration.factory import build_backfill_preview

            validate_range(args.from_date, args.to_date)
            discover = not args.no_discover
            if discover:
                settings.require_discovery()
            service = build_backfill_preview(settings, discover=discover)

            def _aptem():
                return readonly_database_connection(settings.aptem_database_url)

            # A read-only transaction: the preview cannot write even if some
            # future change to a shared component tried to.
            with readonly_database_connection(settings.database_url) as connection:
                summary = service.preview(
                    connection, args.from_date, args.to_date,
                    aptem_connection_factory=_aptem if discover else None,
                    discover=discover).as_dict()
        elif args.command == "scheduler-status":
            scheduler = build_scheduler(settings, dry_run=True)
            from datetime import datetime as _dt
            summary = {"scheduler": scheduler.config.describe(),
                       "legacy_qa_precheck": scheduler.orchestrator.preflight.check(),
                       "next_window": [day.isoformat()
                                       for day in scheduler.config.window()],
                       # What the schedule actually means, resolved. A cron
                       # string nobody has evaluated is a guess.
                       "cron_fields": parse_cron(scheduler.config.cron),
                       "next_fire_at": next_fire_time(
                           scheduler.config.cron,
                           after=_dt.now(scheduler.config.zone)).isoformat(),
                       "runtime": {
                           "environment_variable_alone_schedules_nothing": True,
                           "windows_task": "automation/scheduler/install-windows-task.ps1",
                           "container": "automation/scheduler/docker-compose.yml",
                           "entrypoint": "app.cli.main scheduler-cycle"}}
        elif args.command == "operations":
            settings.require_database()
            operations = build_operations()
            with readonly_database_connection(settings.database_url) as connection:
                if args.view == "recent-runs":
                    summary = {"runs": operations.recent_runs(connection, args.limit)}
                elif args.view == "run-detail":
                    summary = operations.run_detail(connection, args.run_id)
                elif args.view == "day":
                    summary = operations.day_reconciliation(connection, args.date)
                elif args.view == "lecture":
                    summary = operations.lecture_stage_matrix(connection,
                                                              args.lecture_id)
                elif args.view == "lecture-history":
                    summary = {"lecture_id": args.lecture_id,
                               "history": operations.lecture_run_history(
                                   connection, args.lecture_id, args.limit)}
                elif args.view == "pending-attendance":
                    summary = {"lectures": operations.pending_attendance(
                        connection, args.date)}
                elif args.view == "review-required":
                    summary = {"lectures": operations.review_required(
                        connection, args.date)}
                else:
                    summary = {"lectures": operations.error_reasons(connection,
                                                                    args.date)}
        elif args.command == "evaluate-perfect-lecture":
            # Read-only by default: eligibility is derived from the frozen
            # Phase 3A/3B result and never touches a legacy table in any mode.
            settings.require_database()
            planner = PerfectLecturePlanner(
                result_repository=PerfectLectureResultRepository(),
                ownership_repository=PerfectLectureOwnershipRepository(),
                legacy_repository=LegacyPerfectLectureRepository(),
                mode=DRY_RUN, lecture_ids=args.lecture_ids,
                eligibility_version=args.perfect_policy,
                coverage_repository=AttendanceCoverageRepository(),
                persist_shadow_result=args.persist)
            with database_connection(settings.database_url) as connection:
                summary = plan_perfect_for_day(
                    planner, connection, RenderedPayloadRepository(), args.date,
                    RENDERER_VERSION)
                if not args.persist:
                    connection.rollback()
        else:
            settings.require_database()
            with database_connection(settings.database_url) as connection:
                rows = LectureSessionRepository().select_exact(connection, args.date, args.subject)
                connection.rollback()
            summary = select_single_lecture(rows, args.start)
        print(json.dumps(summary, default=json_default, indent=2 if args.json else None))
        return 0
    except PlatformError as exc:
        payload = {"status": "ERROR", "error_code": exc.code, "error": str(exc)}
        if getattr(exc, "http_status", None) is not None:
            payload["http_status"] = exc.http_status
        if getattr(exc, "provider_code", None):
            payload["provider_code"] = exc.provider_code
        if getattr(exc, "diagnostic_reason", None):
            payload["diagnostic_reason"] = exc.diagnostic_reason
        print(json.dumps(payload))
        return 2
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
