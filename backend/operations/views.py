"""
Phase 5A: the read-only Operations API.

Every view here is a thin translation: parse a parameter, call one
`OperationsService` method, return what it said. There is no pipeline logic in
this file and there must never be - see `platform.py` for why.

WHAT THESE ENDPOINTS DELIBERATELY DO NOT DO
-------------------------------------------
No view calls Microsoft Graph, a model provider, the scheduler, attendance
recovery or a legacy writer. Loading a dashboard must never cost money or
change production data, and the read path runs inside a PostgreSQL read-only
transaction so that this is enforced rather than promised.

The one endpoint that makes an outbound call is `precheck`, which reads n8n
workflow state over HTTPS, GET only. It is a separate, explicitly requested
endpoint precisely so that it is not part of any normal page load.
"""
from datetime import date as _date
from datetime import datetime, timedelta

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.common.time import CAIRO

from positive_mentions.utils import encode_session_id

from .platform import operations, reading


def _today() -> _date:
    """
    The business day, in the timezone the platform actually runs in.

    The server's own clock is irrelevant: a lecture's `session_date` is its
    Africa/Cairo date, so "today" has to be the Cairo date or the console
    shows the wrong day for three hours every evening.
    """
    return datetime.now(CAIRO).date()


def _date_param(request) -> _date:
    raw = request.query_params.get("date")
    if not raw:
        return _today()
    try:
        return _date.fromisoformat(raw)
    except ValueError as exc:
        raise _BadRequest(f"date must be YYYY-MM-DD, got {raw!r}") from exc


def _limit_param(request, default: int = 20, ceiling: int = 100) -> int:
    raw = request.query_params.get("limit")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"limit must be an integer, got {raw!r}") from exc
    return max(1, min(value, ceiling))


class _BadRequest(Exception):
    """A parameter the caller can fix, as opposed to a platform failure."""


def _handle(view):
    """
    Turn the two kinds of failure into the two right status codes.

    A bad parameter is the caller's to fix (400). A `PlatformError` is ours,
    and its code travels to the UI because an operator chasing a stuck lecture
    needs the platform's own word for what went wrong - but its message never
    carries a connection string, so there is nothing here to leak.
    """
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _BadRequest as exc:
            return Response({"code": "invalid_parameter", "detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except PlatformError as exc:
            return Response({"code": exc.code, "detail": str(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


# --- the day ----------------------------------------------------------------

@api_view(["GET"])
@_handle
def day_view(request):
    """
    Everything the dashboard shows for one business day.

    `suppressed_duplicate_count` and `business_lecture_count` are the Phase
    4C1 distinction: a suppressed duplicate calendar event is visible for
    audit but is not a lecture anybody has to process.
    """
    session_date = _date_param(request)
    with reading() as connection:
        report = operations().day_reconciliation(connection, session_date)
    return Response({**report, **_navigation(session_date)})


def _navigation(session_date: _date) -> dict:
    today = _today()
    return {"navigation": {
        "date": session_date.isoformat(),
        "previous_date": (session_date - timedelta(days=1)).isoformat(),
        "next_date": (session_date + timedelta(days=1)).isoformat(),
        "today": today.isoformat(),
        "is_today": session_date == today,
        # A future day has no lectures yet and that is not an error. Saying so
        # lets the UI explain an empty day instead of looking broken.
        "is_future": session_date > today,
        "timezone": "Africa/Cairo"}}


@api_view(["GET"])
@_handle
def lectures_view(request):
    """
    The day's lecture table.

    Suppressed duplicate calendar events are EXCLUDED by default: they are not
    lectures. `?include_suppressed=true` returns them in a separate list so an
    operator can still see that the source calendar carried the extra event.
    """
    session_date = _date_param(request)
    include = request.query_params.get("include_suppressed", "").lower() in (
        "1", "true", "yes")
    with reading() as connection:
        report = operations().day_reconciliation(connection, session_date)
    rows = report["lectures"]
    lectures = [row for row in rows if not row.get("is_suppressed_duplicate")]
    suppressed = [row for row in rows if row.get("is_suppressed_duplicate")]
    payload = {"session_date": report["session_date"],
               "business_lecture_count": report["business_lecture_count"],
               "canonical_lecture_count": report["canonical_lecture_count"],
               "suppressed_duplicate_count": report["suppressed_duplicate_count"],
               "lectures": lectures,
               **_navigation(session_date)}
    if include:
        payload["suppressed_duplicates"] = suppressed
    return Response(payload)


# --- the lecture register ------------------------------------------------------

CALENDAR_MAX_DAYS = 400
DIRECTORY_MAX_DAYS = 31


def _range_params(request, *, default_days: int, ceiling: int):
    """A closed [from, to] window, defaulting to the N days ending today."""
    end = _date_param_named(request, "date_to", _today())
    start = _date_param_named(request, "date_from",
                              end - timedelta(days=default_days - 1))
    if start > end:
        raise _BadRequest("date_from must not be after date_to")
    if (end - start).days + 1 > ceiling:
        raise _BadRequest(f"the range must not exceed {ceiling} days")
    return start, end


def _date_param_named(request, name, fallback):
    raw = request.query_params.get(name)
    if not raw:
        return fallback
    try:
        return _date.fromisoformat(raw)
    except ValueError as exc:
        raise _BadRequest(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc


@api_view(["GET"])
@_handle
def calendar_view(request):
    """
    Which dates carry lectures, newest first.

    This is what lets an empty day say "nothing was scheduled here, the last
    day with lectures was X" instead of showing a wall of zeroes. It costs one
    query and resolves no stages.
    """
    start, end = _range_params(request, default_days=60,
                               ceiling=CALENDAR_MAX_DAYS)
    with reading() as connection:
        days = operations().lecture_calendar(connection, start, end)
    business = [day for day in days if day["business_lecture_count"] > 0]
    today = _today()
    past = [day for day in business if day["session_date"] <= today.isoformat()]
    return Response({
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "timezone": "Africa/Cairo",
        # The most recent day on or before today that actually has lectures.
        # Offered to the user as a link; the console never silently jumps to it.
        "latest_lecture_date": past[0]["session_date"] if past else None,
        "days": days,
    })


@api_view(["GET"])
@_handle
def directory_view(request):
    """
    Trainer, module and scheduled window for the lectures in a date range.

    Descriptive metadata only - no stage is resolved here. The Lectures
    workspace joins it to `day/` on `lecture_id`; it never infers a pipeline
    state from it.
    """
    start, end = _range_params(request, default_days=7,
                               ceiling=DIRECTORY_MAX_DAYS)
    with reading() as connection:
        rows = operations().lecture_directory(connection, start, end)
    # The legacy analysis identifier is opaque and is addressed over HTTP in
    # its encoded form. Encoding it here keeps that scheme in the one place
    # that already owns it, instead of reimplementing base64 in the browser.
    for row in rows:
        legacy = row.get("legacy_session_id")
        row["legacy_session_key"] = encode_session_id(legacy) if legacy else None
    return Response({"date_from": start.isoformat(), "date_to": end.isoformat(),
                     "count": len(rows), "lectures": rows})


# --- one lecture ---------------------------------------------------------------

@api_view(["GET"])
@_handle
def lecture_view(request, lecture_id):
    """
    The full fifteen-stage matrix for one lecture, plus what may be done to it.

    `retry_eligibility` and `force_reprocess_eligibility` come from the
    platform, not from the UI's opinion - so a button that should be disabled
    is disabled for the platform's own stated reason.
    """
    service = operations()
    with reading() as connection:
        matrix = service.lecture_stage_matrix(connection, lecture_id)
        history = service.lecture_run_history(connection, lecture_id, limit=20)
    return Response({**matrix, "run_history": history})


@api_view(["GET"])
@_handle
def lecture_history_view(request, lecture_id):
    with reading() as connection:
        history = operations().lecture_run_history(
            connection, lecture_id, limit=_limit_param(request))
    return Response({"lecture_id": str(lecture_id), "runs": history})


# --- the operator's working lists ------------------------------------------------

@api_view(["GET"])
@_handle
def pending_attendance_view(request):
    """
    Lectures whose attendance source is not authoritative.

    This is the list that must never be read as "nobody attended". A
    non-authoritative source means the platform has not been told yet.
    """
    session_date = _date_param(request)
    with reading() as connection:
        rows = operations().pending_attendance(connection, session_date)
    return Response({"session_date": session_date.isoformat(),
                     "count": len(rows), "lectures": rows})


@api_view(["GET"])
@_handle
def review_required_view(request):
    session_date = _date_param(request)
    with reading() as connection:
        rows = operations().review_required(connection, session_date)
    return Response({"session_date": session_date.isoformat(),
                     "count": len(rows), "lectures": rows})


@api_view(["GET"])
@_handle
def errors_view(request):
    """
    Failed lectures only.

    It returns whole rows, filtered on the bucket the platform assigned, so the
    console can show the same columns here as everywhere else. It deliberately
    does NOT list every lecture that happens to carry a reason code: a reason
    code of ELIGIBLE is not an error, and an error queue that fills up with
    successes is an error queue people stop reading.
    """
    session_date = _date_param(request)
    with reading() as connection:
        report = operations().day_reconciliation(connection, session_date)
    rows = [row for row in report["lectures"] if row["bucket"] == "failed"]
    return Response({"session_date": session_date.isoformat(),
                     "count": len(rows), "lectures": rows})


# --- delivered media ------------------------------------------------------------

MEDIA_JOB_STATUSES = ("pending", "processing", "completed", "failed")


@api_view(["GET"])
@_handle
def lecture_media_view(request, lecture_id):
    """
    Positive moments and lecture parts for one lecture, with REAL media state.

    A clip is reported ready only when an asset row carries a durable
    SharePoint URL. A queued job is reported as a queued job, never as a clip.

    Nothing here returns a temporary Graph download URL, an upload-session URL
    or a source drive identifier - the only link that leaves this endpoint is
    the durable SharePoint one the platform already persists.
    """
    service = operations()
    with reading() as connection:
        # The media registry is keyed by the legacy analysis id, which the
        # directory already resolves for a canonical lecture.
        row = connection.execute(
            """SELECT q.session_id
                 FROM public.lecture_sessions l
                 JOIN public.qa_doctors_sessions q
                      ON q.meeting_id = l.meeting_id AND q.date = l.session_date
                WHERE l.lecture_id = %s
                LIMIT 1""", (str(lecture_id),)).fetchone()
        session_id = row[0] if row else None
        media = service.lecture_media(connection, session_id)
    return Response({"lecture_id": str(lecture_id),
                     "legacy_session_id": session_id, **media})


@api_view(["GET"])
@_handle
def media_jobs_view(request):
    """The media queue, for the operations console."""
    status = request.query_params.get("status") or None
    if status and status not in MEDIA_JOB_STATUSES:
        raise _BadRequest(
            f"status must be one of {', '.join(MEDIA_JOB_STATUSES)}, got {status!r}")
    with reading() as connection:
        payload = operations().media_jobs(
            connection, status=status, limit=_limit_param(request, 100, 500))
    return Response(payload)


# --- pipeline runs -------------------------------------------------------------

@api_view(["GET"])
@_handle
def runs_view(request):
    with reading() as connection:
        runs = operations().recent_runs(connection, limit=_limit_param(request))
    return Response({"count": len(runs), "runs": runs})


@api_view(["GET"])
@_handle
def run_view(request, run_id):
    with reading() as connection:
        detail = operations().run_detail(connection, run_id)
    return Response(detail)


# --- the one outbound call ------------------------------------------------------

@api_view(["GET"])
@_handle
def precheck_view(request):
    """
    The read-only n8n safety answer.

    GET only, against a remote n8n on a separate VPS, and never part of a
    normal page load - an operator asks for it. It never modifies a workflow.
    """
    return Response(operations().legacy_qa_precheck())
