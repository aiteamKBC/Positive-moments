"""
Continuous Bunny Publisher operations API contract.

Every platform call is patched: these tests never open a connection to any
platform database and never reach Graph or Bunny. They pin the HTTP layer -
authentication, read-only routing, audited operator actions with the
authenticated actor, safe refusals. The behaviour itself is covered in
tests/unit and tests/integration.
"""
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.publisher.operations import OperatorActionRefused

LECTURE = "22222222-3333-4444-5555-666666666666"
JOB = "11111111-2222-3333-4444-555555555555"
CALLS = []


@contextmanager
def fake_connection():
    connection = mock.MagicMock()
    CALLS.append(connection)
    yield connection


def patched(test):
    test = mock.patch("operations.publisher_views.reading", fake_connection)(test)
    test = mock.patch("operations.publisher_views.writing", fake_connection)(test)
    return mock.patch("operations.publisher_views._library", lambda request: 786045)(test)


ENDPOINTS = (
    ("get", "operations-publisher-status", {}),
    ("get", "operations-publisher-pending", {}),
    ("get", "operations-publisher-exceptions", {}),
    ("get", "operations-publisher-lecture", {"lecture_id": LECTURE}),
    ("get", "operations-publisher-job-history", {"job_id": JOB}),
    ("post", "operations-publisher-job-retry", {"job_id": JOB}),
    ("post", "operations-publisher-part-decision", {"lecture_id": LECTURE, "part_index": 1}),
)


class AuthenticationTests(TestCase):
    def test_every_publisher_route_requires_authentication(self):
        anonymous = APIClient()
        for method, name, kwargs in ENDPOINTS:
            response = getattr(anonymous, method)(reverse(name, kwargs=kwargs))
            self.assertEqual(response.status_code, 401, name)


class PublisherApiTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-publisher")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        CALLS.clear()

    @patched
    def test_status_is_read_only_and_passes_the_library(self):
        with mock.patch("operations.publisher_views.operations.status_summary",
                        return_value={"jobs": {}, "workers_healthy": True}) as summary:
            response = self.client.get(reverse("operations-publisher-status"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workers_healthy"], True)
        self.assertEqual(summary.call_args.kwargs["library_id"], 786045)

    @patched
    def test_retry_is_audited_with_the_authenticated_actor(self):
        with mock.patch("operations.publisher_views.operations.operator_retry",
                        return_value={"job_id": JOB, "status": "QUEUED"}) as retry:
            response = self.client.post(reverse("operations-publisher-job-retry", kwargs={"job_id": JOB}),
                                        {"reason": "Bunny delete fixed", "actor": "someone-else"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(retry.call_args.kwargs["actor"], "ops-publisher")       # never taken from the body
        self.assertEqual(retry.call_args.kwargs["reason"], "Bunny delete fixed")

    @patched
    def test_refusals_are_conflicts_not_server_errors(self):
        with mock.patch("operations.publisher_views.operations.operator_retry",
                        side_effect=OperatorActionRefused("JOB_NOT_RETRYABLE", "job is RUNNING")):
            response = self.client.post(reverse("operations-publisher-job-retry", kwargs={"job_id": JOB}),
                                        {"reason": "x"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "JOB_NOT_RETRYABLE")
        with mock.patch("operations.publisher_views.operations.lecture_detail",
                        side_effect=OperatorActionRefused("LECTURE_NOT_FOUND", "no such lecture")):
            response = self.client.get(reverse("operations-publisher-lecture", kwargs={"lecture_id": LECTURE}))
        self.assertEqual(response.status_code, 404)

    @patched
    def test_part_decision_passes_decision_reason_and_actor(self):
        with mock.patch("operations.publisher_views.operations.decide_part",
                        return_value={"lecture_id": LECTURE, "part": 1, "decision": "EXCLUDE"}) as decide:
            response = self.client.post(
                reverse("operations-publisher-part-decision", kwargs={"lecture_id": LECTURE, "part_index": 1}),
                {"decision": "EXCLUDE", "reason": "false start"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(decide.call_args.kwargs, {"decision": "EXCLUDE", "actor": "ops-publisher",
                                                   "reason": "false start"})

    @patched
    def test_exceptions_return_the_queue_and_open_alerts(self):
        with mock.patch("operations.publisher_views.operations.exceptions", return_value=[{"source": "JOB"}]), \
                mock.patch("operations.publisher_views.operations.open_alerts", return_value=[{"kind": "X"}]):
            response = self.client.get(reverse("operations-publisher-exceptions"))
        self.assertEqual(response.json(), {"exceptions": [{"source": "JOB"}], "alerts": [{"kind": "X"}]})
