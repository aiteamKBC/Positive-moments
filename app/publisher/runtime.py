"""
Assemble the continuous publisher from configuration - the ONE place the CLI
and the systemd service get their objects from.

Write modes refuse to start unless Bunny's core API confirms the destination
library encodes on the FREE tier (no JIT / Premium) with AI transcribing
disabled - the same checks the pilot's preflight made - and the library is
not the old account's.
"""
from __future__ import annotations

import psycopg

from app.publisher.config import DB_CONNECT_KWARGS, FORBIDDEN_LIBRARY_IDS, ConfigError, PublisherConfig

FREE_TIERS = (0, "0", "Free", "free")


def check_library_settings(settings: dict) -> list[str]:
    """Problems with a Bunny library's settings for publishing; [] = acceptable."""
    problems = []
    if settings.get("EncodingTier") not in FREE_TIERS:
        problems.append(f"library EncodingTier is {settings.get('EncodingTier')!r}, not Free")
    if settings.get("JitEncodingEnabled"):
        problems.append("library has JIT encoding enabled")
    if any(settings.get(k) for k in ("EnableTranscribing", "EnableTranscribingTitleGeneration",
                                     "EnableTranscribingDescriptionGeneration")) \
            or settings.get("TranscribingCaptionLanguages"):
        problems.append("library has Bunny AI transcribing enabled")
    if "x264" not in [c.strip().lower() for c in str(settings.get("OutputCodecs") or "").split(",")]:
        problems.append("library has no H.264 (x264) output")
    return problems


def connector(config: PublisherConfig):
    # Bounded connect + TCP keepalives: a dropped Neon route fails fast instead of stalling a worker
    # (the 2026-10-23 pilot saw both workers wait ~2.5 min). Still the DIRECT endpoint (advisory locks).
    return lambda: psycopg.connect(config.database_url, prepare_threshold=None, **DB_CONNECT_KWARGS)


def build_daemon(config: PublisherConfig, *, graph=None, bunny=None, core=None, lms_source=None, calendar=None,
                 connect=None, clock=None):
    """Every dependency can be injected (tests); the defaults are the real clients."""
    from app.publisher.bunny_jobs import BunnyRuntime, CaptionWorker, EncodingWorker, ImportWorker
    from app.publisher.daemon import PublisherDaemon
    from app.publisher.factory import build_recording_worker
    from app.publisher.handlers import BunnyJobHandler, RecordingJobHandler, TranscriptJobHandler
    from app.publisher.planner import CalendarDiscovery, Planner
    from app.publisher.reconcile import Reconciler
    from app.publisher.transcripts import TranscriptWorker
    from app.transcripts.graph import TranscriptGateway

    config.validate()
    connect = connect or connector(config)
    if graph is None:
        from app.graph.auth import build_graph_client
        graph = build_graph_client(config)
    kwargs = {"clock": clock} if clock else {}
    transcript = TranscriptWorker(connect=connect, gateway=TranscriptGateway(graph), graph_client=graph, **kwargs)
    recording = build_recording_worker(config, connect=connect, graph=graph)
    if clock:
        recording.clock = clock
    handlers = {"ACQUIRE_TRANSCRIPT": TranscriptJobHandler(worker=transcript, connect=connect),
                "RESOLVE_RECORDING": RecordingJobHandler(worker=recording, connect=connect)}
    if config.writes_bunny:
        library = config.library_id
        if library in FORBIDDEN_LIBRARY_IDS:
            raise ConfigError([f"library {library} is never a destination"])
        if bunny is None:
            from app.bunny.client import BunnyStreamClient
            bunny = BunnyStreamClient(library, config.api_key)
        if int(bunny.library_id) != int(library):
            raise ConfigError(["the Bunny client is bound to a different library than configured"])
        if core is None:
            from app.bunny.client import BunnyCoreClient
            core = BunnyCoreClient(config.account_api_key)
        settings = core.get_video_library(library)
        problems = check_library_settings(settings)
        if problems:
            raise ConfigError(problems)
        from app.media.delivery import SharePointMedia
        runtime = BunnyRuntime(config=config, connect=connect, bunny=bunny, media=SharePointMedia(graph), graph=graph,
                               encoding_tier=settings.get("EncodingTier"),
                               open_url=_cdn_open if config.cdn_hostname else None, **kwargs)
        for kind, worker in (("IMPORT_VIDEO", ImportWorker(runtime)), ("POLL_ENCODING", EncodingWorker(runtime)),
                             ("PUBLISH_CAPTION", CaptionWorker(runtime))):
            handlers[kind] = BunnyJobHandler(kind, worker=worker, connect=connect)
    if lms_source is None and config.lms_database_url:
        from app.lms.sources import LmsOccurrenceSource
        lms_source = LmsOccurrenceSource(config.lms_database_url)
    if calendar is None and config.discover_calendar:
        calendar = CalendarDiscovery(config)
    planner = Planner(config=config, connect=connect, lms_source=lms_source, calendar=calendar, **kwargs)
    reconciler = Reconciler(config=config, connect=connect, bunny=bunny if config.writes_bunny else None,
                            planner=planner, **kwargs)
    auto_releaser = None
    if config.auto_release:                      # config.validate(): PRODUCTION_WRITE only
        from app.publisher.auto_release import AutoReleaser
        from app.publisher.library_security import DeliveryPolicy, read_delivery_security
        policy, library = DeliveryPolicy.from_config(config), config.library_id
        auto_releaser = AutoReleaser(config=config, connect=connect,
                                     security=lambda: read_delivery_security(core, library, policy))
    return PublisherDaemon(config=config, connect=connect, handlers=handlers, planner=planner,
                           reconciler=reconciler, auto_releaser=auto_releaser, **kwargs)


def _cdn_open(request, timeout):
    import urllib.request
    return urllib.request.urlopen(request, timeout=timeout)
