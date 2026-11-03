"""
API documentation (/api/docs/, /api/schema/): off by default, Django staff only, Swagger UI pinned with
Subresource Integrity and Try It Out off, and a document that describes exactly the three LMS routes -
with their real authentication, headers and error bodies - and nothing internal or secret.

No platform database is opened: the LMS routes' reads are patched, as in test_lms_api. That the real
response bodies conform to the document is tests/integration/test_publisher_lms_openapi.py.
"""
import json
import re
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
from tests.openapi_contract import load, problems, references, resolve, schema

DOCS, SCHEMA, LOGIN, LOGOUT = "/api/docs/", "/api/schema/", "/api/docs/login/", "/api/docs/logout/"
LMS_BASE = "/api/publisher/v1"
PASSWORD = "docs-test-password-4f9Kq2"
ENABLED = override_settings(PUBLISHER_API_DOCS_ENABLED=True)
LMS = dict(PUBLISHER_LMS_SERVICE_TOKENS=[TOKEN], PUBLISHER_LMS_LIBRARY_ID=796471,
           PUBLISHER_LMS_PLAYBACK_ENABLED=True, PUBLISHER_LMS_RATE="1000/min")
SPEC = load()


def account(username, *, staff=True, active=True):
    return get_user_model().objects.create_user(username, password=PASSWORD, is_staff=staff, is_active=active)


def signed_in(user):
    client = Client()
    client.force_login(user)
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
    def test_switched_off_every_docs_route_is_not_found_even_for_staff(self):
        for client in (Client(), signed_in(account("docs-staff"))):
            for path in (DOCS, SCHEMA, LOGIN):
                self.assertEqual(client.get(path).status_code, 404, path)
            self.assertEqual(client.post(LOGIN, {"username": "docs-staff", "password": PASSWORD}).status_code, 404)
            self.assertEqual(client.post(LOGOUT).status_code, 404)


@ENABLED
class AccessTests(TestCase):
    def test_anonymous_and_non_staff_are_sent_to_the_staff_sign_in(self):
        for client in (Client(), signed_in(account("docs-learner", staff=False)),
                       signed_in(account("docs-disabled", active=False))):
            for path in (DOCS, SCHEMA):
                response = client.get(path)
                self.assertEqual(response.status_code, 302, path)
                self.assertEqual(response["Location"], f"{LOGIN}?next={path}")

    def test_staff_read_the_page_and_the_document(self):
        client = signed_in(account("docs-staff"))
        self.assertEqual(client.get(DOCS).status_code, 200)
        response = client.get(SCHEMA)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/json"))
        self.assertEqual(response.json(), SPEC)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(client.post(DOCS).status_code, 405)

    @override_settings(**LMS)
    def test_neither_the_lms_service_token_nor_a_console_token_opens_the_documentation(self):
        staff = account("docs-console-staff")
        for header in (f"Bearer {TOKEN}", f"Token {Token.objects.create(user=staff).key}"):
            for path in (DOCS, SCHEMA):
                self.assertEqual(Client(HTTP_AUTHORIZATION=header).get(path).status_code, 302, (header[:6], path))


@ENABLED
class SignInTests(TestCase):
    def test_staff_sign_in_opens_the_documentation_and_sign_out_closes_it(self):
        account("docs-staff")
        client = Client()
        page = client.get(LOGIN, {"next": DOCS})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="csrfmiddlewaretoken"')
        self.assertIn("default-src 'none'", page["Content-Security-Policy"])
        response = client.post(LOGIN, {"username": "docs-staff", "password": PASSWORD, "next": SCHEMA})
        self.assertEqual((response.status_code, response["Location"]), (302, SCHEMA))
        self.assertEqual(client.get(DOCS).status_code, 200)
        self.assertEqual(client.post(LOGOUT)["Location"], LOGIN)
        self.assertEqual(client.get(DOCS).status_code, 302)

    def test_a_correct_password_without_staff_status_or_a_wrong_password_signs_nobody_in(self):
        account("docs-learner", staff=False)
        account("docs-staff")
        for username, password in (("docs-learner", PASSWORD), ("docs-staff", "wrong-password")):
            client = Client()
            response = client.post(LOGIN, {"username": username, "password": password})
            self.assertEqual(response.status_code, 200, username)
            self.assertContains(response, "staff account")
            self.assertEqual(client.get(DOCS).status_code, 302, username)

    def test_an_https_sign_in_behind_the_proxy_needs_the_trusted_origin(self):
        """Django sees plain HTTP behind the TLS proxy, so the HTTPS form's Origin must be trusted."""
        account("docs-staff")
        for trusted, expected in (([], 403), (["https://testserver"], 302)):
            with override_settings(CSRF_TRUSTED_ORIGINS=trusted):
                client = Client(enforce_csrf_checks=True)
                client.get(LOGIN)
                response = client.post(LOGIN, {"username": "docs-staff", "password": PASSWORD,
                                               "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                                       HTTP_ORIGIN="https://testserver")
                self.assertEqual(response.status_code, expected, trusted)


@ENABLED
class PageTests(TestCase):
    def setUp(self):
        self.response = signed_in(account("docs-staff")).get(DOCS)
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

    def test_the_page_is_locked_down(self):
        policy = self.response["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        self.assertIn(f'<script nonce="{nonce}">', self.html)
        for directive in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotRegex(policy.split("script-src")[1].split(";")[0], r"'unsafe-inline'|\*")
        self.assertIn("no-store", self.response["Cache-Control"])
        self.assertEqual(self.response["X-Frame-Options"], "DENY")

    @override_settings(PUBLISHER_LMS_SERVICE_TOKENS=["lms-secret-sentinel-" + "x" * 32],
                       SOCIAL_MEDIA_SERVICE_TOKEN="social-secret-sentinel-" + "y" * 32,
                       SECRET_KEY="django-secret-sentinel-" + "z" * 40)
    def test_no_configured_secret_reaches_the_page_or_the_document(self):
        client = signed_in(account("docs-staff-2"))
        served = client.get(DOCS).content.decode() + client.get(SCHEMA).content.decode()
        for sentinel in ("lms-secret-sentinel", "social-secret-sentinel", "django-secret-sentinel"):
            self.assertNotIn(sentinel, served)


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
