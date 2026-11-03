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

import json
import secrets
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import views as auth_views
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


class DocsLoginView(auth_views.LoginView):
    """Staff accounts only: AdminAuthenticationForm refuses anyone else before a session exists."""
    template_name = "operations/api_docs_login.html"
    authentication_form = AdminAuthenticationForm
    next_page = reverse_lazy("api-docs")


login_view = documentation(content_policy(LOGIN_POLICY)(DocsLoginView.as_view()))
logout_view = documentation(auth_views.LogoutView.as_view(next_page=reverse_lazy(LOGIN)))
