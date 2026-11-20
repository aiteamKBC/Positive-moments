"""
Retry Teams transcript: ask Microsoft Teams again for ONE lecture's transcript and let the lecture
finish its normal pipeline (app/orchestration/teams_transcript_retry.py).

    GET  lectures/<id>/teams-transcript-retry/        whether it is offered, its plan, latest job (read-only)
    POST lectures/<id>/teams-transcript-retry/queue/  queue it for the background worker            (writes)

The lecture is in the URL and the request carries nothing else. Nothing runs in the request: the
POST queues a job (public.lecture_processing_jobs) and returns 202 with it, or 409 saying why
nothing was queued. Repeating it while a job is queued or running returns that same job. The
Teams call and the pipeline - the paid quality evaluation included - run in the background worker
of the positive-moments-backfill service.

Authentication is the API's default (a signed-in Dashboard user), as for every console action.
"""
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.response import Response

from app.orchestration.factory import build_teams_transcript_retry

from .platform import reading, settings, writing
from .transcript_upload_views import _guarded, _user


def _service():
    # Console side: plans and queues only. No Graph client, no orchestrator, no provider.
    return build_teams_transcript_retry(settings(), worker=False)


@api_view(["GET"])
@_guarded
def teams_transcript_retry_status_view(request, lecture_id):
    """Whether the retry is offered for this lecture, what it would do, and its latest job."""
    with reading() as connection:
        return Response(_service().status(connection, lecture_id))


@api_view(["POST"])
@parser_classes([JSONParser, FormParser])
@_guarded
def teams_transcript_retry_queue_view(request, lecture_id):
    """Queue the retry: 202 with the job, or 409 with the reason nothing was queued."""
    with writing() as connection:
        result = _service().queue(connection, lecture_id, requested_by=_user(request))
        if not result["queued"]:
            connection.rollback()
            return Response({"code": result["reason"], "detail": result["reason_message"], **result},
                            status=status.HTTP_409_CONFLICT)
        connection.commit()
    return Response(result, status=status.HTTP_202_ACCEPTED)
