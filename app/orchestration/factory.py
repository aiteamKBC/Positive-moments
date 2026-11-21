"""
Phase 4A: one place that assembles the orchestration stack.

The CLI and the scheduler must get IDENTICAL objects, and the only way to
guarantee that is for both to call the same builder. Two call sites that each
construct their own orchestrator will agree on the day they are written and
disagree by the time anybody notices.
"""
from app.db.repositories.aptem import AptemRepository
from app.db.repositories.discovery_runs import DiscoveryRunRepository
from app.db.repositories.lecture_sessions import LectureSessionRepository
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.lecture_directory import LectureDirectoryRepository
from app.db.repositories.media_state import MediaStateRepository
from app.db.repositories.pipeline_runs import PipelineRunRepository
from app.db.repositories.qa_validation import QaValidationRepository
from app.orchestration.locks import (
    LectureLockManager,
    NullCycleLock,
    NullLockManager,
    SchedulerCycleLock,
)
from app.orchestration.actions import GuardedActionService
from app.orchestration.n8n_preflight import LegacyQaPreflight, N8nReadOnlyGateway
from app.orchestration.operations import OperationsService
from app.orchestration.orchestrator import (
    DEFAULT_MAX_PASSES,
    PipelineOrchestrator,
)
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.runner import StageRunner, attendance_probe_factory
from app.orchestration.scheduler import SchedulerConfig, SchedulerService
from app.orchestration.state import PipelineStateResolver


def build_resolver(*, probe: bool = False, perfect_eligibility_version=None,
                   recording_link_mode: str = "observe"):
    """
    The state resolver, optionally able to notice that attendance has arrived.

    The probe is off by default so that the plain resolver reads coded tables
    and nothing else. Turning it on costs one read-only Phase 2C3 resolution
    per waiting lecture and is what lets a dry run say RECOVER_ATTENDANCE
    instead of WAIT_FOR_ATTENDANCE_SOURCE.
    """
    attendance_probe = None
    if probe:
        runner = StageRunner(settings=None, persist=False)
        attendance_probe = attendance_probe_factory(runner._resolution_service())
    extra = ({"perfect_eligibility_version": perfect_eligibility_version}
             if perfect_eligibility_version else {})
    return PipelineStateResolver(
        legacy_observations=LegacyObservationRepository(),
        attendance_probe=attendance_probe,
        recording_link_mode=recording_link_mode, **extra)


def _recording_link_mode(settings) -> str:
    """The scheduler, backfill and CLI all read the one switch the same way."""
    return getattr(settings, "recording_link_mode", "observe") or "observe"


def build_discovery_service(settings):
    from app.graph.auth import build_graph_client
    from app.graph.calendar import CalendarGateway
    from app.graph.meetings import OnlineMeetingGateway
    from app.lectures.service import LectureDiscoveryService
    client = build_graph_client(settings)
    return LectureDiscoveryService(
        calendar=CalendarGateway(client, settings.calendar_user_upn),
        meetings=OnlineMeetingGateway(client, settings.calendar_user_upn),
        aptem_repository=AptemRepository(),
        lecture_repository=LectureSessionRepository(),
        run_repository=DiscoveryRunRepository(),
        qa_validation_repository=QaValidationRepository(),
        lms_repository=_lms_repository(settings))


def _lms_repository(settings):
    from app.db.repositories.lms import LmsModuleRepository
    url = getattr(settings, "lms_database_url", "")
    return LmsModuleRepository(url) if url else None


def build_orchestrator(settings, *, dry_run: bool = False, allow_graph: bool = True,
                       allow_provider: bool = True, allow_legacy_writes: bool = True,
                       discover: bool = False,
                       perfect_eligibility_version=None,
                       provider_contract_version=None,
                       max_passes: int = DEFAULT_MAX_PASSES,
                       probe_attendance: bool | None = None,
                       lecture_scope=None):
    """
    Assemble the orchestrator.

    A dry run gets: no run repository (it writes nothing, including no audit
    row), no lock manager (it holds nothing because it changes nothing), a
    non-required n8n precheck, and a runner with `persist=False`. Those are
    four independent reasons a dry run cannot write, which is the right number
    for a feature whose entire promise is that it does not.

    `lecture_scope` narrows the runner's day-scoped transcript services to
    those lectures (see StageRunner); every existing caller leaves it None.
    """
    if probe_attendance is None:
        probe_attendance = dry_run
    resolver = build_resolver(probe=probe_attendance,
                              perfect_eligibility_version=perfect_eligibility_version,
                              recording_link_mode=_recording_link_mode(settings))
    runner = StageRunner(
        settings=settings, allow_graph=allow_graph and not dry_run,
        allow_provider=allow_provider and not dry_run,
        allow_legacy_writes=allow_legacy_writes and not dry_run,
        perfect_eligibility_version=resolver.perfect_eligibility_version,
        provider_contract_version=provider_contract_version, persist=not dry_run,
        lecture_scope=lecture_scope)
    preflight = LegacyQaPreflight(gateway=N8nReadOnlyGateway.from_environment(),
                                  required=not dry_run)
    discovery = None
    if discover and not dry_run:
        discovery = build_discovery_service(settings)
    return PipelineOrchestrator(
        resolver=resolver, runner=runner,
        run_repository=None if dry_run else PipelineRunRepository(),
        lock_manager=NullLockManager() if dry_run else LectureLockManager(),
        cycle_lock=NullCycleLock() if dry_run else SchedulerCycleLock(),
        preflight=preflight, discovery_service=discovery, max_passes=max_passes)


def build_scheduler(settings, *, config: SchedulerConfig | None = None,
                    dry_run: bool = False):
    config = config or SchedulerConfig.from_environment()
    orchestrator = build_orchestrator(
        settings, dry_run=dry_run, allow_graph=config.allow_graph,
        allow_provider=config.allow_provider,
        allow_legacy_writes=config.allow_legacy_writes,
        discover=config.discover, max_passes=config.max_passes)
    return SchedulerService(orchestrator=orchestrator, config=config)


def build_operations(*, perfect_eligibility_version=None,
                     recording_link_mode: str = "observe") -> OperationsService:
    resolver = build_resolver(perfect_eligibility_version=perfect_eligibility_version,
                              recording_link_mode=recording_link_mode)
    from app.db.repositories.recording_links import RecordingLinkRepository
    return OperationsService(
        resolver=resolver, run_repository=PipelineRunRepository(),
        reconciliation=DayReconciliation(resolver=resolver),
        directory_repository=LectureDirectoryRepository(),
        media_repository=MediaStateRepository(),
        recording_links=RecordingLinkRepository(),
        preflight=LegacyQaPreflight(gateway=N8nReadOnlyGateway.from_environment(),
                                    required=True))


def build_guarded_actions(settings, *, perfect_eligibility_version=None):
    """
    Phase 5B: the three operator actions, wired to the REAL orchestrator.

    The orchestrator is built per call rather than once, because recovering
    attendance needs one with the provider switched off. Sharing a single
    instance would mean the attendance button inherited whatever gates the
    retry button happened to need.
    """
    resolver = build_resolver(perfect_eligibility_version=perfect_eligibility_version,
                              recording_link_mode=_recording_link_mode(settings))

    def orchestrator_factory(*, allow_provider: bool = True):
        return build_orchestrator(
            settings, dry_run=False, allow_provider=allow_provider,
            # Discovery is a day-wide Graph sweep and belongs to the scheduler.
            # An operator retrying one lecture is not asking for it.
            discover=False,
            perfect_eligibility_version=perfect_eligibility_version)

    return GuardedActionService(
        resolver=resolver,
        operations=build_operations(
            perfect_eligibility_version=perfect_eligibility_version,
            recording_link_mode=_recording_link_mode(settings)),
        orchestrator_factory=orchestrator_factory)


def build_manual_transcript_service(settings, *, allow_provider: bool = False,
                                    perfect_eligibility_version=None):
    """
    Manual transcript upload: validation, storage, and the lecture-scoped
    recovery that resumes the pipeline for that ONE lecture.

    The recovery orchestrator is the real one (run repository, lecture locks,
    cycle lock, required n8n precheck), built per call with its runner narrowed
    to the uploaded lecture. Discovery is off, as for a retry.

    `allow_provider` is False for the web console (a request timeout must
    never be able to discard a paid generation, see transcript_recovery.py)
    and True only for the server-side CLI command.
    """
    from app.orchestration.transcript_recovery import ManualTranscriptRecovery
    from app.transcripts.manual_upload import ManualTranscriptUploadService

    def orchestrator_factory(lecture_id):
        return build_orchestrator(
            settings, dry_run=False, discover=False, allow_provider=allow_provider,
            perfect_eligibility_version=perfect_eligibility_version,
            lecture_scope=[str(lecture_id)])

    return ManualTranscriptRecovery(
        uploads=ManualTranscriptUploadService(),
        resolver=build_resolver(perfect_eligibility_version=perfect_eligibility_version,
                                recording_link_mode=_recording_link_mode(settings)),
        orchestrator_factory=orchestrator_factory, allow_provider=allow_provider)


def build_teams_transcript_retry(settings, *, worker: bool = False,
                                 perfect_eligibility_version=None):
    """
    Retry Teams transcript (app/orchestration/teams_transcript_retry.py).

    The console (`worker=False`) only plans and queues: no Graph client, no orchestrator. The
    background worker (`worker=True`) gets the Publisher's lecture-scoped Teams transcript worker
    on its own autocommit connection, and the same lecture-scoped orchestrator the manual
    transcript worker uses, with the QA provider allowed. The job holds the lecture's lock in its
    own transaction, so the Teams worker takes none on its (pooled) connection.
    """
    from app.orchestration.teams_transcript_retry import LockHeldByCaller, TeamsTranscriptRetry

    resolver = build_resolver(perfect_eligibility_version=perfect_eligibility_version,
                              recording_link_mode=_recording_link_mode(settings))
    if not worker:
        return TeamsTranscriptRetry(resolver=resolver)

    import psycopg

    from app.graph.auth import build_graph_client
    from app.publisher.config import DB_CONNECT_KWARGS
    from app.publisher.transcripts import TranscriptWorker
    from app.transcripts.graph import TranscriptGateway

    graph = build_graph_client(settings)
    teams = TranscriptWorker(
        connect=lambda: psycopg.connect(settings.database_url, prepare_threshold=None,
                                        **DB_CONNECT_KWARGS),
        gateway=TranscriptGateway(graph), graph_client=graph, lock_manager=LockHeldByCaller())

    def orchestrator_factory(lecture_id):
        return build_orchestrator(
            settings, dry_run=False, discover=False, allow_provider=True,
            perfect_eligibility_version=perfect_eligibility_version,
            lecture_scope=[str(lecture_id)])

    return TeamsTranscriptRetry(resolver=resolver, orchestrator_factory=orchestrator_factory,
                                fetch_transcript=teams.run)


# ---------------------------------------------------------------------------
# QA Core RC2: Operations Backfill
# ---------------------------------------------------------------------------

def build_backfill_preview(settings, *, discover: bool = True,
                           perfect_eligibility_version=None):
    """
    The read-only preview service.

    `probe=True` on the resolver is the same setting a dry run uses: it lets
    attendance report what it WOULD find without claiming a source it has not
    established. The preflight is `required=False` because a preview writes
    nothing, so an active legacy path is information to display, not a refusal.

    Discovery here is the production `LectureDiscoveryService`, called later
    with `persist=False`.
    """
    from app.orchestration.backfill import BackfillPreviewService

    return BackfillPreviewService(
        resolver=build_resolver(
            probe=True, perfect_eligibility_version=perfect_eligibility_version,
            recording_link_mode=_recording_link_mode(settings)),
        discovery_service=build_discovery_service(settings) if discover else None,
        preflight=LegacyQaPreflight(
            gateway=N8nReadOnlyGateway.from_environment(), required=False))


def build_backfill_recording_report(settings, *, graph_factory=None,
                                    perfect_eligibility_version=None):
    """
    Recording Links for Historical Backfill.

    PREVIEW days use the SAME live preview the `recording-links-preview` CLI
    builds (app/recordings/preview.build_preview): Graph reads through the
    platform's one app-only client, no publisher, and a resolver in `write`
    mode so it reports what the coded stage would do - including a retry not
    yet due or an earlier stage still pending. EXECUTE days read back what the
    orchestrator did through a resolver in the CONFIGURED mode.

    The Graph client is built in the backfill runner's process, which already
    owns MICROSOFT_GRAPH_* for discovery; nothing here needs the web process
    to hold a Graph credential.
    """
    from app.recordings.coverage import BackfillRecordingReport
    from app.recordings.preview import build_preview

    extra = ({"perfect_eligibility_version": perfect_eligibility_version}
             if perfect_eligibility_version else {})

    def preview_factory():
        graph = graph_factory() if graph_factory else None
        return build_preview(settings, resolver=build_resolver(
            recording_link_mode="write", **extra), live_graph=True, graph=graph)

    def resolver_factory():
        return build_resolver(recording_link_mode=_recording_link_mode(settings), **extra)

    return BackfillRecordingReport(mode=_recording_link_mode(settings),
                                   preview_factory=preview_factory,
                                   resolver_factory=resolver_factory)


def build_backfill_runner(settings, *, connection_factory,
                          aptem_connection_factory=None,
                          readonly_connection_factory=None,
                          perfect_eligibility_version=None):
    """
    The write-capable runner.

    It is handed a FULL production orchestrator - real run repository, real
    per-lecture locks, real cycle lock, required n8n preflight, discovery on.
    Backfill gets no privileged variant of the pipeline, and there is no
    parameter here that could grant it one.
    """
    from app.orchestration.backfill import BackfillRunner

    from app.orchestration.lecture_jobs import LectureJobWorker, nightly_quiet_window

    orchestrator = build_orchestrator(
        settings, dry_run=False, discover=True,
        perfect_eligibility_version=perfect_eligibility_version)
    # The same service works the per-lecture processing queue (migration 030): the lecture-scoped
    # recovery with the QA provider allowed - what `process-manual-transcript` runs - outside any
    # HTTP request, and never in the minutes before a nightly firing.
    from app.db.repositories.lecture_processing_jobs import REASON_TEAMS_TRANSCRIPT_RETRY

    lecture_jobs = LectureJobWorker(
        recovery=build_manual_transcript_service(
            settings, allow_provider=True,
            perfect_eligibility_version=perfect_eligibility_version),
        recoveries={REASON_TEAMS_TRANSCRIPT_RETRY: build_teams_transcript_retry(
            settings, worker=True, perfect_eligibility_version=perfect_eligibility_version)},
        connection_factory=connection_factory,
        in_quiet_window=nightly_quiet_window())
    return BackfillRunner(
        lecture_jobs=lecture_jobs,
        orchestrator=orchestrator,
        connection_factory=connection_factory,
        readonly_connection_factory=readonly_connection_factory,
        aptem_connection_factory=aptem_connection_factory,
        # The same runner handles PREVIEW runs, on a read-only connection.
        preview_service=build_backfill_preview(
            settings, perfect_eligibility_version=perfect_eligibility_version),
        recording_report=build_backfill_recording_report(
            settings, perfect_eligibility_version=perfect_eligibility_version))
