"""
API documentation (/api/docs/, /api/schema/): off by default; open to exactly the Dashboard's users through
the Dashboard's own token (a documentation-only session grant, no second sign-in, no separate account);
Swagger UI pinned with Subresource Integrity and Try It Out off; and a document that describes exactly the
three LMS routes - with their real authentication, headers and error bodies - and nothing internal or
secret. Over plain HTTP no docs route is ever served (the OpenLiteSpeed rules).

No platform database is opened: the LMS routes' reads are patched, as in test_lms_api. That the real
response bodies conform to the document is tests/integration/test_publisher_lms_openapi.py.
"""
import hashlib
import importlib.util
import json
import logging
import re
import time
from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from operations import api_docs
from operations.tests.test_lms_api import TOKEN, fake_connection, routes
from tests.ols_rewrite import evaluate
from tests.openapi_contract import load, problems, references, resolve, schema

DOCS, SESSION, SCHEMA = "/api/docs/", "/api/docs/session/", "/api/schema/"
ENTRY = "/api-documentation"                       # the Dashboard page behind its "API docs" link
LMS_BASE = "/api/publisher/v1"
ENABLED = override_settings(PUBLISHER_API_DOCS_ENABLED=True)
LMS = dict(PUBLISHER_LMS_SERVICE_TOKENS=[TOKEN], PUBLISHER_LMS_LIBRARY_ID=796471,
           PUBLISHER_LMS_PLAYBACK_ENABLED=True, PUBLISHER_LMS_RATE="1000/min")
SPEC = load()
_rules = importlib.util.spec_from_file_location(
    "kbc_api_https_rules", f"{settings.REPO_ROOT}/deploy/kbc_api_https_rules.py")
HTTPS_RULES = importlib.util.module_from_spec(_rules)
_rules.loader.exec_module(HTTPS_RULES)


def dashboard_user(username="docs-user", *, active=True):
    """A Dashboard user as Microsoft sign-in leaves one: not staff, no password, one API token."""
    user = get_user_model().objects.create_user(username, is_staff=False, is_active=active)
    return user, Token.objects.create(user=user)


def granted(token):
    """A browser after the Dashboard's "API docs" link: one POST with the token header, then cookies only.
    CSRF checks are on, as for a real browser: the token-authenticated POST needs no CSRF token."""
    client = Client(enforce_csrf_checks=True)
    response = client.post(SESSION, HTTP_AUTHORIZATION=f"Token {token.key}")
    assert response.status_code == 204, response.status_code
    return client


def all_routes(resolver=None, prefix="/"):
    """Every URL pattern Django serves, as '/route/{param}'."""
    for entry in (resolver or get_resolver()).url_patterns:
        route = prefix + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", str(entry.pattern))
        if isinstance(entry, URLResolver):
            yield from all_routes(entry, route)
        elif isinstance(entry, URLPattern):
            yield route


def documented_routes():
    base = urlsplit(SPEC["servers"][0]["url"].replace("{host}", "host")).path
    return {base + path for path in SPEC["paths"]}


# =========================================================================== the switch and who may read
class SwitchTests(TestCase):
    def test_off_unless_switched_on(self):
        self.assertIs(settings.PUBLISHER_API_DOCS_ENABLED, False)   # the test environment sets nothing

    @override_settings(PUBLISHER_API_DOCS_ENABLED=False)
    def test_switched_off_every_docs_route_is_not_found_even_for_a_dashboard_user(self):
        _, token = dashboard_user()
        client = Client(HTTP_AUTHORIZATION=f"Token {token.key}")
        for path in (DOCS, SCHEMA):
            self.assertEqual(client.get(path).status_code, 404, path)
        self.assertEqual(client.post(SESSION).status_code, 404)


@ENABLED
class DashboardAccessTests(TestCase):
    def test_a_dashboard_user_opens_the_documentation_without_signing_in_again(self):
        _, token = dashboard_user()
        client = granted(token)
        page = client.get(DOCS)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "docs-user")
        response = client.get(SCHEMA)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/json"))
        self.assertEqual(response.json(), SPEC)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn(client.post(DOCS).status_code, (403, 405))         # read-only: CSRF or method refusal

    @override_settings(**LMS)
    def test_only_a_current_dashboard_token_opens_access(self):
        _, disabled = dashboard_user("docs-disabled", active=False)
        for header in (None, "Token not-a-dashboard-token", f"Bearer {TOKEN}", f"Token {disabled.key}"):
            extra = {"HTTP_AUTHORIZATION": header} if header else {}
            client = Client()
            self.assertEqual(client.post(SESSION, **extra).status_code, 401, header and header[:6])
            self.assertEqual(client.get(DOCS).status_code, 302)
        _, token = dashboard_user()
        self.assertEqual(Client().get(SESSION, HTTP_AUTHORIZATION=f"Token {token.key}").status_code, 405)

    @override_settings(**LMS)
    def test_without_a_grant_the_page_goes_to_the_dashboard_and_the_schema_is_refused(self):
        _, token = dashboard_user()
        for header in (None, f"Token {token.key}", f"Bearer {TOKEN}"):   # a header alone opens nothing
            client = Client(**({"HTTP_AUTHORIZATION": header} if header else {}))
            page = client.get(DOCS)
            self.assertEqual((page.status_code, page["Location"]), (302, ENTRY))
            refused = client.get(SCHEMA)
            self.assertEqual(refused.status_code, 403)
            self.assertNotIn("openapi", refused.content.decode())

    def test_signing_out_of_the_dashboard_ends_documentation_access_at_once(self):
        _, token = dashboard_user()
        client = granted(token)
        dashboard = APIClient()
        dashboard.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(dashboard.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(client.get(DOCS)["Location"], ENTRY)
        self.assertEqual(client.get(SCHEMA).status_code, 403)

    def test_a_new_token_a_deactivated_user_or_an_hour_later_ends_access(self):
        user, token = dashboard_user()
        client = granted(token)
        token.delete()
        Token.objects.create(user=user)
        self.assertEqual(client.get(DOCS).status_code, 302)

        user, token = dashboard_user("docs-leaver")
        client = granted(token)
        user.is_active = False
        user.save()
        self.assertEqual(client.get(DOCS).status_code, 302)

        _, token = dashboard_user("docs-reader")
        client = granted(token)
        self.assertEqual(client.get(DOCS).status_code, 200)
        later = time.time() + api_docs.GRANT_SECONDS + 1
        with mock.patch("operations.api_docs.time.time", return_value=later):
            self.assertEqual(client.get(DOCS).status_code, 302)
            self.assertEqual(client.get(SCHEMA).status_code, 403)

    def test_the_grant_rides_a_fresh_httponly_session_and_signs_nobody_in_to_django(self):
        _, token = dashboard_user()
        client = Client()
        session = client.session
        session["sso_pending"] = "kept"                 # whatever the session already held (e.g. a sign-in)
        session.save()
        before = session.session_key
        client.cookies[settings.SESSION_COOKIE_NAME] = before
        response = client.post(SESSION, HTTP_AUTHORIZATION=f"Token {token.key}")
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertNotEqual(cookie.value, before)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        stored = client.session
        self.assertEqual(stored["sso_pending"], "kept")
        self.assertNotIn("_auth_user_id", stored)       # not a Django sign-in
        self.assertEqual(stored[api_docs.GRANT]["token"], hashlib.sha256(token.key.encode()).hexdigest())
        self.assertEqual(client.get("/api/auth/me/").status_code, 401)      # no API accepts the session

    def test_no_token_reaches_a_response_a_url_or_a_log(self):
        _, token = dashboard_user()
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("docs-test").debug("start")
            client = Client()
            grant = client.post(SESSION, HTTP_AUTHORIZATION=f"Token {token.key}")
            Client().post(SESSION, HTTP_AUTHORIZATION="Token not-a-dashboard-token")
            served = [grant, client.get(DOCS), client.get(SCHEMA), Client().get(DOCS)]
        self.assertEqual(grant.content, b"")
        for response in served:
            text = response.content.decode() + json.dumps(dict(response.items()))
            self.assertNotIn(token.key, text)
            self.assertNotIn("not-a-dashboard-token", text)
        self.assertNotIn(token.key, "\n".join(logs.output))
        self.assertNotIn("not-a-dashboard-token", "\n".join(logs.output))


@ENABLED
class PageTests(TestCase):
    def setUp(self):
        _, token = dashboard_user()
        self.response = granted(token).get(DOCS)
        self.html = self.response.content.decode()

    def test_swagger_ui_is_one_pinned_release_with_subresource_integrity(self):
        self.assertRegex(api_docs.SWAGGER_UI_VERSION, r"^\d+\.\d+\.\d+$")
        tags = re.findall(r"<(?:script|link)\b[^>]*\b(?:src|href)=\"https?://[^>]*>", self.html)
        self.assertEqual(len(tags), 2)
        for tag in tags:
            self.assertIn(f"swagger-ui-dist@{api_docs.SWAGGER_UI_VERSION}/", tag)
            self.assertRegex(tag, r'integrity="sha384-[A-Za-z0-9+/]{64}"')
            self.assertIn('crossorigin="anonymous"', tag)

    def test_try_it_out_and_authorize_are_off_and_nothing_leaves_the_page(self):
        for setting in ("supportedSubmitMethods: []", "tryItOutEnabled: false", "persistAuthorization: false",
                        "validatorUrl: null", f'url: "{SCHEMA}"'):
            self.assertIn(setting, self.html)
        self.assertIn(".swagger-ui .auth-wrapper", self.html)
        self.assertNotIn("<form", self.html)

    def test_the_page_is_locked_down(self):
        policy = self.response["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        self.assertIn(f'<script nonce="{nonce}">', self.html)
        for directive in ("default-src 'none'", "connect-src 'self'", "form-action 'none'", "frame-ancestors 'none'",
                          "base-uri 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotRegex(policy.split("script-src")[1].split(";")[0], r"'unsafe-inline'|\*")
        self.assertIn("no-store", self.response["Cache-Control"])
        self.assertEqual(self.response["X-Frame-Options"], "DENY")

    @override_settings(PUBLISHER_LMS_SERVICE_TOKENS=["lms-secret-sentinel-" + "x" * 32],
                       SOCIAL_MEDIA_SERVICE_TOKEN="social-secret-sentinel-" + "y" * 32,
                       SECRET_KEY="django-secret-sentinel-" + "z" * 40)
    def test_no_configured_secret_reaches_the_page_or_the_document(self):
        _, token = dashboard_user("docs-user-2")
        client = granted(token)
        served = client.get(DOCS).content.decode() + client.get(SCHEMA).content.decode()
        for sentinel in ("lms-secret-sentinel", "social-secret-sentinel", "django-secret-sentinel"):
            self.assertNotIn(sentinel, served)


# =========================================================================== plain HTTP
class HttpsOnlyTests(SimpleTestCase):
    """Every docs route Django serves is covered by the web server's plain-HTTP rules."""
    DOMAIN = "positive-moments.kentbusinesscollege.net"

    def docs_routes(self):
        return sorted(r for r in all_routes() if r.startswith(("/api/docs", "/api/schema")))

    def test_no_docs_route_is_served_or_accepts_a_request_body_over_plain_http(self):
        rules = HTTPS_RULES.block(self.DOMAIN)
        self.assertEqual(self.docs_routes(), [DOCS, SESSION, SCHEMA])
        for route in self.docs_routes():
            for method in ("GET", "HEAD"):
                self.assertEqual(evaluate(rules, port=80, method=method, uri=route),
                                 ("301", f"https://{self.DOMAIN}{route}"), (method, route))
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertEqual(evaluate(rules, port=80, method=method, uri=route), ("403", None), (method, route))
            for method in ("GET", "POST"):
                self.assertEqual(evaluate(rules, port=443, method=method, uri=route), ("pass", None), (method, route))
        self.assertEqual(evaluate(rules, port=80, method="GET", uri=ENTRY), ("301", f"https://{self.DOMAIN}{ENTRY}"))

    def test_the_lms_api_rules_are_unchanged(self):
        rules = HTTPS_RULES.block(self.DOMAIN)
        for route in (r for r in all_routes() if r.startswith(LMS_BASE + "/")):
            self.assertEqual(evaluate(rules, port=80, method="GET", uri=route), ("403", None), route)
            self.assertEqual(evaluate(rules, port=443, method="GET", uri=route), ("pass", None), route)


# =========================================================================== the document
class DocumentTests(SimpleTestCase):
    def test_a_valid_openapi_3_document_whose_examples_match_its_schemas(self):
        self.assertRegex(SPEC["openapi"], r"^3\.0\.\d+$")
        self.assertEqual((SPEC["info"]["version"], len(SPEC["servers"])), ("1", 1))
        for ref in references(SPEC):
            resolve(SPEC, {"$ref": ref})                            # KeyError = a dangling reference
        operation_ids = []
        for path, item in SPEC["paths"].items():
            self.assertEqual(set(item), {"get"}, path)
            operation = item["get"]
            operation_ids.append(operation["operationId"])
            declared = {resolve(SPEC, p)["name"] for p in operation.get("parameters", [])
                        if resolve(SPEC, p)["in"] == "path"}
            self.assertEqual(declared, set(re.findall(r"{(\w+)}", path)), path)
            for code, response in operation["responses"].items():
                response = resolve(SPEC, response)
                self.assertTrue(response["description"], (path, code))
                self.assertIn("X-Publisher-Contract", response["headers"], (path, code))
                for media in response.get("content", {}).values():
                    examples = [media["example"]] if "example" in media else []
                    examples += [resolve(SPEC, e)["value"] for e in media.get("examples", {}).values()]
                    for example in examples:
                        self.assertEqual(problems(SPEC, example, media["schema"]), [], (path, code))
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        for name, node in SPEC["components"]["schemas"].items():
            self.assertLessEqual(set(node.get("required", [])), set(node.get("properties", {})), name)
            for prop, value in node.get("properties", {}).items():
                if value.get("nullable") and "enum" in value:
                    self.assertIn(None, value["enum"], f"{name}.{prop}")

    def test_the_checker_reports_an_undocumented_or_missing_property(self):
        example = resolve(SPEC, SPEC["components"]["examples"]["ReleasedRecording"])["value"]
        self.assertEqual(problems(SPEC, {**example, "extra": 1}, schema("OccurrenceRecording")), ["$: undocumented extra"])
        self.assertEqual(problems(SPEC, {k: v for k, v in example.items() if k != "playback"},
                                  schema("OccurrenceRecording")), ["$: missing playback"])

    def test_bearer_service_token_on_every_route(self):
        self.assertEqual(SPEC["security"], [{"lmsServiceToken": []}])
        scheme = SPEC["components"]["securitySchemes"]["lmsServiceToken"]
        self.assertEqual((scheme["type"], scheme["scheme"]), ("http", "bearer"))
        self.assertTrue(all("security" not in item["get"] for item in SPEC["paths"].values()))

    def test_exactly_the_three_lms_routes(self):
        served = {route for route in all_routes() if route.startswith(LMS_BASE + "/")}
        self.assertEqual(documented_routes(), served)
        self.assertEqual(documented_routes(), {LMS_BASE + "/health", LMS_BASE + "/occurrences",
                                               LMS_BASE + "/occurrences/{occurrence_id}/recording"})

    def test_no_internal_route_is_documented(self):
        text = json.dumps(SPEC)
        internal = {route for route in all_routes() if not route.startswith(LMS_BASE + "/")}
        self.assertTrue(internal)
        self.assertFalse(internal & documented_routes())
        self.assertEqual(re.findall(r"/api/(?!publisher/)[\w-]+", text), [])
        self.assertEqual(re.findall(r"/admin\b|webhook|/operations/|/social-media/|/auth/", text, re.IGNORECASE), [])

    def test_examples_are_sanitised_and_no_secret_shaped_value_is_present(self):
        text = json.dumps(SPEC)
        for forbidden in (r"postgres(ql)?://", r"sslmode", r"BEGIN [A-Z ]*KEY", r"AccessKey", r"tempauth",
                          r"[?&](sig|token)=", r"[0-9a-fA-F]{32,}", r"@thread", r"\.mp4"):
            self.assertIsNone(re.search(forbidden, text, re.IGNORECASE), forbidden)
        for value in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I):
            self.assertTrue(value.startswith("00000000-0000-"), value)
        for value in re.findall(r"\b(?:OCC|LIVE|MOD)-[\w-]+", text):
            self.assertIn("EXAMPLE", value)
        libraries = re.findall(r'"library_id": (\d+)', text)
        self.assertTrue(libraries)
        self.assertEqual(set(libraries), {"796471"})


# =========================================================================== the documented answers are real
@override_settings(**LMS)
class ContractTests(TestCase):
    """The status codes, headers and error bodies the document promises are the ones the API sends."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def documented(self, path, code):
        operation = SPEC["paths"][path.removeprefix(LMS_BASE)]["get"]
        return resolve(SPEC, operation["responses"][str(code)])

    def assert_documented(self, route, path, response):
        self.assertEqual((response["X-Publisher-Contract"], response["Cache-Control"]), ("1", "no-store"))
        documented = self.documented(path, response.status_code)
        body = documented["content"]["application/json"]["schema"]
        self.assertEqual(problems(SPEC, response.json(), body), [], (route, response.status_code))
        for header in documented["headers"]:
            self.assertIn(header, response, (route, response.status_code, header))

    @mock.patch("operations.lms_api.reading", fake_connection)
    def test_401_405_and_429_are_the_documented_responses_on_every_route(self):
        pairs = {routes()[0]: LMS_BASE + "/occurrences/{occurrence_id}/recording",
                 routes()[1]: LMS_BASE + "/occurrences", routes()[2]: LMS_BASE + "/health"}
        for route, path in pairs.items():
            for client in (APIClient(), APIClient(HTTP_AUTHORIZATION="Bearer " + "n" * 40)):
                response = client.get(route)
                self.assertEqual(response.status_code, 401, route)
                self.assert_documented(route, path, response)
            for method in ("post", "put", "patch", "delete"):
                response = getattr(self.client, method)(route)
                self.assertEqual(response.status_code, 405, (route, method))
                self.assert_documented(route, path, response)
                self.assertEqual(getattr(APIClient(), method)(route).status_code, 401, (route, method))
        with override_settings(PUBLISHER_LMS_RATE="1/min"):
            cache.clear()
            self.client.get(routes()[2])
            throttled = self.client.get(routes()[2])
            self.assertEqual(throttled.status_code, 429)
            self.assert_documented(routes()[2], LMS_BASE + "/health", throttled)

    @mock.patch("operations.lms_api.reading", fake_connection)
    def test_success_and_error_bodies_are_the_documented_ones(self):
        recording, feed, health = routes()
        example = resolve(SPEC, SPEC["components"]["examples"]["ReleasedRecording"])["value"]
        with mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=example):
            self.assert_documented(recording, LMS_BASE + "/occurrences/{occurrence_id}/recording",
                                   self.client.get(recording))
        with mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=None):
            self.assert_documented(recording, LMS_BASE + "/occurrences/{occurrence_id}/recording",
                                   self.client.get(recording))
        self.assert_documented(feed, LMS_BASE + "/occurrences", self.client.get(feed, {"limit": "51"}))
        with override_settings(PUBLISHER_LMS_LIBRARY_ID=None):
            self.assert_documented(feed, LMS_BASE + "/occurrences", self.client.get(feed))
            self.assert_documented(health, LMS_BASE + "/health", self.client.get(health))
        self.assert_documented(health, LMS_BASE + "/health", self.client.get(health))
