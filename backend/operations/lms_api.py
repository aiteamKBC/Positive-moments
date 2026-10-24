"""
Continuous Bunny Publisher -> LMS backend: the delivery API, contract v1.

    GET /api/publisher/v1/occurrences/{lms_occurrence_id}/recording
    GET /api/publisher/v1/occurrences?changed_since=<ISO 8601>&cursor=<opaque>&limit=<1..50>
    GET /api/publisher/v1/health

WHO MAY CALL
    The LMS backend only: `Authorization: Bearer <token>` where the token is one
    of PUBLISHER_LMS_SERVICE_TOKENS (>= 32 characters; several = an overlapping
    rotation). Compared in constant time against every configured token. No
    user login, no session, no CSRF surface. No token configured = 401 for all.

WHAT IT ANSWERS
    Read-only (PostgreSQL READ ONLY transactions via platform.reading). Every
    answer is computed live by app.publisher.delivery from the same readiness
    rule the publisher itself uses. Learner entitlement and playback signing are
    the LMS backend's: this API never sees a learner and never signs anything.

SAFETY
    Rate limited per service token (PUBLISHER_LMS_RATE). One access-log line per
    request on the `publisher.lms_api` logger: route, status, duration, the
    token's fingerprint (never the token), the occurrence id. Responses are
    `Cache-Control: no-store`. Errors never echo an exception message.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import psycopg
from django.conf import settings
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from app.common.errors import PlatformError
from app.publisher import delivery
from app.publisher.operations import OperatorActionRefused

from .platform import reading

MIN_TOKEN_LENGTH = 32
MAX_FEED_LIMIT = 50              # each item is a live readiness computation (~25 small queries)
DEFAULT_FEED_LIMIT = 20
access_log = logging.getLogger("publisher.lms_api")


# =========================================================================== authentication
class LmsService:
    """The authenticated caller: the LMS backend, identified by its token's fingerprint only."""
    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = False

    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        self.username = f"lms-service:{fingerprint}"

    def __str__(self):
        return self.username


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def configured_tokens() -> list[str]:
    return [t for t in settings.PUBLISHER_LMS_SERVICE_TOKENS if len(t) >= MIN_TOKEN_LENGTH]


class LmsServiceTokenAuthentication(BaseAuthentication):
    """Bearer service token, constant-time against every configured token (no early exit)."""

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None                                      # -> 401 with WWW-Authenticate
        scheme, _, sent = header.partition(" ")
        sent = sent.strip()
        if scheme != "Bearer" or not sent:
            raise AuthenticationFailed("Use Authorization: Bearer <service token>.")
        matched = None
        for token in configured_tokens():
            if hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8")) and matched is None:
                matched = token
        if matched is None:
            raise AuthenticationFailed("Invalid service token.")
        service = LmsService(fingerprint(matched))
        return service, service.fingerprint

    def authenticate_header(self, request):
        return 'Bearer realm="publisher-lms"'


class IsLmsService(BasePermission):
    message = "Only the LMS backend may call this API."

    def has_permission(self, request, view):
        return isinstance(getattr(request, "user", None), LmsService)


class LmsServiceThrottle(SimpleRateThrottle):
    """Per service token (by fingerprint). Uses Django's cache: per process unless a shared cache is set."""
    scope = "publisher_lms"

    def get_rate(self):
        return settings.PUBLISHER_LMS_RATE

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        ident = user.fingerprint if isinstance(user, LmsService) else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


# =========================================================================== plumbing
def _lms_route(route_name: str):
    """DRF function view for the LMS: service token, throttle, refusal mapping, access log, no-store."""
    def decorate(view):
        @functools.wraps(view)
        def handled(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except OperatorActionRefused as exc:
                code = status.HTTP_400_BAD_REQUEST if exc.code.startswith("INVALID") else status.HTTP_409_CONFLICT
                return Response({"code": exc.code, "detail": exc.detail}, status=code)
            except (PlatformError, psycopg.Error):
                access_log.warning(json.dumps({"event": "lms_api_platform_error", "route": route_name}))
                return Response({"code": "PLATFORM_UNAVAILABLE", "detail": "the publisher database could not answer"},
                                status=status.HTTP_503_SERVICE_UNAVAILABLE)

        drf_view = api_view(["GET"])(throttle_classes([LmsServiceThrottle])(
            permission_classes([IsLmsService])(authentication_classes([LmsServiceTokenAuthentication])(handled))))

        @functools.wraps(view)
        def logged(request, *args, **kwargs):
            started = time.monotonic()
            response = drf_view(request, *args, **kwargs)
            response["Cache-Control"] = "no-store"
            response["X-Publisher-Contract"] = delivery.CONTRACT_VERSION
            user = getattr(request, "user", None)
            access_log.info(json.dumps({
                "event": "lms_api_access", "route": route_name, "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "client": user.fingerprint if isinstance(user, LmsService) else "unauthenticated",
                "occurrence_id": kwargs.get("occurrence_id"), "remote_addr": request.META.get("REMOTE_ADDR")}))
            return response
        return logged
    return decorate


def _library():
    library = settings.PUBLISHER_LMS_LIBRARY_ID
    if library is None:
        raise _NotConfigured()
    return library


class _NotConfigured(Exception):
    pass


def _not_configured():
    return Response({"code": "NOT_CONFIGURED", "detail": "the delivery library is not configured"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)


# =========================================================================== routes
@_lms_route("occurrence_recording")
def occurrence_recording(request, occurrence_id):
    try:
        library = _library()
    except _NotConfigured:
        return _not_configured()
    with reading() as connection:
        body = delivery.occurrence_recording(connection, occurrence_id, library_id=library,
                                             playback_enabled=settings.PUBLISHER_LMS_PLAYBACK_ENABLED,
                                             language=settings.PUBLISHER_LMS_CAPTION_LANGUAGE)
    if body is None:
        return Response({"code": "OCCURRENCE_NOT_FOUND", "detail": "the publisher has never seen this occurrence"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(body)


@_lms_route("occurrence_feed")
def occurrence_feed(request):
    try:
        library = _library()
    except _NotConfigured:
        return _not_configured()
    params = request.query_params
    since = None
    if params.get("changed_since"):
        since = parse_datetime(params["changed_since"].replace(" ", "+"))
        if since is None or since.tzinfo is None:
            raise OperatorActionRefused("INVALID_CHANGED_SINCE",
                                        "changed_since must be ISO 8601 with a timezone, e.g. 2026-10-24T00:00:00Z")
    raw_limit = params.get("limit", str(DEFAULT_FEED_LIMIT))
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_FEED_LIMIT:
        raise OperatorActionRefused("INVALID_LIMIT", f"limit must be 1..{MAX_FEED_LIMIT}")
    with reading() as connection:
        body = delivery.occurrence_feed(connection, library_id=library,
                                        playback_enabled=settings.PUBLISHER_LMS_PLAYBACK_ENABLED,
                                        changed_since=since, cursor=params.get("cursor") or None,
                                        limit=int(raw_limit), language=settings.PUBLISHER_LMS_CAPTION_LANGUAGE)
    return Response(body)


@_lms_route("health")
def health(request):
    library = settings.PUBLISHER_LMS_LIBRARY_ID
    database = "ok"
    try:
        with reading() as connection:
            present = connection.execute("SELECT to_regclass('public.bunny_videos') IS NOT NULL").fetchone()[0]
            database = "ok" if present else "schema_missing"
    except (PlatformError, psycopg.Error):
        database = "unavailable"
    healthy = database == "ok" and library is not None
    return Response({"contract_version": delivery.CONTRACT_VERSION, "status": "ok" if healthy else "degraded",
                     "library_id": library, "playback_enabled": settings.PUBLISHER_LMS_PLAYBACK_ENABLED,
                     "database": database, "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
                    status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE)
