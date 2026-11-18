"""
Manual transcript upload: the Operations console's fallback when Microsoft Graph will not serve a
Teams transcript's content.

FOUR ROUTES, EACH NAMED FOR WHAT IT DOES
----------------------------------------
    GET  transcript-recovery/                      lectures (any date) an upload could complete,
                                                   and the recent processing jobs              (read-only)
    GET  lectures/<id>/transcript-upload/          target parts, provenance, plan, latest job  (read-only)
    POST lectures/<id>/transcript-upload/preview/  validate a .vtt against THIS lecture         (read-only)
    POST lectures/<id>/transcript-upload/store/    store the confirmed file, re-select, and
                                                   queue the lecture's processing              (writes)
    POST lectures/<id>/transcript-upload/process/  queue this lecture's processing             (writes)

The lecture is in the URL; the request never names a meeting, a transcript id or a date. The
target transcript is the platform's own selection for the lecture (app/transcripts/manual_upload.py).
Confirming sends back the file's SHA-256 and the part the preview showed, and the server re-checks
both: a different file, or a plan that changed, is refused rather than stored.

The preview runs on the read-only connection, so validating a file cannot write anything even by
mistake. Storing and processing are separate requests with separate transactions: a stored
transcript is never lost because the (much longer) processing step failed or timed out, and
processing can simply be asked for again.

NO PROCESSING IN A REQUEST
--------------------------
Storing queues the lecture (public.lecture_processing_jobs, migration 030) in the same
transaction. The pipeline - the paid quality evaluation included - runs in the background worker
of the positive-moments-backfill service (app/orchestration/lecture_jobs.py). The browser can be
closed as soon as the store returns; "process" only (re)queues.

Authentication is the API's default (a signed-in Dashboard user, IsAuthenticated). By business
decision there is no separate operator role on the console's actions.
"""
from datetime import date

import psycopg
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.orchestration.factory import build_manual_transcript_service
from app.orchestration.locks import LectureBusy, SchedulerCycleBusy
from app.orchestration.transcript_recovery import QUEUE_UNAVAILABLE, ProcessingRefused
from app.transcripts.manual_upload import (
    EMPTY_FILE,
    FILE_TOO_LARGE,
    MAX_UPLOAD_BYTES,
    RECOVERY_QUEUE_LIMIT,
    TRANSCRIPT_ROW_LOCKED,
    UploadRefused,
    problem,
)

from .platform import reading, settings, writing


RECENT_JOBS = 20

STORE_REFUSED_NO_QUEUE = (
    "Automatic processing is not installed on this server yet (migration 030), so the transcript "
    "was not stored: it would not be evaluated. Nothing was changed. Upload it again once the "
    "server update is complete.")


def _service():
    # Provider OFF: the console never runs the paid QA call inside a request (transcript_recovery.py).
    return build_manual_transcript_service(settings(), allow_provider=False)


def _refused(problems, http_status):
    first = problems[0] if problems else {"code": "REFUSED", "message": "The upload was refused."}
    return Response({"code": first["code"], "detail": first["message"], "problems": problems},
                    status=http_status)


def _guarded(view):
    """Every expected outcome to a status code, and nothing to a 500."""
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except UploadRefused as exc:
            code = (status.HTTP_404_NOT_FOUND if exc.is_not_found
                    else status.HTTP_409_CONFLICT if exc.is_conflict
                    else status.HTTP_400_BAD_REQUEST)
            return _refused(exc.problems, code)
        except ProcessingRefused as exc:
            return Response({"code": exc.code, "detail": exc.detail, "plan": exc.plan},
                            status=status.HTTP_409_CONFLICT)
        except SchedulerCycleBusy:
            return Response({"code": "SCHEDULER_CYCLE_RUNNING",
                             "detail": "A scheduler cycle is running; try again shortly."},
                            status=status.HTTP_409_CONFLICT)
        except LectureBusy:
            return Response({"code": "LECTURE_LOCKED",
                             "detail": "Another process is working on this lecture; try again shortly."},
                            status=status.HTTP_409_CONFLICT)
        except PlatformError as exc:
            if isinstance(exc.__cause__, psycopg.errors.LockNotAvailable):
                return _refused([problem(TRANSCRIPT_ROW_LOCKED)], status.HTTP_409_CONFLICT)
            return Response({"code": exc.code, "detail": str(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _file(request) -> tuple[str, bytes]:
    """The uploaded file's name and bytes, refusing an oversize file before reading it."""
    upload = request.FILES.get("file")
    if upload is None:
        raise UploadRefused([problem(EMPTY_FILE, detail="no file was sent")])
    if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
        raise UploadRefused([problem(FILE_TOO_LARGE, limit_bytes=MAX_UPLOAD_BYTES)])
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UploadRefused([problem(FILE_TOO_LARGE, limit_bytes=MAX_UPLOAD_BYTES)])
    return upload.name, raw


def _field(request, name):
    value = request.data.get(name) if request.data else None
    value = str(value).strip() if value is not None else ""
    return value or None


@api_view(["GET"])
@_guarded
def transcript_upload_status_view(request, lecture_id):
    """The lecture, its Teams transcript part(s), upload provenance and the processing plan."""
    with reading() as connection:
        return Response(_service().status(connection, lecture_id))


def _date_param(request, name):
    value = (request.query_params.get(name) or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


@api_view(["GET"])
@_guarded
def transcript_recovery_view(request):
    """
    Lectures, across all dates unless bounded by ?from=&to= (YYYY-MM-DD), whose Teams transcript
    content is missing and that an upload could complete now. Read-only.
    """
    try:
        date_from = _date_param(request, "from")
        date_to = _date_param(request, "to")
        limit = int(request.query_params.get("limit") or RECOVERY_QUEUE_LIMIT)
    except ValueError:
        return Response({"code": "BAD_REQUEST",
                         "detail": "from/to must be YYYY-MM-DD and limit a whole number."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not 1 <= limit <= RECOVERY_QUEUE_LIMIT:
        return Response({"code": "BAD_REQUEST",
                         "detail": f"limit must be between 1 and {RECOVERY_QUEUE_LIMIT}."},
                        status=status.HTTP_400_BAD_REQUEST)
    if date_from and date_to and date_from > date_to:
        return Response({"code": "BAD_REQUEST", "detail": "from must not be after to."},
                        status=status.HTTP_400_BAD_REQUEST)
    with reading() as connection:
        service = _service()
        result = service.uploads.recovery_queue(
            connection, date_from=date_from, date_to=date_to, limit=limit)
        # The background processing jobs, newest first, so the dashboard shows what the uploads
        # became (Queued, Running, Completed, Failed). Absent before migration 030.
        result["jobs"] = (service.jobs.recent(connection, limit=RECENT_JOBS)
                          if service.jobs.available(connection) else [])
        result["queue_installed"] = bool(service.jobs.available(connection))
        return Response(result)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@_guarded
def transcript_upload_preview_view(request, lecture_id):
    """Validate a .vtt against this lecture. Nothing is written: a read-only connection."""
    filename, raw = _file(request)
    with reading() as connection:
        result = _service().uploads.preview(connection, lecture_id, filename=filename, raw=raw,
                                            artifact_id=_field(request, "artifact_id"))
    return Response(result)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@_guarded
def transcript_upload_view(request, lecture_id):
    """Store the confirmed file on this lecture's Teams transcript and re-select the lecture."""
    filename, raw = _file(request)
    service = _service()
    user = _user(request)
    with writing() as connection:
        if not service.jobs.available(connection):
            # Fail BEFORE storing: a stored transcript with no queued processing would leave the
            # operator believing QA is on its way. Nothing has been written in this transaction.
            return _refused([{"code": QUEUE_UNAVAILABLE, "message": STORE_REFUSED_NO_QUEUE}],
                            status.HTTP_503_SERVICE_UNAVAILABLE)
        result = service.uploads.commit(
            connection, lecture_id, filename=filename, raw=raw,
            artifact_id=_field(request, "artifact_id"),
            expected_sha256=_field(request, "expected_sha256") or "",
            uploaded_by=user)
        # Same transaction: the stored transcript and its queued processing job are committed
        # together. Nothing is processed here; the background worker picks the job up.
        result["queue"] = service.queue_processing(connection, lecture_id, requested_by=user)
        connection.commit()
    return Response(result, status=status.HTTP_201_CREATED if result.get("stored") else status.HTTP_200_OK)


@api_view(["POST"])
@parser_classes([JSONParser, FormParser])
@_guarded
def transcript_upload_process_view(request, lecture_id):
    """
    Queue this lecture's processing (QA included) for the background worker. Runs nothing in the
    request: 202 with the job, or 409 saying why there is nothing to queue. Repeating it while a
    job is queued or running returns that same job.
    """
    with writing() as connection:
        result = _service().queue_processing(connection, lecture_id, requested_by=_user(request))
        if not result["queued"]:
            connection.rollback()
            return Response({"code": result["reason"], "detail": result["reason_message"], **result},
                            status=status.HTTP_409_CONFLICT)
        connection.commit()
    return Response(result, status=status.HTTP_202_ACCEPTED)


def _user(request):
    return request.user.get_username() if request.user else None
