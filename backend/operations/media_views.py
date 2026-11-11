"""
Positive Moments Media: the website's API.

Every request here is short. None of them runs an AI analysis, calls
Microsoft Graph, renders a video or transfers a file:

  * reads run in PostgreSQL READ ONLY transactions;
  * "Preview", "Start analysis", "Render all safe clips" and "Retry failed"
    each write ONE run row and return 202 - the `media-runner` service does
    the work, and the page polls;
  * per-moment Render / Retry are compare-and-set state changes on one job.

The webhook is the one unauthenticated route, because Creatomate cannot send
our token. It trusts nothing it is sent: see app/media/webhook.py.

No response contains a temporary download URL, a token or the Creatomate key.
The two "open" routes return one durable SharePoint link, on request.
"""
from datetime import date as _date
from datetime import timedelta

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.db.repositories.media_platform import MediaPlatformRepository
from app.db.repositories.positive_moments import PositiveMomentRepository
from app.media import access
from app.media.dashboard import _clean, dashboard, lecture_media
from app.media.moments_media import PositiveMomentMediaService

from .platform import reading, settings, writing
from .views import _BadRequest, _today

RUN_MODES = ("PREVIEW", "ANALYZE", "RENDER", "RETRY_FAILED")
MAX_RANGE_DAYS = 62


def _handle(view):
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _BadRequest as exc:
            return Response({"code": "invalid_parameter", "detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except PlatformError as exc:
            return Response({"code": exc.code, "detail": "the platform could not answer"},
                            status=status.HTTP_502_BAD_GATEWAY)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _parse_date(raw, name):
    try:
        return _date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise _BadRequest(f"{name} must be YYYY-MM-DD") from exc


def _range(source, *, default_days=14):
    end = _parse_date(source.get("date_to") or source.get("to") or _today().isoformat(),
                      "date_to")
    start = _parse_date(source.get("date_from") or source.get("from")
                        or (end - timedelta(days=default_days - 1)).isoformat(), "date_from")
    if start > end:
        raise _BadRequest("date_from must not be after date_to")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise _BadRequest(f"the range must not exceed {MAX_RANGE_DAYS} days")
    return start, end


def _actor(request):
    return getattr(getattr(request, "user", None), "username", "") or "unknown"


def _jobs_service():
    return PositiveMomentMediaService(resolver=None, moments_repository=PositiveMomentRepository(),
                                      media_repository=MediaPlatformRepository())


# --- reads ------------------------------------------------------------------------

@api_view(["GET"])
@_handle
def media_dashboard_view(request):
    start, end = _range(request.query_params)
    from app.media.factory import render_policy
    config = settings()
    with reading() as connection:
        payload = dashboard(connection, media_repository=MediaPlatformRepository(),
                            date_from=start, date_to=end, render_policy=render_policy(config))
    return Response({**payload, "readiness": config.media_readiness()})


@api_view(["GET"])
@_handle
def media_run_view(request, run_id):
    with reading() as connection:
        run = MediaPlatformRepository().run(connection, run_id)
    if run is None:
        return Response({"code": "not_found", "detail": "no such run"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response({"run": _clean(run)})


@api_view(["GET"])
@_handle
def lecture_positive_moments_view(request, lecture_id):
    with reading() as connection:
        payload = lecture_media(connection, lecture_id=lecture_id,
                                media_repository=MediaPlatformRepository(),
                                moments_repository=PositiveMomentRepository())
    return Response(payload)


@api_view(["GET"])
@_handle
def recording_open_view(request, lecture_id):
    """The lecture's full recording, as ONE durable SharePoint link, on request."""
    with reading() as connection:
        url, code = access.recording_link(connection, lecture_id)
    if url is None:
        return Response({"code": code, "detail": "no openable recording for this lecture"},
                        status=status.HTTP_409_CONFLICT)
    return Response({"url": url})


@api_view(["GET"])
@_handle
def asset_open_view(request, asset_id):
    with reading() as connection:
        url, code = access.asset_link(connection, asset_id)
    if url is None:
        return Response({"code": code, "detail": "no delivered clip"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response({"url": url})


# --- operator actions: queue, never execute -----------------------------------------

@api_view(["POST"])
@_handle
def media_run_create_view(request):
    """Queue a Preview / Analyze / Render / Retry run. Returns immediately."""
    data = request.data if isinstance(request.data, dict) else {}
    mode = str(data.get("mode") or "").upper()
    if mode not in RUN_MODES:
        raise _BadRequest(f"mode must be one of {', '.join(RUN_MODES)}")
    lecture_id = data.get("lecture_id")
    if lecture_id:
        import uuid
        try:
            uuid.UUID(str(lecture_id))
        except ValueError as exc:
            raise _BadRequest("lecture_id must be a UUID") from exc
    start, end = _range(data)
    with writing() as connection:
        run = MediaPlatformRepository().create_run(
            connection, requested_from=start, requested_to=end, mode=mode,
            created_by=_actor(request), lecture_id=lecture_id)
        connection.commit()
    return Response({"run": _clean(run),
                     "detail": "queued; the media runner will process it in the background"},
                    status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@_handle
def moment_render_view(request, moment_id):
    """Queue ONE planned clip for rendering. Never re-renders a delivered plan."""
    with writing() as connection:
        queued = _jobs_service().queue_render(connection, moment_id=moment_id)
        connection.commit()
    if not queued:
        return Response({"code": "NOT_READY_TO_RENDER",
                         "detail": "this moment has no clip ready to render (or it is "
                                   "already rendering or delivered)"},
                        status=status.HTTP_409_CONFLICT)
    return Response({"queued": queued}, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@_handle
def moment_retry_view(request, moment_id):
    with writing() as connection:
        armed = _jobs_service().retry_failed(connection, moment_id=moment_id)
        connection.commit()
    if not armed:
        return Response({"code": "NOTHING_TO_RETRY",
                         "detail": "this moment's clip has no retryable failure"},
                        status=status.HTTP_409_CONFLICT)
    return Response({"rearmed": armed}, status=status.HTTP_202_ACCEPTED)


# --- the provider webhook ----------------------------------------------------------------

@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def creatomate_webhook_view(request):
    from app.media.factory import build_provider
    from app.media.render.base import RenderProviderError
    from app.media.webhook import WebhookRefused, handle_creatomate_webhook

    import hmac

    from app.media.webhook import webhook_token

    config = settings()
    job_id, token = request.query_params.get("job"), request.query_params.get("token")
    # Refused before any database connection or provider call.
    if not config.media_webhook_secret or not job_id or not token or not hmac.compare_digest(
            webhook_token(config.media_webhook_secret, job_id), str(token)):
        return Response({"code": "WEBHOOK_TOKEN_INVALID"}, status=status.HTTP_403_FORBIDDEN)
    try:
        provider = build_provider(config)
    except RenderProviderError:
        return Response({"code": "WEBHOOK_NOT_CONFIGURED"},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        with writing() as connection:
            result = handle_creatomate_webhook(
                connection, media_repository=MediaPlatformRepository(), provider=provider,
                secret=config.media_webhook_secret, job_id=job_id, token=token,
                payload=request.data if isinstance(request.data, dict) else {})
            connection.commit()
    except WebhookRefused as refused:
        return Response({"code": refused.code}, status=refused.http_status)
    except PlatformError:
        return Response({"code": "WEBHOOK_DEFERRED"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({"result": result["result"]})
