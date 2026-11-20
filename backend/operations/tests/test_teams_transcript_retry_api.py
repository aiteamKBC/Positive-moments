"""
Retry Teams transcript: the HTTP contract.

Every platform call is patched - these tests never open a platform connection and never reach
Graph or a model. They pin authentication on both routes, the status on the READ-ONLY connection,
queueing on the writing connection with a commit (and a rollback when nothing is queued), the
signed-in user recorded as the requester, and every refusal mapped to a status code rather than a
500. The behaviour itself is in tests/unit/test_teams_transcript_retry.py and
tests/integration/test_teams_transcript_retry.py.
"""
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.orchestration.locks import SchedulerCycleBusy
from operations import transcript_retry_views as views

LECTURE = "94008498-1fa8-51a2-a5c6-c9cd4077e8ab"
STATUS = "operations-teams-transcript-retry"
QUEUE = "operations-teams-transcript-retry-queue"
OPENED = []
JOB = {"job_id": "J1", "lecture_id": LECTURE, "reason": "TEAMS_TRANSCRIPT_RETRY", "status": "QUEUED"}


class _Base(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops.operator")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        OPENED.clear()
        self.service = mock.MagicMock()
        for target, value in (("_service", lambda: self.service),
                              ("reading", self._opener("reading")),
                              ("writing", self._opener("writing"))):
            patcher = mock.patch.object(views, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _opener(kind):
        @contextmanager
        def opened():
            handle = mock.MagicMock(name=kind)
            OPENED.append((kind, handle))
            yield handle
        return opened

    def url(self, name):
        return reverse(name, kwargs={"lecture_id": LECTURE})


class AuthenticationTests(TestCase):
    def test_both_routes_require_authentication(self):
        anonymous = APIClient()
        with mock.patch.object(views, "_service") as service:
            self.assertIn(anonymous.get(reverse(STATUS, kwargs={"lecture_id": LECTURE})).status_code, (401, 403))
            self.assertIn(anonymous.post(reverse(QUEUE, kwargs={"lecture_id": LECTURE})).status_code, (401, 403))
            service.assert_not_called()

    def test_the_lecture_is_in_the_url_and_must_be_a_uuid(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse(QUEUE, kwargs={"lecture_id": "not-a-uuid"})


class StatusTests(_Base):
    def test_status_is_read_on_the_read_only_connection(self):
        self.service.status.return_value = {"lecture_id": LECTURE, "plan": {"available": True}, "job": None}
        response = self.client.get(self.url(STATUS))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plan"], {"available": True})
        self.assertEqual([kind for kind, _ in OPENED], ["reading"])
        self.assertEqual(str(self.service.status.call_args.args[1]), LECTURE)
        self.service.queue.assert_not_called()

    def test_the_status_route_queues_nothing_and_the_queue_route_is_post_only(self):
        self.assertEqual(self.client.post(self.url(STATUS)).status_code, 405)
        self.assertEqual(self.client.get(self.url(QUEUE)).status_code, 405)


class QueueTests(_Base):
    def test_a_retry_is_queued_on_the_writing_connection_with_the_operator_and_committed(self):
        self.service.queue.return_value = {"queued": True, "created": True, "reason": None,
                                           "reason_message": None, "job": JOB}
        response = self.client.post(self.url(QUEUE), {}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job"]["job_id"], "J1")
        ((kind, handle),) = OPENED
        self.assertEqual(kind, "writing")
        self.assertIs(self.service.queue.call_args.args[0], handle)
        self.assertEqual(self.service.queue.call_args.kwargs["requested_by"], "ops.operator")
        handle.commit.assert_called_once()

    def test_a_second_click_returns_the_same_job_with_202(self):
        self.service.queue.return_value = {"queued": True, "created": False, "reason": "ALREADY_QUEUED",
                                           "reason_message": "already", "job": JOB}
        response = self.client.post(self.url(QUEUE), {}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["created"])

    def test_a_refusal_is_409_with_the_reason_and_writes_nothing(self):
        self.service.queue.return_value = {"queued": False, "created": False, "reason": "QA_ALREADY_COMPLETE",
                                           "reason_message": "This lecture's quality evaluation is complete.",
                                           "job": None}
        response = self.client.post(self.url(QUEUE), {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "QA_ALREADY_COMPLETE")
        self.assertIn("complete", response.json()["detail"])
        ((_, handle),) = OPENED
        handle.rollback.assert_called_once()
        handle.commit.assert_not_called()

    def test_a_busy_cycle_is_a_409_not_a_500(self):
        self.service.queue.side_effect = SchedulerCycleBusy("busy")
        response = self.client.post(self.url(QUEUE), {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SCHEDULER_CYCLE_RUNNING")
