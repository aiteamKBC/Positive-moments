"""
Positive Moments Media API contract.

Every platform call is patched: these tests never open a connection to any
platform database and never reach Graph, a model or Creatomate. They pin the
HTTP layer only - authentication, fast queueing, safe responses, the webhook
guard. The platform behaviour itself is covered in tests/unit and
tests/integration.
"""
import json
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.config.settings import Settings
from app.media.webhook import webhook_token

JOB = "11111111-2222-3333-4444-555555555555"
LECTURE = "22222222-3333-4444-5555-666666666666"
MOMENT = "33333333-4444-5555-6666-777777777777"
ASSET = "44444444-5555-6666-7777-888888888888"
RUN = {"run_id": JOB, "mode": "ANALYZE", "status": "PENDING",
       "requested_from": "2026-09-01", "requested_to": "2026-09-30"}


def config(**overrides):
    values = dict(database_url="postgresql://never", aptem_database_url="", graph_tenant_id="",
                  graph_client_id="", graph_client_secret="", graph_scope="",
                  graph_base_url="", calendar_user_upn="",
                  creatomate_api_key="ck_live_SECRET", media_webhook_secret="hook-secret")
    values.update(overrides)
    return Settings(**values)


@contextmanager
def fake_connection():
    yield mock.MagicMock()


def patched(test):
    """No real connection, ever."""
    test = mock.patch("operations.media_views.reading", fake_connection)(test)
    test = mock.patch("operations.media_views.writing", fake_connection)(test)
    return mock.patch("operations.media_views.settings", lambda: config())(test)


class _Authenticated(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-test")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")


class AuthenticationTests(TestCase):
    ENDPOINTS = (
        ("get", "operations-media-dashboard", {}),
        ("post", "operations-media-run-create", {}),
        ("get", "operations-media-run", {"run_id": JOB}),
        ("post", "operations-media-moment-render", {"moment_id": MOMENT}),
        ("post", "operations-media-moment-retry", {"moment_id": MOMENT}),
        ("get", "operations-media-asset-open", {"asset_id": ASSET}),
        ("get", "operations-lecture-positive-moments", {"lecture_id": LECTURE}),
        ("get", "operations-lecture-recording-open", {"lecture_id": LECTURE}),
    )

    def test_every_media_endpoint_except_the_webhook_requires_authentication(self):
        client = APIClient()
        for method, name, kwargs in self.ENDPOINTS:
            with self.subTest(endpoint=name):
                response = getattr(client, method)(reverse(name, kwargs=kwargs))
                self.assertIn(response.status_code, (401, 403))


class RunQueueTests(_Authenticated):

    @patched
    def test_start_analysis_queues_one_run_and_returns_immediately(self):
        with mock.patch("operations.media_views.MediaPlatformRepository") as repo:
            repo.return_value.create_run.return_value = RUN
            response = self.client.post(reverse("operations-media-run-create"),
                                        {"mode": "analyze", "from": "2026-09-01",
                                         "to": "2026-09-30"}, format="json")
        self.assertEqual(response.status_code, 202)
        kwargs = repo.return_value.create_run.call_args.kwargs
        self.assertEqual((kwargs["mode"], str(kwargs["requested_from"])),
                         ("ANALYZE", "2026-09-01"))

    @patched
    def test_bad_modes_and_ranges_are_the_operators_mistake(self):
        with mock.patch("operations.media_views.MediaPlatformRepository") as repo:
            for body in ({"mode": "DELETE_EVERYTHING", "from": "2026-09-01", "to": "2026-09-02"},
                         {"mode": "RENDER", "from": "2026-09-30", "to": "2026-09-01"},
                         {"mode": "RENDER", "from": "2026-01-01", "to": "2026-09-30"},
                         {"mode": "RENDER", "lecture_id": "not-a-uuid"}):
                with self.subTest(body=body):
                    response = self.client.post(reverse("operations-media-run-create"), body,
                                                format="json")
                    self.assertEqual(response.status_code, 400)
            repo.return_value.create_run.assert_not_called()

    @patched
    def test_a_moment_render_is_a_state_change_not_a_render(self):
        with mock.patch("operations.media_views._jobs_service") as jobs:
            jobs.return_value.queue_render.return_value = 1
            response = self.client.post(reverse("operations-media-moment-render",
                                                kwargs={"moment_id": MOMENT}))
            self.assertEqual(response.status_code, 202)
            jobs.return_value.queue_render.return_value = 0
            again = self.client.post(reverse("operations-media-moment-render",
                                             kwargs={"moment_id": MOMENT}))
        self.assertEqual(again.status_code, 409)


class LinkTests(_Authenticated):

    @patched
    def test_open_recording_returns_only_the_durable_link(self):
        link = "https://kbc.sharepoint.com/:v:/s/cohort/org-link"
        with mock.patch("operations.media_views.access.recording_link",
                        return_value=(link, "OK")):
            response = self.client.get(reverse("operations-lecture-recording-open",
                                               kwargs={"lecture_id": LECTURE}))
        self.assertEqual(response.json(), {"url": link})

    @patched
    def test_an_unresolved_recording_has_no_link(self):
        with mock.patch("operations.media_views.access.recording_link",
                        return_value=(None, "RECORDING_LINK_NOT_EXACT")):
            response = self.client.get(reverse("operations-lecture-recording-open",
                                               kwargs={"lecture_id": LECTURE}))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("url", response.json())

    @patched
    def test_the_dashboard_never_carries_a_secret(self):
        board = {"counters": [], "lectures": [], "cost_preview": {}, "active_runs": [],
                 "recent_runs": []}
        with mock.patch("operations.media_views.dashboard", return_value=board):
            response = self.client.get(reverse("operations-media-dashboard"),
                                       {"date_from": "2026-09-01", "date_to": "2026-09-30"})
        self.assertEqual(response.status_code, 200)
        text = json.dumps(response.json())
        self.assertNotIn("ck_live_SECRET", text)
        self.assertNotIn("hook-secret", text)
        self.assertTrue(response.json()["readiness"]["render_provider_configured"])


class WebhookTests(TestCase):

    def url(self, token):
        return (reverse("operations-media-creatomate-webhook")
                + f"?job={JOB}&token={token}")

    @patched
    def test_a_forged_token_is_refused_before_any_database_access(self):
        with mock.patch("operations.media_views.writing") as writing:
            response = APIClient().post(self.url("forged"), {"id": "r-1", "status": "succeeded"},
                                        format="json")
        self.assertEqual(response.status_code, 403)
        writing.assert_not_called()

    @patched
    def test_a_valid_token_is_verified_server_side(self):
        with mock.patch("app.media.webhook.handle_creatomate_webhook",
                        return_value={"result": "UPLOAD_PENDING"}) as handle, \
                mock.patch("app.media.factory.build_provider") as provider:
            response = APIClient().post(self.url(webhook_token("hook-secret", JOB)),
                                        {"id": "r-1", "status": "succeeded"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "UPLOAD_PENDING"})
        self.assertIs(handle.call_args.kwargs["provider"], provider.return_value)
