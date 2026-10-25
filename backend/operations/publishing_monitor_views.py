"""
Lecture Publishing Monitor: read-only operations API (GET only).

Authenticated like every other operations route (DRF TokenAuthentication + IsAuthenticated,
config/settings.py). Every query runs on a READ ONLY connection (platform.reading); nothing here
writes, calls Microsoft Graph or Bunny, or returns a URL, key, token or transcript text.

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
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.publisher import monitor

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


@api_view(["GET"])
@_handle
def alerts_view(request):
    """The publisher's open alerts as stored: severity, kind, message, related lecture/job if named, times."""
    with reading() as connection:
        alerts = monitor.open_alerts(connection)
    counts = {}
    for alert in alerts:
        counts[alert["severity"]] = counts.get(alert["severity"], 0) + 1
    return Response({"alerts": alerts, "counts": counts})


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
