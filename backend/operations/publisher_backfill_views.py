"""
Historical Backfill (Lecture Publishing Monitor): the operations API.

    GET  publishing-monitor/backfill/runs/                 runs, newest first, the active preview, your role
    POST publishing-monitor/backfill/preview/              request a live preview of an inclusive date range
    GET  publishing-monitor/backfill/preview/<id>/         the preview: per-day coverage, summary, every lecture
    POST publishing-monitor/backfill/runs/start/           start a run of a COMPLETE preview (operator)
    GET  publishing-monitor/backfill/runs/<id>/            one run: counts, every lecture's live pipeline
    POST publishing-monitor/backfill/runs/<id>/cancel/     stop pending work (operator)
    POST publishing-monitor/backfill/runs/<id>/retry/      re-open blocked / failed lectures (operator)

PREVIEW discovers the range LIVE in Microsoft Teams, which only the publisher service can do (it holds the
Graph, Aptem and LMS credentials). So the request writes one PREVIEW row and returns; the service works
through it day by day, read-only towards every production record (app/publisher/backfill.py), and the
page polls the GET. Nothing but the preview's own rows is written, and nothing is called from this request.
A preview whose discovery did not cover every day is INCOMPLETE and can never be confirmed.

START does no publishing work. It accepts only a COMPLETE preview younger than PREVIEW_TTL, re-checks every
lecture whose category came from stored state against the current database, and writes ONE run row plus its
items - including lectures that so far exist only in Teams (at most one run is active: a concurrent start
gets 409). The publisher service's HISTORICAL_BACKFILL task does the rest, through the publisher's own
discovery, jobs and workers - nothing is discovered or imported inside an HTTP request.

Start, cancel and retry need the operator group (settings.PUBLISHER_BACKFILL_OPERATOR_GROUP); every response
carries codes, counts and identities only - never a URL, key or transcript text.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.db.repositories.publisher_backfill import EXECUTE, PREVIEW, BackfillRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher import backfill, monitor
from app.publisher.operations import OperatorActionRefused

from . import publishing_monitor_views as pm
from .platform import reading, writing

PREVIEW_TTL = 30 * 60


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


@contextmanager
def _writing():
    """platform.writing(), never preparing a statement server-side (the web's database URL is pooled)."""
    with writing() as connection:
        connection.prepare_threshold = None
        yield connection


def _range(data) -> tuple:
    date_from = backfill.parse_date(data.get("date_from"), "From Date")
    date_to = backfill.parse_date(data.get("date_to"), "To Date")
    backfill.validate_range(date_from, date_to, today=pm.cairo_today(), max_days=settings.PUBLISHER_BACKFILL_MAX_DAYS)
    return date_from, date_to


def stored_categories(date_from, date_to) -> dict:
    """lecture_id -> category from the publisher's STORED state now (READ ONLY; no Graph, LMS or Bunny call)."""
    library, language = pm._library(), pm._language()
    with reading() as connection:
        now = connection.execute("SELECT now()").fetchone()[0]
        facts = monitor.load_facts(connection, date_from, date_to, library)
        recordings = backfill.load_recordings(connection, [f["lecture_id"] for f in facts])

    def resolve(fact):
        with reading() as connection:
            return fact["lecture_id"], monitor.resolve(connection, fact, library_id=library, language=language)

    views = dict(pm._workers.map(resolve, [f for f in facts if monitor.needs_resolution(f)]))
    preview = backfill.build_preview(facts, views, recordings, date_from=date_from, date_to=date_to,
                                     library_id=library, now=now)
    return {r["lecture_id"]: r["category"] for r in preview["lectures"]}


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _shape(run: dict | None, counts: dict | None = None) -> dict | None:
    if run is None:
        return None
    out = {k: (str(v) if k in ("run_id", "preview_run_id") and v is not None else _iso(v))
           for k, v in run.items() if k not in ("preview_fingerprint", "discovery")}
    coverage = run.get("discovery") or {}
    days = backfill.business_days(run["date_from"], run["date_to"])
    out["coverage"] = [{"date": d.isoformat(), **backfill.coverage_view(coverage.get(d.isoformat()))} for d in days]
    out["days_discovered"] = sum(1 for c in out["coverage"] if c["status"] in (backfill.COMPLETE, backfill.NOT_AVAILABLE))
    if counts is not None:
        out["counts"] = {"total": sum(counts.values()), **{k.lower(): v for k, v in counts.items()}}
    out["is_finished"] = run["status"] in ("COMPLETED", "INCOMPLETE", "CANCELLED", "FAILED")
    out["cancel_requested"] = run["cancel_requested_at"] is not None
    return monitor.redact(out)


def _preview_answer(run: dict, items: list[dict]) -> dict:
    shaped = _shape(run)
    finished = shaped["is_finished"]
    age = (datetime.now(timezone.utc) - run["finished_at"]).total_seconds() if run["finished_at"] else None
    return {"preview": shaped, "summary": run["preview_summary"] if finished else None,
            "complete": run["status"] == "COMPLETED",
            "confirmable": run["status"] == "COMPLETED" and age is not None and age <= PREVIEW_TTL,
            "expires_in_seconds": max(0, int(PREVIEW_TTL - age)) if age is not None else None,
            "lectures": ([i["detail"] for i in items] + list((run["preview_summary"] or {}).get("qa_only_lectures") or ()))
            if finished else [],
            "production_writes": 0, "jobs_queued": 0, "bunny_calls": 0}


# --------------------------------------------------------------------------- reads
@api_view(["GET"])
@_handle
def runs_view(request):
    """Recent runs (newest first), the active one and preview, the limits, and whether you may operate."""
    repository = BackfillRepository()
    with reading() as connection:
        runs = repository.recent(connection, limit=25)
        shaped = [_shape(r, repository.counts(connection, r["run_id"])) for r in runs]
        previews = repository.recent(connection, limit=1, mode=PREVIEW)
    active = next((r for r in shaped if r["status"] in ("QUEUED", "RUNNING")), None)
    return Response({"runs": shaped, "active": active, "latest_preview": _shape(previews[0]) if previews else None,
                     "operator": is_operator(request.user), "library_id": pm._library(),
                     "max_days": settings.PUBLISHER_BACKFILL_MAX_DAYS, "today": pm.cairo_today().isoformat(),
                     "timezone": "Africa/Cairo"})


@api_view(["POST"])
@_handle
def preview_view(request):
    """Request a live, read-only preview of the inclusive range (the publisher service performs it)."""
    date_from, date_to = _range(request.data or {})
    repository = BackfillRepository()
    with _writing() as connection:
        with connection.transaction():
            run = repository.create(connection, date_from=date_from, date_to=date_to, library_id=pm._library(),
                                    requested_by=_actor(request), mode=PREVIEW)
            if run is None:
                active = repository.active(connection, PREVIEW)
                if active and (active["date_from"], active["date_to"]) == (date_from, date_to):
                    return Response(_preview_answer(active, []), status=status.HTTP_200_OK)
                return Response({"code": "PREVIEW_IN_PROGRESS", "detail": "another preview is being prepared; "
                                 "wait a moment and try again", "preview": _shape(active)},
                                status=status.HTTP_409_CONFLICT)
    return Response(_preview_answer(run, []), status=status.HTTP_202_ACCEPTED)


@api_view(["GET"])
@_handle
def preview_detail_view(request, run_id):
    """The preview: per-day discovery coverage while it runs; its summary and every lecture once finished."""
    repository = BackfillRepository()
    with reading() as connection:
        run = repository.get(connection, run_id)
        if run is None or run["mode"] != PREVIEW:
            return Response({"code": "PREVIEW_NOT_FOUND", "detail": "no such preview"}, status=status.HTTP_404_NOT_FOUND)
        items = repository.items(connection, run_id) if run["status"] in ("COMPLETED", "INCOMPLETE") else []
    return Response(monitor.redact(_preview_answer(run, items)))


@api_view(["GET"])
@_handle
def run_view(request, run_id):
    """One run: its counts and every lecture with its live pipeline (Teams -> ... -> LMS playback)."""
    repository = BackfillRepository()
    with reading() as connection:
        run = repository.get(connection, run_id)
        if run is None or run["mode"] != EXECUTE:
            return Response({"code": "RUN_NOT_FOUND", "detail": "no such historical backfill run"},
                            status=status.HTTP_404_NOT_FOUND)
        items = repository.items(connection, run_id)
        counts = repository.counts(connection, run_id)
    rows, generated_at = pm._rows(run["date_from"], run["date_to"])
    by_id = {r["lecture_id"]: r for r in rows}
    lectures = []
    for item in items:
        lid = str(item["lecture_id"])
        row, known = by_id.get(lid), item["detail"] or {}
        lectures.append(monitor.redact({"sources": known.get("sources"),
            "lecture_id": lid, "session_date": _iso(item["session_date"]), "status": item["status"],
            "origin": item["origin"], "initial_category": item["initial_category"], "reason": item["reason"],
            "stage": item["stage"], "retries": item["retries"], "started_at": _iso(item["started_at"]),
            "finished_at": _iso(item["finished_at"]), "updated_at": _iso(item["updated_at"]),
            "registered": row is not None,
            "subject": row["subject"] if row else known.get("subject"),
            "module": row["module"] if row else known.get("module"),
            "scheduled_start": row["scheduled_start"] if row else known.get("scheduled_start"),
            "scheduled_end": row["scheduled_end"] if row else known.get("scheduled_end"),
            "monitor_state": row["state"] if row else None, "monitor_reason": row["reason"] if row else None,
            "pipeline": backfill.pipeline(row) if row else [],
        }))
    with reading() as connection:
        qa_only = backfill.run_qa_only(connection, run)
    for row in qa_only:            # never queued: not a publisher lecture (the reason says what evidence it lacks)
        lectures.append(monitor.redact({
            "lecture_id": None, "qa_ref": row["qa_ref"], "session_date": row["session_date"],
            "status": "SKIPPED" if row["category"] == backfill.SKIPPED else "BLOCKED", "origin": row["origin"],
            "initial_category": row["category"], "reason": row["code"], "reason_detail": row["reason"],
            "missing_evidence": row["missing_evidence"], "stage": "SOURCE_DISCOVERY", "retries": 0,
            "started_at": None, "finished_at": None, "updated_at": None, "registered": False,
            "subject": row["subject"], "module": row["module"], "scheduled_start": None, "scheduled_end": None,
            "monitor_state": None, "monitor_reason": None, "pipeline": [], "sources": row["sources"]}))
    shaped = _shape(run, counts)
    shaped["qa_only"] = len(qa_only)
    return Response({"run": shaped, "lectures": lectures, "generated_at": generated_at,
                     "operator": is_operator(request.user)})


# --------------------------------------------------------------------------- writes (operators only)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsBackfillOperator])
@_handle
def start_view(request):
    """
    Start a run of a COMPLETE preview. Body: {preview_run_id, confirm: {library_id, eligible}, reason?}.
    Re-checks the preview against the current database before writing anything.
    """
    data = request.data or {}
    repository = BackfillRepository()
    with reading() as connection:
        try:
            preview = repository.get(connection, str(data.get("preview_run_id") or ""))
        except psycopg.errors.InvalidTextRepresentation:
            preview = None
        if preview is None or preview["mode"] != PREVIEW:
            return Response({"code": "PREVIEW_REQUIRED", "detail": "run a preview of this range first"},
                            status=status.HTTP_400_BAD_REQUEST)
        items = repository.items(connection, preview["run_id"])
    if preview["status"] in ("QUEUED", "RUNNING"):
        return _conflict("PREVIEW_NOT_READY", "the preview is still discovering the range")
    if preview["status"] != "COMPLETED":
        return _conflict("PREVIEW_INCOMPLETE", "the preview did not cover every day of the range; preview again",
                         incomplete_days=(preview["preview_summary"] or {}).get("incomplete_days"))
    if (datetime.now(timezone.utc) - preview["finished_at"]).total_seconds() > PREVIEW_TTL:
        return _conflict("PREVIEW_EXPIRED", "the preview is older than 30 minutes; preview the range again")
    library, summary = pm._library(), preview["preview_summary"] or {}
    if int(preview["library_id"]) != library:
        return _conflict("PREVIEW_MISMATCH", "the preview belongs to another library")
    confirm = data.get("confirm") or {}
    if str(confirm.get("library_id")) != str(library) or str(confirm.get("eligible")) != str(summary.get("eligible")):
        return _conflict("CONFIRMATION_MISMATCH", "the confirmation does not match the preview's library and "
                         "eligible lecture count", summary=summary)
    if not summary.get("eligible"):
        return _conflict("NOTHING_TO_QUEUE", "no lecture in this range is eligible for a backfill", summary=summary)
    rows = [i["detail"] for i in items]
    now = stored_categories(preview["date_from"], preview["date_to"])
    changed = [r["lecture_id"] for r in rows if r.get("basis") == "STORED" and now.get(r["lecture_id"]) != r["category"]]
    if changed:
        return _conflict("PREVIEW_STALE", "the stored state of some lectures changed since the preview; preview "
                         "again", changed=len(changed))
    reason = monitor.scrub_text(" ".join(str(data.get("reason") or "").split()))[:300]
    with _writing() as connection:
        with connection.transaction():
            run = repository.create(connection, date_from=preview["date_from"], date_to=preview["date_to"],
                                    library_id=library, requested_by=_actor(request), reason=reason,
                                    fingerprint=preview["preview_fingerprint"],
                                    summary={k: v for k, v in summary.items() if k != "per_day"},
                                    items=backfill.run_items({"lectures": rows}), mode=EXECUTE,
                                    preview_run_id=preview["run_id"])
            if run is None:
                active = repository.active(connection)
                return _conflict("BACKFILL_ALREADY_ACTIVE", "another historical backfill is running; wait for it "
                                 "to finish or cancel it", active=_shape(active))
            PublisherQueueRepository().event(
                connection, "HISTORICAL_BACKFILL_REQUESTED", actor=_actor(request),
                detail={"run_id": str(run["run_id"]), "preview_run_id": str(preview["run_id"]),
                        "date_from": _iso(preview["date_from"]), "date_to": _iso(preview["date_to"]),
                        "library_id": library, "eligible": summary["eligible"], "new": summary.get("new"),
                        "estimated_imports": (summary.get("estimated_imports") or {}).get("upper_bound")})
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


def _conflict(code: str, detail: str, **extra) -> Response:
    return Response({"code": code, "detail": detail, **extra}, status=status.HTTP_409_CONFLICT)
