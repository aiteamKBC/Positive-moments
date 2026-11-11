"""
LMS delivery API contract (/api/publisher/v1/): service authentication, rate limiting, access logging,
parameter validation and error mapping, plus the staff WITHDRAW route.

Every platform call is patched: these tests open no connection to any platform database and reach
neither Graph nor Bunny. The delivery behaviour itself is covered in tests/integration.
"""
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.common.errors import PlatformError
from app.publisher.operations import OperatorActionRefused

TOKEN = "lms-service-token-" + "a" * 32
ROTATED = "lms-service-token-" + "b" * 32
OCCURRENCE = "OCC-4493AC65DF5041459BC5A14DA8B357A0"
LECTURE = "0fe6c2fa-ae09-571c-b9d9-0416bed8295c"
CONFIG = dict(PUBLISHER_LMS_SERVICE_TOKENS=[TOKEN, ROTATED, "too-short"], PUBLISHER_LMS_LIBRARY_ID=786045,
              PUBLISHER_LMS_PLAYBACK_ENABLED=False, PUBLISHER_LMS_RATE="1000/min")
ANSWER = {"contract_version": "1", "lms_occurrence_id": OCCURRENCE, "state": "READY",
          "playback": {"available": False, "reason": "NOT_RELEASED"}}


@contextmanager
def fake_connection():
    yield mock.MagicMock()


def platform(test):
    test = mock.patch("operations.lms_api.reading", fake_connection)(test)
    return mock.patch("operations.publisher_views.writing", fake_connection)(test)


def routes():
    return (reverse("publisher-lms-occurrence-recording", kwargs={"occurrence_id": OCCURRENCE}),
            reverse("publisher-lms-occurrence-feed"), reverse("publisher-lms-health"))


@override_settings(**CONFIG)
class ServiceAuthenticationTests(TestCase):
    def setUp(self):
        cache.clear()

    @platform
    def test_every_route_needs_the_bearer_service_token(self):
        staff = get_user_model().objects.create(username="ops-staff")
        staff_token = Token.objects.create(user=staff).key
        for route in routes():
            for header in (None, f"Token {staff_token}", f"Bearer {staff_token}", "Bearer ", f"Basic {TOKEN}",
                           "Bearer too-short", f"Bearer {TOKEN}x"):
                client = APIClient()
                if header:
                    client.credentials(HTTP_AUTHORIZATION=header)
                response = client.get(route)
                self.assertEqual(response.status_code, 401, (route, header))
                self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="publisher-lms"')
                self.assertNotIn("lecture", response.json())

    @platform
    def test_no_configured_token_means_nobody(self):
        with override_settings(PUBLISHER_LMS_SERVICE_TOKENS=[]):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
            self.assertEqual(client.get(routes()[0]).status_code, 401)

    @platform
    def test_both_tokens_of_a_rotation_are_accepted(self):
        for token in (TOKEN, ROTATED):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            with mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=dict(ANSWER)):
                self.assertEqual(client.get(routes()[0]).status_code, 200)


@override_settings(**CONFIG)
class DeliveryApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    @platform
    def test_recording_is_looked_up_by_lms_occurrence_with_the_configured_library(self):
        with mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=dict(ANSWER)) as found, \
                self.assertLogs("publisher.lms_api", "INFO") as logs:
            response = self.client.get(routes()[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ANSWER)
        self.assertEqual(found.call_args.args[1], OCCURRENCE)
        self.assertEqual(found.call_args.kwargs["library_id"], 786045)
        self.assertIs(found.call_args.kwargs["playback_enabled"], False)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Publisher-Contract"], "1")
        line = "\n".join(logs.output)
        self.assertIn('"status": 200', line)
        self.assertIn(OCCURRENCE, line)
        self.assertNotIn(TOKEN, line)
        self.assertIn('"client": "', line)

    @platform
    def test_playback_switch_is_passed_through(self):
        with override_settings(PUBLISHER_LMS_PLAYBACK_ENABLED=True), \
                mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=dict(ANSWER)) as found:
            self.client.get(routes()[0])
        self.assertIs(found.call_args.kwargs["playback_enabled"], True)

    @platform
    def test_unknown_malformed_unconfigured_and_unavailable(self):
        with mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=None):
            response = self.client.get(routes()[0])
        self.assertEqual((response.status_code, response.json()["code"]), (404, "OCCURRENCE_NOT_FOUND"))
        with mock.patch("operations.lms_api.delivery.occurrence_recording",
                        side_effect=OperatorActionRefused("INVALID_OCCURRENCE_ID", "malformed")):
            self.assertEqual(self.client.get(routes()[0]).status_code, 400)
        with override_settings(PUBLISHER_LMS_LIBRARY_ID=None):
            response = self.client.get(routes()[0])
        self.assertEqual((response.status_code, response.json()["code"]), (503, "NOT_CONFIGURED"))
        with mock.patch("operations.lms_api.delivery.occurrence_recording",
                        side_effect=PlatformError("DATABASE_ERROR", "host db.internal password=hunter2")):
            response = self.client.get(routes()[0])
        self.assertEqual((response.status_code, response.json()["code"]), (503, "PLATFORM_UNAVAILABLE"))
        self.assertNotIn("hunter2", response.content.decode())

    @platform
    def test_feed_validates_and_forwards_paging(self):
        page = {"contract_version": "1", "items": [], "next_cursor": None, "has_more": False}
        with mock.patch("operations.lms_api.delivery.occurrence_feed", return_value=page) as feed:
            ok = self.client.get(routes()[1], {"changed_since": "2026-10-24T08:00:00Z", "cursor": "abc", "limit": "50"})
            for bad in ({"changed_since": "2026-10-24T08:00:00"}, {"changed_since": "yesterday"}, {"limit": "0"},
                        {"limit": "51"}, {"limit": "ten"}):
                self.assertEqual(self.client.get(routes()[1], bad).status_code, 400, bad)
        self.assertEqual(ok.status_code, 200)
        kwargs = feed.call_args.kwargs
        self.assertEqual((kwargs["cursor"], kwargs["limit"], kwargs["library_id"]), ("abc", 50, 786045))
        self.assertEqual(kwargs["changed_since"].isoformat(), "2026-10-24T08:00:00+00:00")
        self.assertEqual(feed.call_count, 1)

    @platform
    def test_rate_limit_per_service_token(self):
        with override_settings(PUBLISHER_LMS_RATE="2/min"), \
                mock.patch("operations.lms_api.delivery.occurrence_recording", return_value=dict(ANSWER)):
            codes = [self.client.get(routes()[0]).status_code for _ in range(3)]
            other = APIClient()
            other.credentials(HTTP_AUTHORIZATION=f"Bearer {ROTATED}")
            self.assertEqual(other.get(routes()[0]).status_code, 200)
        self.assertEqual(codes, [200, 200, 429])

    @platform
    def test_health_reports_configuration_without_secrets(self):
        response = self.client.get(routes()[2])
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((body["status"], body["library_id"], body["playback_enabled"]), ("ok", 786045, False))
        self.assertNotIn(TOKEN, response.content.decode())


class StaffWithdrawTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-withdraw")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")

    def test_withdraw_needs_staff_login(self):
        url = reverse("operations-publisher-lecture-withdraw", kwargs={"lecture_id": LECTURE})
        self.assertEqual(APIClient().post(url, {"reason": "x"}, format="json").status_code, 401)
        service = APIClient()
        service.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(service.post(url, {"reason": "x"}, format="json").status_code, 401)

    @override_settings(PUBLISHER_LMS_LIBRARY_ID=786045)
    @platform
    def test_withdraw_is_audited_with_the_authenticated_actor(self):
        result = {"lecture_id": LECTURE, "action": "WITHDRAW", "changed": True, "release": "WITHDRAWN"}
        with mock.patch("operations.publisher_views.delivery.withdraw", return_value=result) as withdraw:
            response = self.client.post(reverse("operations-publisher-lecture-withdraw", kwargs={"lecture_id": LECTURE}),
                                        {"reason": "complaint received", "actor": "someone-else"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(withdraw.call_args.kwargs["actor"], "ops-withdraw")
        self.assertEqual(withdraw.call_args.kwargs["library_id"], 786045)
        self.assertEqual(withdraw.call_args.kwargs["reason"], "complaint received")

    @override_settings(PUBLISHER_LMS_LIBRARY_ID=786045)
    @platform
    def test_withdraw_refusals_are_conflicts(self):
        with mock.patch("operations.publisher_views.delivery.withdraw",
                        side_effect=OperatorActionRefused("ACTOR_AND_REASON_REQUIRED", "reason needed")):
            response = self.client.post(reverse("operations-publisher-lecture-withdraw", kwargs={"lecture_id": LECTURE}),
                                        {}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "ACTOR_AND_REASON_REQUIRED"))
