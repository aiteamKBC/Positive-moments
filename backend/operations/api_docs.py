"""
Interactive documentation of the LMS delivery API (contract v1), for the users of the KBC Lecture
Intelligence Dashboard.

    GET  /api/docs/           Swagger UI over the document below
    GET  /api/schema/         the OpenAPI document, docs/api/publisher_lms_v1.openapi.json, as written
    POST /api/docs/session/   opens documentation access for a signed-in Dashboard user (see below)

WHO MAY READ IT
    Nobody unless PUBLISHER_API_DOCS_ENABLED: every route above answers 404.
    When enabled: exactly the people who may use the Dashboard - an active user holding a current
    Dashboard API token (the Dashboard's own TokenAuthentication + IsAuthenticated). There is no
    second login and no separate account.

    The Dashboard keeps its token in browser storage and sends it as a header, which a plain page
    navigation cannot carry. So the Dashboard's /api-documentation page first POSTs to
    /api/docs/session/ with that header. The answer is 204 with an empty body; the server writes a
    documentation-only grant into the existing Django session (an HttpOnly, SameSite=Lax cookie,
    Secure in production, holding only a random session id, issued afresh for the grant):
      * the user, a SHA-256 digest of their current Dashboard token, and an expiry 1 hour ahead;
      * it does NOT sign the user in to Django: no other route accepts it (every API route
        authenticates by token only, and /admin/ is not served in production).
    /api/docs/ and /api/schema/ honour the grant only while it is unexpired, the user is active
    and the digest still matches their current token, so signing out of the Dashboard (which
    deletes the token) ends documentation access at once. Without a valid grant /api/docs/ sends
    the browser to the Dashboard's /api-documentation page (Microsoft sign-in if needed, then a
    fresh grant) and /api/schema/ answers 403. No token ever appears in a URL, a page or a log.

    The LMS service token is neither asked for nor accepted, and the page can never call the API:
    Try It Out and the Authorize dialog are switched off.

    Over plain HTTP nothing here is served: OpenLiteSpeed redirects GET/HEAD to https:// and refuses
    every other method (deploy/kbc_api_https_rules.py).

WHAT IT SHOWS
    Only the three LMS routes: the document is hand-written, and the tests hold it to
    operations/lms_urls.py and to the real response bodies. Nothing is read from settings or the
    database, so no token, key or credential can appear in it.

BROWSER SAFETY
    Swagger UI is one pinned swagger-ui-dist release from jsDelivr with Subresource Integrity; the
    page sends a Content-Security-Policy that allows exactly those two files plus its own nonce'd
    script, and nothing is cached (Cache-Control: no-store).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view
from rest_framework.response import Response

SWAGGER_UI_VERSION = "5.17.14"
SWAGGER_UI_BASE = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/"
SWAGGER_UI_JS = ("swagger-ui-bundle.js", "sha384-wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep")
SWAGGER_UI_CSS = ("swagger-ui.css", "sha384-wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn")
GRANT = "api_docs_grant"
GRANT_SECONDS = 60 * 60
DASHBOARD_ENTRY = "/api-documentation"         # the Dashboard page that opens access (frontend/src/router.ts)


def schema_file() -> Path:
    return Path(settings.REPO_ROOT) / "docs" / "api" / "publisher_lms_v1.openapi.json"


def documentation(view):
    """404 unless the documentation is switched on - checked first, so it is not discoverable."""
    @wraps(view)
    def enabled(request, *args, **kwargs):
        if not settings.PUBLISHER_API_DOCS_ENABLED:
            raise Http404
        return view(request, *args, **kwargs)
    return enabled


def _digest(token_key: str) -> str:
    return hashlib.sha256(token_key.encode("utf-8")).hexdigest()


def dashboard_user(request):
    """The user whose documentation grant is still good: unexpired, active, same Dashboard token. Else None."""
    grant = request.session.get(GRANT)
    if not isinstance(grant, dict) or not isinstance(grant.get("until"), (int, float)) or grant["until"] <= time.time():
        return None
    token = Token.objects.select_related("user").filter(user_id=grant.get("user")).first()
    if token is None or not token.user.is_active:
        return None
    if not hmac.compare_digest(_digest(token.key), str(grant.get("token", ""))):
        return None
    return token.user


@documentation
@api_view(["POST"])
def session_view(request):
    """The Dashboard's own authentication and permission (token, IsAuthenticated) open the grant."""
    if not isinstance(request.auth, Token):
        return Response({"detail": "Sign in to the Dashboard first."}, status=status.HTTP_403_FORBIDDEN)
    request.session.cycle_key()                         # the grant never rides on a session id set before it
    request.session[GRANT] = {"user": request.user.pk, "token": _digest(request.auth.key),
                              "until": time.time() + GRANT_SECONDS}
    return Response(status=status.HTTP_204_NO_CONTENT)


@documentation
@require_GET
@never_cache
def docs_view(request):
    user = dashboard_user(request)
    if user is None:
        return HttpResponseRedirect(DASHBOARD_ENTRY)
    nonce = secrets.token_urlsafe(16)
    response = render(request, "operations/api_docs.html", {
        "nonce": nonce, "version": SWAGGER_UI_VERSION, "username": user.get_username(),
        "js_url": SWAGGER_UI_BASE + SWAGGER_UI_JS[0], "js_integrity": SWAGGER_UI_JS[1],
        "css_url": SWAGGER_UI_BASE + SWAGGER_UI_CSS[0], "css_integrity": SWAGGER_UI_CSS[1],
    })
    response["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}' {SWAGGER_UI_BASE}{SWAGGER_UI_JS[0]}; "
        f"style-src {SWAGGER_UI_BASE}{SWAGGER_UI_CSS[0]} 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
    return response


@documentation
@require_GET
@never_cache
def schema_view(request):
    if dashboard_user(request) is None:
        return JsonResponse({"detail": "Open the API documentation from the Dashboard."}, status=403)
    with schema_file().open(encoding="utf-8") as handle:
        return JsonResponse(json.load(handle), json_dumps_params={"indent": 2})
