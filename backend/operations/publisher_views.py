"""
Continuous Bunny Publisher: the operations API.

Authenticated like every other operations route (DRF TokenAuthentication +
IsAuthenticated, config/settings.py). Reads run in READ ONLY transactions
(platform.reading); the two operator actions - retry a FAILED / REVIEW_REQUIRED
job, INCLUDE / EXCLUDE a recording part - write through platform.writing and
are audited in bunny_publisher_events with the authenticated username as the actor.

No response carries a URL, a key, a file's content or transcript text; no
request calls Microsoft Graph or Bunny - the publisher service does the work.
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.conf import settings

from app.common.errors import PlatformError
from app.publisher import delivery, operations
from app.publisher.config import PublisherConfig

from .platform import reading, writing


def _actor(request) -> str:
    return getattr(getattr(request, "user", None), "username", "") or ""


def _library(request):
    raw = request.query_params.get("library_id")
    if raw:
        if not str(raw).isdigit():
            raise operations.OperatorActionRefused("INVALID_LIBRARY", "library_id must be a number")
        return int(raw)
    return PublisherConfig.from_environment().library_id


def _handle(view):
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except operations.OperatorActionRefused as exc:
            code = status.HTTP_404_NOT_FOUND if exc.code.endswith("NOT_FOUND") else status.HTTP_409_CONFLICT
            return Response({"code": exc.code, "detail": exc.detail}, status=code)
        except PlatformError as exc:
            return Response({"code": exc.code, "detail": "the platform could not answer"},
                            status=status.HTTP_502_BAD_GATEWAY)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


@api_view(["GET"])
@_handle
def status_view(request):
    """Status summary: jobs by kind and status, worker health, open alerts, readiness counts."""
    with reading() as connection:
        return Response(operations.status_summary(connection, library_id=_library(request)))


@api_view(["GET"])
@_handle
def lecture_view(request, lecture_id):
    """One lecture: every track, every recording part, its jobs and retry history."""
    with reading() as connection:
        return Response(operations.lecture_detail(connection, str(lecture_id), library_id=_library(request)))


@api_view(["GET"])
@_handle
def pending_view(request):
    with reading() as connection:
        return Response({"jobs": operations.pending_work(connection)})


@api_view(["GET"])
@_handle
def exceptions_view(request):
    """The exception queue: failed / review-required work from every track, and open alerts."""
    with reading() as connection:
        return Response({"exceptions": operations.exceptions(connection),
                         "alerts": operations.open_alerts(connection)})


@api_view(["GET"])
@_handle
def job_history_view(request, job_id):
    with reading() as connection:
        return Response(operations.job_history(connection, str(job_id)))


@api_view(["POST"])
@_handle
def job_retry_view(request, job_id):
    """Operator retry (audited). Body: {"reason": "..."}."""
    reason = (request.data or {}).get("reason") if hasattr(request, "data") else None
    with writing() as connection:
        connection.autocommit = True
        job = operations.operator_retry(connection, str(job_id), actor=_actor(request), reason=reason or "")
    return Response(job, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@_handle
def part_decision_view(request, lecture_id, part_index):
    """INCLUDE / EXCLUDE one recording part (audited). Body: {"decision": "...", "reason": "..."}."""
    data = request.data or {}
    with writing() as connection:
        connection.autocommit = True
        result = operations.decide_part(connection, str(lecture_id), part_index, decision=data.get("decision"),
                                        actor=_actor(request), reason=data.get("reason") or "")
    return Response(result)


@api_view(["POST"])
@_handle
def lecture_withdraw_view(request, lecture_id):
    """
    WITHDRAW a lecture from LMS delivery immediately (audited; body {"reason": "..."}).

    RELEASE is deliberately not offered here: it needs a live read of the Bunny library's security
    settings with the account key, which only the publisher service holds (CLI `release`).
    """
    library = settings.PUBLISHER_LMS_LIBRARY_ID or _library(request)
    if library is None:
        raise operations.OperatorActionRefused("NO_LIBRARY", "PUBLISHER_LMS_LIBRARY_ID is not configured")
    reason = (request.data or {}).get("reason") if hasattr(request, "data") else None
    with writing() as connection:
        connection.autocommit = True
        result = delivery.withdraw(connection, str(lecture_id), library_id=int(library), actor=_actor(request),
                                   reason=reason or "")
    return Response(result)
