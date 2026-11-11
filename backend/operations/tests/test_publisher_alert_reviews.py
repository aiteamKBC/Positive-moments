"""
Read / dismiss state of publisher alerts: per signed-in user, never the publisher's own alert.

The publisher database is patched (monitor.open_alerts / resolved_alerts / alert_ref): these tests never
open a platform connection. The review rows live in the Django test database. They pin: authentication,
one user's state never leaking to another, idempotency and concurrency, that a repeated sighting of the
same open alert keeps its state while a resolved-and-recurred alert (a new alert row) is NEW again, that
the digest is informational, and that no write ever reaches the publisher.
"""
import threading
from contextlib import contextmanager
from unittest import mock

import psycopg
from django.contrib.auth import get_user_model
from django.db import connection as django_connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from operations import publishing_monitor_views as views
from positive_mentions.models import PublisherAlertReview

from .test_publishing_monitor_api import NOW, SERVICE_TOKEN, stored_alert

WARN = "bbbbbbbb-0000-0000-0000-000000000001"
LMS_ONLY = "bbbbbbbb-0000-0000-0000-000000000002"
DIGEST = "bbbbbbbb-0000-0000-0000-000000000003"
OLD = "bbbbbbbb-0000-0000-0000-000000000004"          # a resolved alert
RECURRED = "bbbbbbbb-0000-0000-0000-000000000005"     # the same condition raised again after it resolved
MISSING = "bbbbbbbb-0000-0000-0000-0000000000ff"
EXECUTED = []


def lms_alert():
    return stored_alert(LMS_ONLY, kind="LMS_ONLY_SESSION", detail={"lms_occurrence_id": "OCC-1"}, lms={
        "occurrence_id": "OCC-1", "series_id": "LIVE-1", "programme": "Programme", "coverage_status":
        "LMS_SESSION_NOT_DISCOVERED", "scheduled_start": NOW.isoformat(), "source": None})


class Publisher:
    """The publisher's alerts, as the monitor would read them; mutable between requests."""

    def __init__(self):
        self.open = [stored_alert(WARN), lms_alert(), stored_alert(DIGEST, severity="DIGEST", kind="DAILY_DIGEST")]
        self.resolved = [stored_alert(OLD, status="RESOLVED", detail={"bunny_video_id": "v-1"})]

    def ref(self, connection, alert_id):
        for alert in self.open + self.resolved:
            if alert["alert_id"] == alert_id:
                return {"alert_id": alert_id, "severity": alert["severity"], "status": alert["status"]}
        return None


@contextmanager
def recording_reading():
    connection = mock.MagicMock()
    connection.execute.side_effect = lambda sql, *a: EXECUTED.append(sql) or mock.MagicMock()
    yield connection


def with_publisher(test):
    def run(self, *args):
        self.publisher = Publisher()
        EXECUTED.clear()
        views._lms_cache.clear()
        with mock.patch("operations.publishing_monitor_views.reading", recording_reading), \
                mock.patch("operations.publishing_monitor_views.monitor.open_alerts",
                           lambda c, **k: [dict(a) for a in self.publisher.open]), \
                mock.patch("operations.publishing_monitor_views.monitor.resolved_alerts",
                           lambda c, **k: [dict(a) for a in self.publisher.resolved]), \
                mock.patch("operations.publishing_monitor_views.monitor.alert_ref", self.publisher.ref), \
                mock.patch("operations.publishing_monitor_views.platform.settings",
                           return_value=mock.MagicMock(lms_database_url="")), \
                override_settings(PUBLISHER_LMS_LIBRARY_ID=786045, PUBLISHER_LMS_PLAYBACK_ENABLED=True,
                                  PUBLISHER_LMS_SERVICE_TOKENS=[SERVICE_TOKEN]):
            return test(self, *args)
    run.__name__ = test.__name__
    return run


def client_for(username):
    user = get_user_model().objects.create(username=username)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
    return user, client


def route(action, alert_id):
    return reverse(f"operations-publishing-monitor-alert-{action}", kwargs={"alert_id": alert_id})


class ReviewTests(TestCase):
    def setUp(self):
        self.alice, self.client = client_for("alice")
        self.bob, self.other = client_for("bob")

    def alerts(self, client=None):
        response = (client or self.client).get(reverse("operations-publishing-monitor-alerts"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        return body, {a["alert_id"]: a["review"]["state"] for a in body["alerts"] + body["history"]}

    # -- authentication ---------------------------------------------------------------------------
    @with_publisher
    def test_every_review_route_needs_a_signed_in_user_not_the_lms_service_token(self):
        service = APIClient()
        service.credentials(HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}")
        for action in ("read", "dismiss", "restore"):
            self.assertEqual(APIClient().post(route(action, WARN)).status_code, 401, action)
            self.assertEqual(service.post(route(action, WARN)).status_code, 401, action)
        self.assertEqual(self.client.get(route("read", WARN)).status_code, 405)     # POST only
        self.assertEqual(PublisherAlertReview.objects.count(), 0)

    # -- new / read ----------------------------------------------------------------------------
    @with_publisher
    def test_actionable_alerts_start_new_and_the_digest_is_informational(self):
        body, state = self.alerts()
        self.assertEqual(state, {WARN: "NEW", LMS_ONLY: "NEW", DIGEST: "INFO", OLD: "RESOLVED"})
        self.assertEqual(body["review_counts"], {"new": 2, "new_by_severity": {"WARNING": 2}, "read": 0,
                                                 "dismissed": 0, "resolved": 1})
        self.assertEqual(body["counts"], {"WARNING": 2, "DIGEST": 1})              # the stored alerts, unchanged
        self.assertEqual([r["code"] for r in body["dismiss_reasons"]],
                         ["TEST_LMS_SESSION", "NO_LMS_SESSION_INTENDED", "KNOWN_TEST_UPLOAD", "OTHER"])

    @with_publisher
    def test_reading_is_per_user_and_does_not_resolve_the_alert(self):
        response = self.client.post(route("read", WARN))
        self.assertEqual((response.status_code, response.json()["review"]["state"]), (200, "READ"))
        body, state = self.alerts()
        self.assertEqual(state[WARN], "READ")
        self.assertEqual(body["review_counts"]["new"], 1)
        self.assertIn(WARN, [a["alert_id"] for a in body["alerts"]])                 # still an open alert
        self.assertEqual(self.alerts(self.other)[1][WARN], "NEW")                   # bob has not read it

    @with_publisher
    def test_reading_twice_is_idempotent_and_keeps_the_first_read_time(self):
        first = self.client.post(route("read", WARN)).json()["review"]["read_at"]
        second = self.client.post(route("read", WARN)).json()["review"]["read_at"]
        self.assertEqual(first, second)
        self.assertEqual(PublisherAlertReview.objects.filter(user=self.alice).count(), 1)

    @with_publisher
    def test_a_repeated_sighting_of_the_same_open_alert_does_not_make_it_new_again(self):
        self.client.post(route("read", WARN))
        self.client.post(route("dismiss", LMS_ONLY), {"reason": "TEST_LMS_SESSION"}, format="json")
        for alert in self.publisher.open:                     # the reconciler saw both again: same rows
            alert.update(occurrences=alert["occurrences"] + 5, last_seen_at="2026-10-27T02:00:00+00:00")
        body, state = self.alerts()
        self.assertEqual((state[WARN], state[LMS_ONLY]), ("READ", "DISMISSED"))
        self.assertEqual(body["review_counts"]["new"], 0)

    @with_publisher
    def test_a_resolved_alert_that_recurs_is_new_again(self):
        self.client.post(route("read", WARN))
        # The reconciler resolved WARN, then saw the condition again: a NEW alert row with a new id.
        warn = self.publisher.open.pop(0)
        self.publisher.resolved.insert(0, {**warn, "status": "RESOLVED", "resolved_at": NOW.isoformat()})
        self.publisher.open.insert(0, stored_alert(RECURRED))
        body, state = self.alerts()
        self.assertEqual((state[WARN], state[RECURRED]), ("RESOLVED", "NEW"))
        self.assertEqual(body["review_counts"]["new"], 2)

    # -- dismiss / restore ------------------------------------------------------------------------
    @with_publisher
    def test_dismissing_keeps_the_alert_in_history_and_out_of_the_new_count(self):
        response = self.client.post(route("dismiss", LMS_ONLY), {"reason": "TEST_LMS_SESSION"}, format="json")
        review = response.json()["review"]
        self.assertEqual((response.status_code, review["state"], review["dismiss_reason"]),
                         (200, "DISMISSED", "TEST_LMS_SESSION"))
        self.assertIsNotNone(review["read_at"])                                     # dismissing also reads it
        body, state = self.alerts()
        self.assertEqual(state[LMS_ONLY], "DISMISSED")
        self.assertEqual(body["review_counts"], {"new": 1, "new_by_severity": {"WARNING": 1}, "read": 0,
                                                 "dismissed": 1, "resolved": 1})
        self.assertEqual(self.alerts(self.other)[1][LMS_ONLY], "NEW")              # only alice dismissed it

    @with_publisher
    def test_restore_undoes_a_dismissal_and_the_alert_stays_read(self):
        self.client.post(route("dismiss", WARN), {"reason": "KNOWN_TEST_UPLOAD", "note": "pre-pilot"}, format="json")
        review = self.client.post(route("restore", WARN)).json()["review"]
        self.assertEqual((review["state"], review["dismiss_reason"], review["dismiss_note"]), ("READ", None, None))

    @with_publisher
    def test_dismiss_validates_its_reason_and_note(self):
        cases = (({}, "INVALID_REASON"), ({"reason": "BECAUSE"}, "INVALID_REASON"),
                 ({"reason": "OTHER"}, "NOTE_REQUIRED"), ({"reason": "OTHER", "note": "  "}, "NOTE_REQUIRED"),
                 ({"reason": "OTHER", "note": "x" * 301}, "NOTE_TOO_LONG"))
        for data, code in cases:
            response = self.client.post(route("dismiss", WARN), data, format="json")
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), data)
        self.assertEqual(PublisherAlertReview.objects.count(), 0)

    @with_publisher
    def test_a_note_is_scrubbed_of_links_and_secrets(self):
        note = "see https://dash.example/v?sig=abc and password=hunter2 - old test upload"
        review = self.client.post(route("dismiss", WARN), {"reason": "OTHER", "note": note},
                                  format="json").json()["review"]
        self.assertNotIn("dash.example", review["dismiss_note"])
        self.assertNotIn("hunter2", review["dismiss_note"])
        self.assertIn("old test upload", review["dismiss_note"])
        stored = PublisherAlertReview.objects.get().dismiss_note
        self.assertNotIn("hunter2", stored)

    @with_publisher
    def test_a_resolved_alert_cannot_be_dismissed_and_the_digest_has_no_review_state(self):
        response = self.client.post(route("dismiss", OLD), {"reason": "KNOWN_TEST_UPLOAD"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "ALERT_RESOLVED"))
        for action in ("read", "dismiss"):
            response = self.client.post(route(action, DIGEST), {"reason": "OTHER", "note": "x"}, format="json")
            self.assertEqual((response.status_code, response.json()["code"]), (400, "INFORMATIONAL"))
        response = self.client.post(route("read", MISSING))
        self.assertEqual((response.status_code, response.json()["code"]), (404, "ALERT_NOT_FOUND"))
        self.assertEqual(PublisherAlertReview.objects.count(), 0)

    # -- the publisher is never written --------------------------------------------------------
    @with_publisher
    def test_reviews_never_write_to_the_publisher_database(self):
        self.client.post(route("read", WARN))
        self.client.post(route("dismiss", LMS_ONLY), {"reason": "TEST_LMS_SESSION"}, format="json")
        self.client.post(route("restore", LMS_ONLY))
        self.alerts()
        writes = [sql for sql in EXECUTED if not sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(writes, [])
        self.assertFalse(hasattr(views, "writing"))
        self.assertEqual(sorted(t for t in django_connection.introspection.table_names()
                                if "alert" in t), ["qa_publisher_alert_reviews"])

    # -- LMS session details ----------------------------------------------------------------------
    @with_publisher
    def test_the_lms_record_is_optional_and_its_absence_is_explained(self):
        lms = next(a for a in self.alerts()[0]["alerts"] if a["alert_id"] == LMS_ONLY)["lms"]
        self.assertEqual((lms["programme"], lms["source"]), ("Programme", {"status": "NOT_CONFIGURED"}))
        record = {"status": "OK", "title": "MarTech Foundations - TEST", "session_number": 3}
        with mock.patch("operations.publishing_monitor_views.platform.settings",
                        return_value=mock.MagicMock(lms_database_url="postgresql://lms")):
            with mock.patch("operations.publishing_monitor_views.monitor.lms_sessions",
                            side_effect=psycopg.OperationalError("down")):
                lms = next(a for a in self.alerts()[0]["alerts"] if a["alert_id"] == LMS_ONLY)["lms"]
            self.assertEqual(lms["source"], {"status": "UNAVAILABLE"})
            views._lms_cache.clear()
            with mock.patch("operations.publishing_monitor_views.monitor.lms_sessions",
                            return_value={"OCC-1": record}) as lookup:
                for _ in range(2):
                    lms = next(a for a in self.alerts()[0]["alerts"] if a["alert_id"] == LMS_ONLY)["lms"]
            self.assertEqual(lms["source"], record)
            self.assertEqual(lookup.call_count, 1)                                 # cached between refreshes


class ConcurrentReviewTests(TransactionTestCase):
    """Real concurrent requests on the real test database: one row per user and alert, never an error."""

    @with_publisher
    def test_concurrent_reads_and_dismissals_leave_one_consistent_row(self):
        user, _ = client_for("carol")
        token = Token.objects.get(user=user).key
        statuses, start = [], threading.Barrier(8)

        def act(index):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
            try:
                start.wait()
                if index % 2:
                    statuses.append(client.post(route("read", WARN)).status_code)
                else:
                    statuses.append(client.post(route("dismiss", WARN), {"reason": "KNOWN_TEST_UPLOAD"},
                                                format="json").status_code)
            finally:
                django_connection.close()

        threads = [threading.Thread(target=act, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(statuses, [200] * 8)
        [review] = PublisherAlertReview.objects.filter(user=user, alert_id=WARN)
        self.assertIsNotNone(review.read_at)
        self.assertEqual(review.dismiss_reason, "KNOWN_TEST_UPLOAD")
