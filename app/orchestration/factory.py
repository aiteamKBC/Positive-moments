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
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.runner import StageRunner, attendance_probe_factory
from app.orchestration.scheduler import SchedulerConfig, SchedulerService
from app.orchestration.state import PipelineStateResolver


def build_resolver(*, probe: bool = False, perfect_eligibility_version=None):
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
        attendance_probe=attendance_probe, **extra)


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
        qa_validation_repository=QaValidationRepository())


def build_orchestrator(settings, *, dry_run: bool = False, allow_graph: bool = True,
                       allow_provider: bool = True, allow_legacy_writes: bool = True,
                       discover: bool = False,
                       perfect_eligibility_version=None,
                       provider_contract_version=None, max_passes: int = 8,
                       probe_attendance: bool | None = None):
    """
    Assemble the orchestrator.

    A dry run gets: no run repository (it writes nothing, including no audit
    row), no lock manager (it holds nothing because it changes nothing), a
    non-required n8n precheck, and a runner with `persist=False`. Those are
    four independent reasons a dry run cannot write, which is the right number
    for a feature whose entire promise is that it does not.
    """
    if probe_attendance is None:
        probe_attendance = dry_run
    resolver = build_resolver(probe=probe_attendance,
                              perfect_eligibility_version=perfect_eligibility_version)
    runner = StageRunner(
        settings=settings, allow_graph=allow_graph and not dry_run,
        allow_provider=allow_provider and not dry_run,
        allow_legacy_writes=allow_legacy_writes and not dry_run,
        perfect_eligibility_version=resolver.perfect_eligibility_version,
        provider_contract_version=provider_contract_version, persist=not dry_run)
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


def build_operations(*, perfect_eligibility_version=None) -> OperationsService:
    resolver = build_resolver(perfect_eligibility_version=perfect_eligibility_version)
    return OperationsService(
        resolver=resolver, run_repository=PipelineRunRepository(),
        reconciliation=DayReconciliation(resolver=resolver),
        directory_repository=LectureDirectoryRepository(),
        media_repository=MediaStateRepository(),
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
    resolver = build_resolver(perfect_eligibility_version=perfect_eligibility_version)

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
            perfect_eligibility_version=perfect_eligibility_version),
        orchestrator_factory=orchestrator_factory)
