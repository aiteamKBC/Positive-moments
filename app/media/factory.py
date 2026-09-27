"""
Assemble the Positive Moments media platform from Settings.

One app-only Graph client (the platform's existing MICROSOFT_GRAPH_* one), the
existing QA model credential, the one new CREATOMATE_API_KEY. No second
credential for anything.
"""
from __future__ import annotations

from app.db.repositories.media_platform import MediaPlatformRepository
from app.db.repositories.positive_moments import PositiveMomentRepository
from app.media.delivery import SharePointMedia
from app.media.moments_media import MediaSettings, PositiveMomentMediaService, lookup_graph_recordings
from app.media.render.base import RenderPolicy, RenderProviderError
from app.media.runner import MediaJobWorker, MediaRunner, MediaRunProcessor
from app.media.webhook import webhook_url


def render_policy(settings) -> RenderPolicy:
    return RenderPolicy(max_width=settings.media_render_max_width,
                        max_height=settings.media_render_max_height,
                        frame_rate=settings.media_render_fps)


def media_settings(settings) -> MediaSettings:
    return MediaSettings(provider=settings.media_render_provider,
                         padding_before=settings.positive_moment_padding_before_seconds,
                         padding_after=settings.positive_moment_padding_after_seconds,
                         render_policy=render_policy(settings))


def build_provider(settings, *, http=None):
    from app.media.render.creatomate import CreatomateMediaRenderProvider
    if settings.media_render_provider != "creatomate":
        raise ValueError("only the creatomate provider exists in this release")
    return CreatomateMediaRenderProvider(api_key=settings.creatomate_api_key, http=http)


def build_model(settings):
    if not settings.qa_model_api_key:
        return None
    from app.positive_moments.ai import JsonSchemaModel
    return JsonSchemaModel(api_key=settings.qa_model_api_key,
                           model=settings.positive_moments_model_name or settings.qa_model_name,
                           base_url=settings.qa_model_base_url)


def build_media_service(settings, *, graph=None, model=None, resolver=None):
    from app.orchestration.factory import build_resolver
    from app.positive_moments.service import PositiveMomentAnalyzer
    from app.recordings.graph_lookup import RecordingMetadataGateway
    from app.db.repositories.recording_links import RecordingLinkRepository

    if graph is None:
        from app.graph.auth import build_graph_client
        graph = build_graph_client(settings)
    moments = PositiveMomentRepository()
    return PositiveMomentMediaService(
        resolver=resolver or build_resolver(
            recording_link_mode=getattr(settings, "recording_link_mode", "observe")),
        moments_repository=moments, media_repository=MediaPlatformRepository(),
        analyzer=PositiveMomentAnalyzer(repository=moments,
                                        model=model if model is not None else build_model(settings)),
        sharepoint=SharePointMedia(graph),
        recordings_lookup=lookup_graph_recordings(RecordingMetadataGateway(graph),
                                                  RecordingLinkRepository()),
        settings=media_settings(settings))


def build_media_runner(settings, *, connection_factory, graph=None, provider=None, model=None):
    if graph is None:
        from app.graph.auth import build_graph_client
        graph = build_graph_client(settings)
    media = MediaPlatformRepository()
    drive, folder = settings.media_destination()
    secret, base = settings.media_webhook_secret, settings.media_webhook_base_url
    provider_holder = {}

    def provider_factory():
        if "provider" not in provider_holder:
            try:
                provider_holder["provider"] = provider or build_provider(settings)
            except RenderProviderError:
                provider_holder["provider"] = UnconfiguredProvider()
        return provider_holder["provider"]

    def worker():
        return MediaJobWorker(
            media_repository=media, moments_repository=PositiveMomentRepository(),
            provider=provider_factory(), sharepoint=SharePointMedia(graph),
            destination_drive_id=drive, destination_folder_item_id=folder,
            webhook_url_for=(lambda job_id: webhook_url(base, secret, job_id))
            if base and secret else None)

    service = build_media_service(settings, graph=graph, model=model)

    def processor():
        return MediaRunProcessor(media_repository=media, media_service=service)

    return MediaRunner(connection_factory=connection_factory, worker_factory=worker,
                       run_processor_factory=processor, media_repository=media)


class UnconfiguredProvider:
    """Stands in when CREATOMATE_API_KEY is empty: every call fails, visibly and retryably."""
    name = "creatomate"

    def _refuse(self, *args, **kwargs):
        raise RenderProviderError("CREATOMATE_NOT_CONFIGURED",
                                  "CREATOMATE_API_KEY is not configured on the server",
                                  retryable=True)

    submit = get_status = _refuse

    def validate_result(self, *args, **kwargs):
        return ["RENDER_PROVIDER_NOT_CONFIGURED"]
