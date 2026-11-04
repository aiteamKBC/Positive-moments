"""
Interactive documentation of the LMS delivery API (contract v1), for Kent's own developers.

    GET      /api/docs/          Swagger UI over the document below
    GET      /api/schema/        the OpenAPI document, docs/api/publisher_lms_v1.openapi.json, as written
    GET/POST /api/docs/login/    Django staff sign-in (username + password, a normal Django session)
    POST     /api/docs/logout/

WHO MAY READ IT
    Nobody unless PUBLISHER_API_DOCS_ENABLED: every route above answers 404, before any sign-in.
    When enabled: an active Django staff account (is_active and is_staff). The LMS service token is
    neither asked for nor accepted here, and the page can never call the API: Try It Out and the
    Authorize dialog are switched off, so no token is ever typed into a browser.

    Over plain HTTP nothing here is served: OpenLiteSpeed redirects GET/HEAD to https:// and refuses
    every other method (deploy/kbc_api_https_rules.py), so the sign-in form never travels, and a
    password is never accepted, in clear text.

    Password guessing: failed sign-ins are counted in Django's cache for 15 minutes, per username
    (5) and per client address (30; behind the proxy that address is shared, so it is a ceiling for
    everyone). Past either limit the sign-in answers 429 without checking the password. A
    successful sign-in clears its username's count. Counted per server process (the default
    local-memory cache), like the LMS API's own rate limit, which this does not touch.

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
import json
import secrets
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import views as auth_views
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

SWAGGER_UI_VERSION = "5.17.14"
SWAGGER_UI_BASE = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/"
SWAGGER_UI_JS = ("swagger-ui-bundle.js", "sha384-wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep")
SWAGGER_UI_CSS = ("swagger-ui.css", "sha384-wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn")
LOGIN = "api-docs-login"
LOGIN_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'")
FAILED_SIGN_IN_WINDOW = 15 * 60                 # seconds a failed sign-in counts
MAX_FAILED_PER_USERNAME = 5
MAX_FAILED_PER_ADDRESS = 30                     # REMOTE_ADDR only: a forwarded-for header can be forged


def schema_file() -> Path:
    return Path(settings.REPO_ROOT) / "docs" / "api" / "publisher_lms_v1.openapi.json"


def documentation(view):
    """404 unless the documentation is switched on - checked before sign-in, so it is not discoverable."""
    @wraps(view)
    def enabled(request, *args, **kwargs):
        if not settings.PUBLISHER_API_DOCS_ENABLED:
            raise Http404
        return view(request, *args, **kwargs)
    return enabled


def content_policy(policy):
    def decorate(view):
        @wraps(view)
        def with_policy(request, *args, **kwargs):
            response = view(request, *args, **kwargs)
            response["Content-Security-Policy"] = policy
            return response
        return with_policy
    return decorate


@documentation
@require_GET
@never_cache
@staff_member_required(login_url=LOGIN)
def docs_view(request):
    nonce = secrets.token_urlsafe(16)
    response = render(request, "operations/api_docs.html", {
        "nonce": nonce, "version": SWAGGER_UI_VERSION,
        "js_url": SWAGGER_UI_BASE + SWAGGER_UI_JS[0], "js_integrity": SWAGGER_UI_JS[1],
        "css_url": SWAGGER_UI_BASE + SWAGGER_UI_CSS[0], "css_integrity": SWAGGER_UI_CSS[1],
    })
    response["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}' {SWAGGER_UI_BASE}{SWAGGER_UI_JS[0]}; "
        f"style-src {SWAGGER_UI_BASE}{SWAGGER_UI_CSS[0]} 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    return response


@documentation
@require_GET
@never_cache
@staff_member_required(login_url=LOGIN)
def schema_view(request):
    with schema_file().open(encoding="utf-8") as handle:
        return JsonResponse(json.load(handle), json_dumps_params={"indent": 2})


def sign_in_counters(request) -> list[tuple[str, int]]:
    """(cache key, limit) of the failed sign-ins this attempt is counted against: its username, its address."""
    username = (request.POST.get("username") or "").strip().lower()
    return [(f"api-docs-login:user:{hashlib.sha256(username.encode()).hexdigest()}", MAX_FAILED_PER_USERNAME),
            (f"api-docs-login:addr:{request.META.get('REMOTE_ADDR', '')}", MAX_FAILED_PER_ADDRESS)]


def _count_failure(key: str) -> None:
    if not cache.add(key, 1, FAILED_SIGN_IN_WINDOW):     # the window runs from the first failure
        try:
            cache.incr(key)
        except ValueError:                               # expired between add and incr
            cache.add(key, 1, FAILED_SIGN_IN_WINDOW)


class DocsLoginView(auth_views.LoginView):
    """Staff accounts only: AdminAuthenticationForm refuses anyone else before a session exists."""
    template_name = "operations/api_docs_login.html"
    authentication_form = AdminAuthenticationForm
    next_page = reverse_lazy("api-docs")

    def post(self, request, *args, **kwargs):
        if any(cache.get(key, 0) >= limit for key, limit in sign_in_counters(request)):
            # Refused before the password is checked; an unbound form, so nothing authenticates.
            response = self.render_to_response(self.get_context_data(
                form=self.get_form_class()(request),
                refused="Too many failed sign-in attempts. Try again in 15 minutes."), status=429)
            response["Retry-After"] = str(FAILED_SIGN_IN_WINDOW)
            return response
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key, _ in sign_in_counters(self.request):
            _count_failure(key)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(sign_in_counters(self.request)[0][0])   # this account's own count starts again
        return super().form_valid(form)


login_view = documentation(content_policy(LOGIN_POLICY)(DocsLoginView.as_view()))
logout_view = documentation(auth_views.LogoutView.as_view(next_page=reverse_lazy(LOGIN)))
