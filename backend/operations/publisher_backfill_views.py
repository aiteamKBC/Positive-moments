"""
Historical Backfill (Lecture Publishing Monitor): the operations API.

    GET  publishing-monitor/backfill/runs/                 runs, newest first (+ whether you may operate)
    POST publishing-monitor/backfill/preview/              READ ONLY preview of an inclusive date range
    POST publishing-monitor/backfill/runs/start/           start a run (operator; confirmed preview only)
    GET  publishing-monitor/backfill/runs/<id>/            one run: counts, every lecture's live pipeline
    POST publishing-monitor/backfill/runs/<id>/cancel/     stop pending work (operator)
    POST publishing-monitor/backfill/runs/<id>/retry/      re-open blocked / failed lectures (operator)

PREVIEW never writes: it runs on platform.reading() (a PostgreSQL READ ONLY transaction) and calls no
Graph, LMS or Bunny API; it answers from the publisher's stored state and says so (`data_source`). It is
a POST only because it returns a confirmation token: the preview's fingerprint, signed with the Django
secret and valid for PREVIEW_TTL seconds.

START does no publishing work. It re-computes the preview from the current database, refuses a stale or
forged confirmation, and writes ONE run row plus its items (at most one run is active: a concurrent start
gets 409). The publisher service's HISTORICAL_BACKFILL task does the rest, through the publisher's own
jobs and workers - nothing is imported inside an HTTP request.

Every write needs the operator group (settings.PUBLISHER_BACKFILL_OPERATOR_GROUP); every response carries
codes, counts and identities only - never a URL, key or transcript text.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
from django.conf import settings
from django.core import signing
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.db.repositories.publisher_backfill import BackfillRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher import backfill, monitor
from app.publisher.operations import OperatorActionRefused

from . import publishing_monitor_views as pm
from .platform import reading, writing

PREVIEW_TTL = 30 * 60
TOKEN_SALT = "kbc.publisher.historical-backfill.preview"


def is_operator(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active
                and user.groups.filter(name=settings.PUBLISHER_BACKFILL_OPERATOR_GROUP).exists())


class IsBackfillOperator(BasePermission):
    message = "Starting, cancelling or retrying a historical backfill needs the publisher backfill operator role."

    def has_permission(self, request, view):
        return is_operator(request.user)


def _actor(request) -> str:
    return getattr(request.user, "username", "") or "unknown"


def _handle(view):
    def wrapper(request, *args, **kwargs):
        if settings.PUBLISHER_LMS_LIBRARY_ID is None:
            return Response({"code": "NOT_CONFIGURED", "detail": "PUBLISHER_LMS_LIBRARY_ID is not configured"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            return view(request, *args, **kwargs)
        except backfill.RangeRefused as exc:
            return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except OperatorActionRefused as exc:
            code = status.HTTP_404_NOT_FOUND if exc.code.endswith("NOT_FOUND") else status.HTTP_409_CONFLICT
            return Response({"code": exc.code, "detail": exc.detail}, status=code)
        except psycopg.errors.UndefinedTable:
            return Response({"code": "NOT_INSTALLED", "detail": "the historical backfill tables are not installed"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except (PlatformError, psycopg.Error):
            return Response({"code": "PLATFORM_UNAVAILABLE", "detail": "the publisher database could not answer"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _range(data) -> tuple:
    date_from = backfill.parse_date(data.get("date_from"), "From Date")
    date_to = backfill.parse_date(data.get("date_to"), "To Date")
    backfill.validate_range(date_from, date_to, today=pm.cairo_today(), max_days=settings.PUBLISHER_BACKFILL_MAX_DAYS)
    return date_from, date_to


def compute_preview(date_from, date_to) -> dict:
    """The preview from the current database: every query READ ONLY, lectures with a video resolved in parallel."""
    library, language = pm._library(), pm._language()
    with reading() as connection:
        now = connection.execute("SELECT now()").fetchone()[0]
        facts = monitor.load_facts(connection, date_from, date_to, library)
        recordings = backfill.load_recordings(connection, [f["lecture_id"] for f in facts])

    def resolve(fact):
        try:
            with reading() as connection:
                return fact["lecture_id"], monitor.resolve(connection, fact, library_id=library, language=language), None
        except (PlatformError, psycopg.Error) as exc:
            return fact["lecture_id"], None, f"RESOLUTION_FAILED:{type(exc).__name__}"

    views, errors = {}, {}
    for lecture_id, view, error in pm._workers.map(resolve, [f for f in facts if monitor.needs_resolution(f)]):
        if error:
            errors[lecture_id] = error
        else:
            views[lecture_id] = view
    return backfill.build_preview(facts, views, recordings, date_from=date_from, date_to=date_to, library_id=library,
                                  now=now, playback_enabled=bool(settings.PUBLISHER_LMS_PLAYBACK_ENABLED),
                                  errors=errors)


def _token(preview: dict) -> str:
    return signing.dumps({"f": preview["date_from"], "t": preview["date_to"], "l": preview["library_id"],
                          "fp": preview["fingerprint"], "n": preview["summary"]["eligible"]}, salt=TOKEN_SALT)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _shape(run: dict | None, counts: dict | None = None) -> dict | None:
    if run is None:
        return None
    out = {k: (str(v) if k == "run_id" else _iso(v)) for k, v in run.items() if k != "preview_fingerprint"}
    if counts is not None:
        out["counts"] = {"total": sum(counts.values()), **{k.lower(): v for k, v in counts.items()}}
    out["is_finished"] = run["status"] in ("COMPLETED", "CANCELLED", "FAILED")
    out["cancel_requested"] = run["cancel_requested_at"] is not None
    return monitor.redact(out)


# --------------------------------------------------------------------------- reads
@api_view(["GET"])
@_handle
def runs_view(request):
    """Recent runs (newest first), the active one, the limits, and whether the signed-in user may operate."""
    repository = BackfillRepository()
    with reading() as connection:
        runs = repository.recent(connection, limit=25)
        shaped = [_shape(r, repository.counts(connection, r["run_id"])) for r in runs]
    active = next((r for r in shaped if r["status"] in ("QUEUED", "RUNNING")), None)
    return Response({"runs": shaped, "active": active, "operator": is_operator(request.user),
                     "library_id": pm._library(), "max_days": settings.PUBLISHER_BACKFILL_MAX_DAYS,
                     "today": pm.cairo_today().isoformat(), "timezone": "Africa/Cairo"})


@api_view(["POST"])
@_handle
def preview_view(request):
    """READ ONLY: what a run over the inclusive range would do, per lecture, with a confirmation token."""
    date_from, date_to = _range(request.data or {})
    preview = compute_preview(date_from, date_to)
    return Response({**preview, "preview_token": _token(preview), "token_ttl_seconds": PREVIEW_TTL,
                     "read_only": True, "database_writes": 0, "jobs_queued": 0, "bunny_calls": 0})


@api_view(["GET"])
@_handle
def run_view(request, run_id):
    """One run: its counts and every lecture with its live pipeline (Teams -> ... -> LMS playback)."""
    repository = BackfillRepository()
    with reading() as connection:
        run = repository.get(connection, run_id)
        if run is None:
            return Response({"code": "RUN_NOT_FOUND", "detail": "no such historical backfill run"},
                            status=status.HTTP_404_NOT_FOUND)
        items = repository.items(connection, run_id)
        counts = repository.counts(connection, run_id)
    rows, generated_at = pm._rows(run["date_from"], run["date_to"])
    by_id = {r["lecture_id"]: r for r in rows}
    lectures = []
    for item in items:
        lid = str(item["lecture_id"])
        row = by_id.get(lid)
        lectures.append(monitor.redact({
            "lecture_id": lid, "session_date": _iso(item["session_date"]), "status": item["status"],
            "initial_category": item["initial_category"], "reason": item["reason"], "stage": item["stage"],
            "retries": item["retries"], "started_at": _iso(item["started_at"]), "finished_at": _iso(item["finished_at"]),
            "updated_at": _iso(item["updated_at"]),
            "subject": row["subject"] if row else None, "module": row["module"] if row else None,
            "scheduled_start": row["scheduled_start"] if row else None, "scheduled_end": row["scheduled_end"] if row else None,
            "monitor_state": row["state"] if row else None, "monitor_reason": row["reason"] if row else None,
            "pipeline": backfill.pipeline(row) if row else [],
        }))
    return Response({"run": _shape(run, counts), "lectures": lectures, "generated_at": generated_at,
                     "operator": is_operator(request.user)})


# --------------------------------------------------------------------------- writes (operators only)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsBackfillOperator])
@_handle
def start_view(request):
    """
    Start a run for a confirmed preview. Body: {date_from, date_to, preview_token, confirm: {library_id,
    eligible}, reason?}. Re-checks the preview against the current database before writing anything.
    """
    data = request.data or {}
    date_from, date_to = _range(data)
    try:
        token = signing.loads(str(data.get("preview_token") or ""), salt=TOKEN_SALT, max_age=PREVIEW_TTL)
    except signing.SignatureExpired:
        return _conflict("PREVIEW_EXPIRED", "the preview is older than 30 minutes; preview the range again")
    except signing.BadSignature:
        return Response({"code": "PREVIEW_REQUIRED", "detail": "run a preview of this range first"},
                        status=status.HTTP_400_BAD_REQUEST)
    library = pm._library()
    if (token.get("f"), token.get("t"), token.get("l")) != (date_from.isoformat(), date_to.isoformat(), library):
        return Response({"code": "PREVIEW_MISMATCH", "detail": "the confirmation belongs to a different range"},
                        status=status.HTTP_400_BAD_REQUEST)
    preview = compute_preview(date_from, date_to)
    summary = preview["summary"]
    if preview["fingerprint"] != token.get("fp"):
        return _conflict("PREVIEW_STALE", "the lectures of this range changed since the preview; review the new "
                         "preview and confirm again", summary=summary)
    confirm = data.get("confirm") or {}
    if str(confirm.get("library_id")) != str(library) or str(confirm.get("eligible")) != str(summary["eligible"]):
        return _conflict("CONFIRMATION_MISMATCH", "the confirmation does not match the preview's library and "
                         "eligible lecture count", summary=summary)
    if not summary["eligible"]:
        return _conflict("NOTHING_TO_QUEUE", "no lecture in this range is eligible for a backfill", summary=summary)
    reason = monitor.scrub_text(" ".join(str(data.get("reason") or "").split()))[:300]
    repository = BackfillRepository()
    with _writing() as connection:
        with connection.transaction():
            run = repository.create(connection, date_from=date_from, date_to=date_to, library_id=library,
                                    requested_by=_actor(request), reason=reason, fingerprint=preview["fingerprint"],
                                    summary={k: v for k, v in summary.items() if k != "per_day"},
                                    items=backfill.run_items(preview))
            if run is None:
                active = repository.active(connection)
                return _conflict("BACKFILL_ALREADY_ACTIVE", "another historical backfill is running; wait for it "
                                 "to finish or cancel it", active=_shape(active))
            PublisherQueueRepository().event(
                connection, "HISTORICAL_BACKFILL_REQUESTED", actor=_actor(request),
                detail={"run_id": str(run["run_id"]), "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(), "library_id": library, "eligible": summary["eligible"],
                        "estimated_imports": summary["estimated_imports"]["upper_bound"]})
        counts = repository.counts(connection, run["run_id"])
    return Response({"run": _shape(run, counts), "detail": "queued; the publisher service starts it within a minute"},
                    status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsBackfillOperator])
@_handle
def cancel_view(request, run_id):
    """Stop pending work of the active run; in-flight imports, encoding and captions finish normally."""
    repository = BackfillRepository()
    with _writing() as connection:
        with connection.transaction():
            run = repository.request_cancel(connection, run_id, requested_by=_actor(request))
            if run is not None:
                PublisherQueueRepository().event(connection, "HISTORICAL_BACKFILL_CANCEL_REQUESTED",
                                                 actor=_actor(request), detail={"run_id": str(run_id)})
        if run is None:
            existing = repository.get(connection, run_id)
            if existing is None:
                raise OperatorActionRefused("RUN_NOT_FOUND", "no such historical backfill run")
            raise OperatorActionRefused("RUN_NOT_ACTIVE", f"this run is already {existing['status']}")
    return Response({"run": _shape(run), "detail": "cancellation requested; applied by the publisher within a minute"},
                    status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsBackfillOperator])
@_handle
def retry_view(request, run_id):
    """Re-open the run's BLOCKED / FAILED lectures (all, or `lecture_ids`). Body: {reason, lecture_ids?}."""
    data = request.data or {}
    reason = monitor.scrub_text(" ".join(str(data.get("reason") or "").split()))[:300]
    if not reason:
        raise backfill.RangeRefused("REASON_REQUIRED", "give a reason for the retry")
    ids = data.get("lecture_ids") or []
    if not isinstance(ids, list) or len(ids) > 1000:
        raise backfill.RangeRefused("INVALID_LECTURES", "lecture_ids must be a list")
    with _writing() as connection:
        connection.autocommit = True
        try:
            result = backfill.retry_items(connection, run_id, ids, actor=_actor(request), reason=reason)
        except psycopg.errors.UniqueViolation:
            raise OperatorActionRefused("ANOTHER_RUN_ACTIVE", "another historical run is active") from None
    return Response(result, status=status.HTTP_202_ACCEPTED)


@contextmanager
def _writing():
    """platform.writing(), never preparing a statement server-side (the web's database URL is pooled)."""
    with writing() as connection:
        connection.prepare_threshold = None
        yield connection


def _conflict(code: str, detail: str, **extra) -> Response:
    return Response({"code": code, "detail": detail, **extra}, status=status.HTTP_409_CONFLICT)

