"""
Lecture Publishing Monitor: read-only operations API.

Authenticated like every other operations route (DRF TokenAuthentication + IsAuthenticated,
config/settings.py). Every publisher query runs on a READ ONLY connection (platform.reading); nothing
here writes publisher, LMS or Bunny state, calls Microsoft Graph or Bunny, or returns a URL, key,
token or transcript text.

The ONE kind of write is the signed-in user's own notification state for an alert (read / dismissed,
positive_mentions.PublisherAlertReview, the Django database). It never touches the publisher's alert:
reading or dismissing does not resolve the technical issue - only the reconciler does that.

The rules live in app.publisher.monitor (facts, classification, KPI definitions) and
app.publisher.delivery (READY / release / LMS playback - the same rule the LMS API uses). This module
only resolves a date range in parallel and serves the answer.

The summary and the lecture list are computed from ONE row set per date range, cached for a few
seconds, so a page refresh shows numbers and rows that agree.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
from django.conf import settings
from django.db.models import DateTimeField, F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.publisher import monitor

from positive_mentions.models import ALERT_DISMISS_REASONS, PublisherAlertReview

from . import platform
from .platform import reading

CAIRO = ZoneInfo("Africa/Cairo")
MAX_RANGE_DAYS = 31
MAX_PAGE_SIZE = 100
CACHE_SECONDS = 20

# Resolving one lecture is ~15 small queries; lectures with a video are resolved side by side, each on
# its own read connection (the day report's pattern in platform.py).
_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="publishing-monitor")
_cache: dict = {}
_flights: dict = {}
_cache_lock = threading.Lock()


class BadRequest(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _handle(view):
    def wrapper(request, *args, **kwargs):
        if settings.PUBLISHER_LMS_LIBRARY_ID is None:
            return Response({"code": "NOT_CONFIGURED", "detail": "PUBLISHER_LMS_LIBRARY_ID is not configured"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            return view(request, *args, **kwargs)
        except BadRequest as exc:
            return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        except (PlatformError, psycopg.Error):
            return Response({"code": "PLATFORM_UNAVAILABLE", "detail": "the publisher database could not answer"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _library() -> int:
    return int(settings.PUBLISHER_LMS_LIBRARY_ID)


def _language() -> str:
    return getattr(settings, "PUBLISHER_LMS_CAPTION_LANGUAGE", "en") or "en"


def cairo_today() -> date:
    return datetime.now(CAIRO).date()


def _range(params) -> tuple[date, date]:
    """date_from / date_to are Africa/Cairo business dates (lecture_sessions.session_date). Default: today."""
    today = cairo_today()
    try:
        date_from = date.fromisoformat(params.get("date_from") or today.isoformat())
        date_to = date.fromisoformat(params.get("date_to") or date_from.isoformat())
    except ValueError:
        raise BadRequest("INVALID_DATE", "date_from and date_to must be YYYY-MM-DD") from None
    if date_to < date_from:
        raise BadRequest("INVALID_RANGE", "date_to is before date_from")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise BadRequest("RANGE_TOO_LONG", f"choose at most {MAX_RANGE_DAYS} days")
    return date_from, date_to


def _rows(date_from: date, date_to: date) -> tuple[list[dict], str]:
    """Every lecture in the range, classified; cached for CACHE_SECONDS per (range, library, flag)."""
    library, enabled, language = _library(), bool(settings.PUBLISHER_LMS_PLAYBACK_ENABLED), _language()
    key = (date_from, date_to, library, enabled, language)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
        flight = _flights.setdefault(key, threading.Lock())
    # Single flight: the page asks for the summary and the table at the same moment; the second request
    # waits for the first one's rows instead of resolving the same range again.
    with flight:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return hit[1], hit[2]
        return _compute(key, date_from, date_to, library, enabled, language)


def _compute(key, date_from, date_to, library, enabled, language) -> tuple[list[dict], str]:
    with reading() as connection:
        now = connection.execute("SELECT now()").fetchone()[0]
        facts = monitor.load_facts(connection, date_from, date_to, library)

    def resolve(fact):
        with reading() as connection:
            return fact["lecture_id"], monitor.resolve(connection, fact, library_id=library, language=language)

    views = dict(_workers.map(resolve, [f for f in facts if monitor.needs_resolution(f)]))
    rows = [monitor.classify(f, views.get(f["lecture_id"]), playback_enabled=enabled, library_id=library, now=now)
            for f in facts]
    generated_at = now.isoformat()
    with _cache_lock:
        for stale in [k for k, v in _cache.items() if time.monotonic() - v[0] >= CACHE_SECONDS]:
            del _cache[stale]
            if stale != key:
                _flights.pop(stale, None)
        _cache[key] = (time.monotonic(), rows, generated_at)
    return rows, generated_at


def _choice(params, name: str, allowed) -> str:
    value = (params.get(name) or "").strip().upper()
    if value and value not in allowed:
        raise BadRequest("INVALID_FILTER", f"{name} must be one of {', '.join(allowed)}")
    return value


def _positive(params, name: str, default: int, maximum: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    if not str(raw).isdigit() or int(raw) < 1:
        raise BadRequest("INVALID_PAGE", f"{name} must be a positive number")
    return min(int(raw), maximum)


@api_view(["GET"])
@_handle
def health_view(request):
    """Publisher service health from fresh heartbeats and the task schedule (never from a stale record)."""
    with reading() as connection:
        return Response(monitor.health(connection, library_id=_library(),
                                       playback_enabled=bool(settings.PUBLISHER_LMS_PLAYBACK_ENABLED)))


# --------------------------------------------------------------------------- alerts and reviews
HISTORY_DAYS = 30
NOTE_MAX = 300
DISMISS_REASONS = {"TEST_LMS_SESSION": "Test LMS session", "NO_LMS_SESSION_INTENDED": "Intentionally no LMS session",
                   "KNOWN_TEST_UPLOAD": "Known test upload", "OTHER": "Other"}
assert tuple(DISMISS_REASONS) == ALERT_DISMISS_REASONS
LMS_CACHE_SECONDS, LMS_RETRY_SECONDS = 600, 60
_lms_cache: dict = {}
_lms_lock = threading.Lock()


class NotFound(Exception):
    pass


def _lms_records(occurrence_ids) -> dict:
    """The LMS's own record of each occurrence (title, session number, whether it took place), cached.

    Optional: without LMS_DATABASE_URL, or when the LMS cannot answer, the publisher's coverage row is
    still shown and `source.status` says why the LMS record is missing.
    """
    wanted = sorted({o for o in occurrence_ids if o})
    if not wanted:
        return {}
    url = platform.settings().lms_database_url
    if not url:
        return {o: {"status": "NOT_CONFIGURED"} for o in wanted}
    now = time.monotonic()
    with _lms_lock:
        fresh = {o: hit[1] for o in wanted if (hit := _lms_cache.get(o)) and now < hit[0]}
    missing = [o for o in wanted if o not in fresh]
    if missing:
        try:
            found = monitor.lms_sessions(url, missing)
            got = {o: found.get(o) or {"status": "NOT_FOUND"} for o in missing}
            until = now + LMS_CACHE_SECONDS
        except psycopg.Error:
            got = {o: {"status": "UNAVAILABLE"} for o in missing}
            until = now + LMS_RETRY_SECONDS
        with _lms_lock:
            for o, record in got.items():
                _lms_cache[o] = (until, record)
        fresh.update(got)
    return fresh


def review_state(alert: dict, review) -> str:
    """NEW / READ / DISMISSED / RESOLVED for the signed-in user; INFO for the informational digest."""
    if alert["severity"] == monitor.DIGEST:
        return "INFO"
    if review is not None and review.dismissed_at:
        return "DISMISSED"
    if alert["status"] == "RESOLVED":
        return "RESOLVED"
    if review is not None and review.read_at:
        return "READ"
    return "NEW"


def _review(alert: dict, review) -> dict:
    return monitor.redact({
        "state": review_state(alert, review),
        "read_at": review.read_at.isoformat() if review and review.read_at else None,
        "dismissed_at": review.dismissed_at.isoformat() if review and review.dismissed_at else None,
        "dismiss_reason": (review.dismiss_reason or None) if review else None,
        "dismiss_note": (review.dismiss_note or None) if review else None,
    })


@api_view(["GET"])
@_handle
def alerts_view(request):
    """The publisher's alerts as stored - open ones, and those resolved in the last 30 days - each with
    the signed-in user's own review state (NEW / READ / DISMISSED / RESOLVED; the digest is INFO)."""
    with reading() as connection:
        alerts = monitor.open_alerts(connection)
        history = monitor.resolved_alerts(connection, days=HISTORY_DAYS)
    records = _lms_records(a["lms"]["occurrence_id"] for a in alerts + history if a["lms"])
    reviews = {str(r.alert_id): r for r in PublisherAlertReview.objects.filter(
        user=request.user, alert_id__in=[a["alert_id"] for a in alerts + history])}
    counts, review_counts = {}, {"new": 0, "new_by_severity": {}, "read": 0, "dismissed": 0, "resolved": 0}
    for alert in alerts + history:
        if alert["lms"]:
            alert["lms"]["source"] = records.get(alert["lms"]["occurrence_id"])
        alert["review"] = _review(alert, reviews.get(alert["alert_id"]))
        state = alert["review"]["state"]
        if alert["status"] == "OPEN":
            counts[alert["severity"]] = counts.get(alert["severity"], 0) + 1
            if state == "NEW":
                by_severity = review_counts["new_by_severity"]
                by_severity[alert["severity"]] = by_severity.get(alert["severity"], 0) + 1
        if state != "INFO":
            review_counts[state.lower()] += 1
    return Response({"alerts": alerts, "history": history, "history_days": HISTORY_DAYS, "counts": counts,
                     "review_counts": review_counts,
                     "dismiss_reasons": [{"code": c, "label": t} for c, t in DISMISS_REASONS.items()]})


def _reviewable(alert_id) -> dict:
    with reading() as connection:
        ref = monitor.alert_ref(connection, str(alert_id))
    if ref is None:
        raise NotFound()
    if ref["severity"] == monitor.DIGEST:
        raise BadRequest("INFORMATIONAL", "the daily digest is informational and has no read or dismiss state")
    return ref


def _mine(request, alert_id):
    """The user's review row for the alert, created if absent - safe under concurrency (ON CONFLICT DO NOTHING)."""
    PublisherAlertReview.objects.bulk_create([PublisherAlertReview(user=request.user, alert_id=alert_id)],
                                             ignore_conflicts=True)
    return PublisherAlertReview.objects.filter(user=request.user, alert_id=alert_id)


def _answer(ref: dict, rows) -> Response:
    return Response({"alert_id": ref["alert_id"], "review": _review(ref, rows.first())})


def _review_action(view):
    def wrapper(request, alert_id):
        try:
            return view(request, alert_id, _reviewable(alert_id))
        except NotFound:
            return Response({"code": "ALERT_NOT_FOUND", "detail": "no such publisher alert"},
                            status=status.HTTP_404_NOT_FOUND)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _at(now):
    return Value(now, output_field=DateTimeField())


@api_view(["POST"])
@_handle
@_review_action
def alert_read_view(request, alert_id, ref):
    """Mark the alert read for the signed-in user (idempotent: the first read time is kept)."""
    now = timezone.now()
    rows = _mine(request, alert_id)
    rows.update(read_at=Coalesce(F("read_at"), _at(now)), updated_at=now)
    return _answer(ref, rows)


@api_view(["POST"])
@_handle
@_review_action
def alert_dismiss_view(request, alert_id, ref):
    """Dismiss an open alert for the signed-in user, with a reason (and a note, required for OTHER)."""
    reason = str(request.data.get("reason") or "").strip().upper()
    note = monitor.scrub_text(" ".join(str(request.data.get("note") or "").split()))
    if reason not in DISMISS_REASONS:
        raise BadRequest("INVALID_REASON", f"reason must be one of {', '.join(DISMISS_REASONS)}")
    if len(note) > NOTE_MAX:
        raise BadRequest("NOTE_TOO_LONG", f"the note is at most {NOTE_MAX} characters")
    if reason == "OTHER" and not note:
        raise BadRequest("NOTE_REQUIRED", "describe the reason when it is Other")
    if ref["status"] != "OPEN":
        return Response({"code": "ALERT_RESOLVED", "detail": "the publisher has already resolved this alert"},
                        status=status.HTTP_409_CONFLICT)
    now = timezone.now()
    rows = _mine(request, alert_id)
    rows.update(dismissed_at=Coalesce(F("dismissed_at"), _at(now)), dismiss_reason=reason, dismiss_note=note,
                read_at=Coalesce(F("read_at"), _at(now)), updated_at=now)
    return _answer(ref, rows)


@api_view(["POST"])
@_handle
@_review_action
def alert_restore_view(request, alert_id, ref):
    """Undo the signed-in user's dismissal (the alert stays read)."""
    rows = PublisherAlertReview.objects.filter(user=request.user, alert_id=alert_id)
    rows.filter(dismissed_at__isnull=False).update(dismissed_at=None, dismiss_reason="", dismiss_note="",
                                                   updated_at=timezone.now())
    return _answer(ref, rows)


@api_view(["GET"])
@_handle
def summary_view(request):
    """The KPI counts for a date range (lectures, never parts or job attempts), with their definitions."""
    date_from, date_to = _range(request.query_params)
    rows, generated_at = _rows(date_from, date_to)
    return Response({"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                     "generated_at": generated_at, "library_id": _library(),
                     "playback_enabled": bool(settings.PUBLISHER_LMS_PLAYBACK_ENABLED),
                     "total": len(rows), **monitor.summarize(rows)})


@api_view(["GET"])
@_handle
def lectures_view(request):
    """One row per lecture in the range, filtered and paginated on the server."""
    params = request.query_params
    date_from, date_to = _range(params)
    kpi = (params.get("kpi") or "").strip().lower()
    if kpi and kpi not in monitor.KPIS:
        raise BadRequest("INVALID_FILTER", f"kpi must be one of {', '.join(monitor.KPIS)}")
    filters = {
        "q": (params.get("q") or "")[:120], "kpi": kpi, "state": _choice(params, "state", monitor.STATES),
        "video": _choice(params, "video", monitor.FILTERS["video"]),
        "playback": _choice(params, "playback", monitor.FILTERS["playback"]),
        "captions": _choice(params, "captions", monitor.FILTERS["captions"]),
        "transcript": _choice(params, "transcript", monitor.FILTERS["transcript"]),
        "attention": (params.get("attention") or "").lower() in ("1", "true", "yes"),
    }
    page, page_size = _positive(params, "page", 1, 10_000), _positive(params, "page_size", 25, MAX_PAGE_SIZE)
    rows, generated_at = _rows(date_from, date_to)
    matched = monitor.apply_filters(rows, **filters)
    start = (page - 1) * page_size
    return Response({"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                     "generated_at": generated_at, "range_total": len(rows), "total": len(matched),
                     "page": page, "page_size": page_size, "results": matched[start:start + page_size]})


@api_view(["GET"])
@_handle
def lecture_view(request, lecture_id):
    """One lecture in full: identity, LMS mapping, recording, Bunny parts, captions, release, jobs, timeline."""
    with reading() as connection:
        detail = monitor.lecture_detail(connection, str(lecture_id), library_id=_library(),
                                        playback_enabled=bool(settings.PUBLISHER_LMS_PLAYBACK_ENABLED),
                                        language=_language())
    if detail is None:
        return Response({"code": "LECTURE_NOT_FOUND", "detail": "no such lecture"}, status=status.HTTP_404_NOT_FOUND)
    return Response(detail)
