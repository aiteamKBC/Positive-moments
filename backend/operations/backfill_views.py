"""
QA Core RC2: the Operations Backfill endpoints.

WHAT THESE VIEWS DO NOT DO
--------------------------
They do not run a backfill. Measured on real September data, one day costs
about seven seconds of Microsoft Graph plus stage resolution, so a month is
minutes of work - and a request that spends minutes holding a worker is one
the browser, the proxy or gunicorn will time out long before it finishes.

So both POSTs do the same small thing: validate the range, write one row, and
return it. The `backfill-runner` service claims that row and does the work.
The console polls the status endpoint. Closing the tab changes nothing.

PREVIEW IS A RUN TOO
--------------------
A preview costs the same Graph reads as an execution, so it gets the same
durable treatment - one run with `mode = PREVIEW`. It writes nothing except
its own progress rows: no registry row, no QA row, no Perfect row, no
discovery run row. That is enforced by the runner giving preview work a
PostgreSQL read-only connection, not by this module promising it.
"""
from datetime import date as _date

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.db.repositories.backfill import (
    MODE_EXECUTE,
    MODE_PREVIEW,
    TERMINAL,
    BackfillRepository,
)
from app.orchestration.backfill import (
    BACKFILL_RUNNER_VERSION,
    business_days,
    validate_range,
)
from app.recordings.coverage import summarize

from .platform import reading, writing
from .views import _BadRequest


def _repository() -> BackfillRepository:
    return BackfillRepository()


def _handle(view):
    """Every expected outcome is a status code; nothing here is a 500."""
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _BadRequest as exc:
            return Response({"code": "invalid_parameter", "detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except PlatformError as exc:
            # A bad RANGE is the operator's mistake and deserves a 400; a
            # database fault is ours and deserves a 502.
            bad_request = exc.code in ("INVALID_BACKFILL_RANGE",
                                       "BACKFILL_RANGE_TOO_LARGE")
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=(status.HTTP_400_BAD_REQUEST if bad_request
                        else status.HTTP_502_BAD_GATEWAY))
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _range(request) -> tuple[_date, _date]:
    data = request.data if isinstance(request.data, dict) else {}
    raw_from = str(data.get("from") or data.get("requested_from") or "").strip()
    raw_to = str(data.get("to") or data.get("requested_to") or "").strip()
    if not raw_from or not raw_to:
        raise _BadRequest("both 'from' and 'to' dates are required")
    try:
        requested_from = _date.fromisoformat(raw_from)
        requested_to = _date.fromisoformat(raw_to)
    except ValueError as exc:
        raise _BadRequest("dates must be ISO-8601, for example 2026-09-01") from exc
    # The platform's own rule, not a copy of it.
    validate_range(requested_from, requested_to)
    return requested_from, requested_to


def _actor(request) -> str:
    user = getattr(request, "user", None)
    return getattr(user, "username", "") or "unknown"


def _shape(run: dict) -> dict:
    """Serialise a run. Dates as ISO strings; the console formats them."""
    if run is None:
        return {}
    out = {}
    for key, value in run.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key in ("backfill_run_id", "current_lecture_id") and value is not None:
            out[key] = str(value)
        else:
            out[key] = value
    total = int(run.get("total_days") or 0)
    done = int(run.get("completed_days") or 0)
    out["progress_percent"] = round((done / total) * 100) if total else 0
    out["is_finished"] = run.get("status") in TERMINAL
    return out


def _create(request, mode: str):
    requested_from, requested_to = _range(request)
    days = business_days(requested_from, requested_to)
    with writing() as connection:
        run_id = _repository().create(
            connection, requested_from=requested_from, requested_to=requested_to,
            created_by=_actor(request), total_days=len(days), mode=mode,
            runner_version=BACKFILL_RUNNER_VERSION)
        connection.commit()
        run = _repository().get(connection, run_id)
    return Response({"backfill_run": _shape(run),
                     "detail": "queued; the backfill runner will pick this up"},
                    status=status.HTTP_202_ACCEPTED)


# --- create -----------------------------------------------------------------

@api_view(["POST"])
@_handle
def backfill_preview_view(request):
    """
    Queue a READ-ONLY inspection of a date range.

    Nothing is written but this run's own progress. Poll the detail endpoint
    for per-day results; `Start` is a separate, explicit decision afterwards.
    """
    return _create(request, MODE_PREVIEW)


@api_view(["POST"])
@_handle
def backfill_start_view(request):
    """
    Queue a real backfill.

    Guarded by everything a nightly cycle is guarded by, because it IS a
    nightly cycle per day: the read-only n8n ownership preflight fails the run
    closed if the legacy QA path is active, the cycle lock keeps it out of the
    scheduler's way, and every writer protection applies unchanged.
    """
    return _create(request, MODE_EXECUTE)


# --- read -------------------------------------------------------------------

@api_view(["GET"])
@_handle
def backfill_list_view(request):
    try:
        limit = min(int(request.query_params.get("limit", 25)), 100)
    except (TypeError, ValueError) as exc:
        raise _BadRequest("limit must be a whole number") from exc
    with reading() as connection:
        runs = _repository().recent(connection, limit=limit)
        active = _repository().active(connection)
    return Response({"backfill_runs": [_shape(run) for run in runs],
                     "active": _shape(active) if active else None})


@api_view(["GET"])
@_handle
def backfill_detail_view(request, run_id):
    with reading() as connection:
        run = _repository().get(connection, run_id)
        if run is None:
            return Response({"code": "not_found",
                             "detail": "no such backfill run"},
                            status=status.HTTP_404_NOT_FOUND)
        days = _repository().days(connection, run_id)
        items = _repository().recording_items(connection, run_id)
    return Response({"backfill_run": _shape(run),
                     "days": [_shape(day) for day in days],
                     "recording_links": _recording_summary(run, days, items)})


@api_view(["GET"])
@_handle
def backfill_recording_links_view(request, run_id):
    """
    Lecture-level Recording Links outcomes for one run.

    A PREVIEW run's rows are live, read-only Graph verdicts; an EXECUTE run's
    rows are what the shared orchestrator's RECORDING_LINK stage did. Every
    field comes from the run's stored snapshot - statuses, counts, timings and
    a source NAME. No recording URL, Graph id, file name or token is stored,
    so none can be returned.
    """
    with reading() as connection:
        run = _repository().get(connection, run_id)
        if run is None:
            return Response({"code": "not_found",
                             "detail": "no such backfill run"},
                            status=status.HTTP_404_NOT_FOUND)
        days = _repository().days(connection, run_id)
        items = _repository().recording_items(connection, run_id)
    return Response({"backfill_run_id": str(run_id), "mode": run.get("mode"),
                     "recording_links": _recording_summary(run, days, items),
                     "items": [_shape(item) for item in items]})


def _recording_summary(run: dict, days: list[dict], items: list[dict]) -> dict:
    """The platform's own totals over the stored rows. The console counts nothing."""
    evaluated_days = sum(1 for day in days if day.get("status") == "COMPLETED")
    failed_days = sorted({str(getattr(day["business_date"], "isoformat",
                                      lambda: day["business_date"])())
                          for day in days if day.get("recording_error_code")})
    return {
        "coverage": summarize(items),
        "evaluation": ("LIVE_PREVIEW" if run.get("mode") == MODE_PREVIEW else "EXECUTE"),
        "days_evaluated": evaluated_days,
        "days_with_recording_errors": failed_days,
        "recording_error_codes": sorted({day["recording_error_code"] for day in days
                                         if day.get("recording_error_code")}),
        # Preview guarantees, stated as data rather than implied.
        "database_writes": 0 if run.get("mode") == MODE_PREVIEW else None,
        "sharing_links_created": 0 if run.get("mode") == MODE_PREVIEW else None,
        "provider_calls": 0,
    }


# --- cancel -----------------------------------------------------------------

@api_view(["POST"])
@_handle
def backfill_cancel_view(request, run_id):
    """
    Ask the runner to stop at the next safe boundary.

    Cooperative by design: the day in flight finishes, no new day starts, and
    the run is recorded CANCELLED. Nothing here interrupts a transaction or
    kills a process, because a backfill day ends in a database write and
    tearing that down mid-flight is how half-written state happens.
    """
    with writing() as connection:
        accepted = _repository().request_cancel(
            connection, run_id, requested_by=_actor(request))
        connection.commit()
        run = _repository().get(connection, run_id)
    if run is None:
        return Response({"code": "not_found", "detail": "no such backfill run"},
                        status=status.HTTP_404_NOT_FOUND)
    if not accepted:
        return Response(
            {"code": "ALREADY_FINISHED",
             "detail": f"this run is already {run['status']}; nothing to cancel",
             "backfill_run": _shape(run)},
            status=status.HTTP_409_CONFLICT)
    return Response({"backfill_run": _shape(run),
                     "detail": "cancellation requested; the current day will "
                               "finish and no further day will start"})
