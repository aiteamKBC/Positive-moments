"""
Lecture Publishing Monitor API contract.

Every platform call is patched: these tests never open a connection to a platform database and never
reach Graph or Bunny. They pin the HTTP layer - authentication, GET only, read-only routing, input
validation, pagination, that the summary and the table agree, and that no token leaks. The
classification itself is covered in tests/unit and tests/integration.
"""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import psycopg
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.publisher import monitor
from operations import publishing_monitor_views as views

LECTURE = "22222222-3333-4444-5555-666666666666"
NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "lms-service-token-that-must-never-reach-the-browser-0123456789"
CONNECTIONS = []


@contextmanager
def fake_reading():
    connection = mock.MagicMock()
    connection.execute.return_value.fetchone.return_value = (NOW,)
    CONNECTIONS.append(connection)
    yield connection


def fact(index, **over):
    start = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc) + timedelta(hours=index)
    base = {"lecture_id": f"00000000-0000-0000-0000-{index:012d}", "session_date": date(2026, 10, 25),
            "scheduled_start": start, "scheduled_end": start + timedelta(hours=1), "subject": f"Lecture {index}",
            "module": None, "cancelled": False, "suppressed": False, "match_status": "VERIFIED",
            "match_review": False, "occurrence_id": f"OCC-{index}", "link_current": True,
            "recording_status": "WAITING", "recording_reason": "NO_RECORDING_YET", "recording_review_reason": None,
            "expected_parts": None, "located_parts": 0, "next_check_after": None, "selection_status": None,
            "jobs": [], "videos": [], "updated_at": NOW}
    base.update(over)
    return base


FACTS = [fact(i) for i in range(1, 31)] + [fact(31, match_status="CONFLICT", match_review=True)]
A1, A2, A3 = (f"aaaaaaaa-0000-0000-0000-00000000000{n}" for n in (1, 2, 3))


def stored_alert(alert_id, *, severity="WARNING", kind="ORPHAN_BUNNY_VIDEO", status="OPEN", detail=None, **over):
    """An alert as monitor.open_alerts / resolved_alerts return it."""
    base = {"alert_id": alert_id, "severity": severity, "kind": kind, "summary": f"{kind} {alert_id[-1]}",
            "status": status, "occurrences": 1, "first_seen_at": NOW.isoformat(), "last_seen_at": NOW.isoformat(),
            "resolved_at": NOW.isoformat() if status == "RESOLVED" else None, "notification_status": "NOT_SENT",
            "lecture": None, "job_id": None, "detail": detail or {}, "lms": None, "match": None}
    base.update(over)
    return base


def patched(test):
    test = mock.patch("operations.publishing_monitor_views.reading", fake_reading)(test)
    test = mock.patch("operations.publishing_monitor_views.monitor.load_facts", lambda *a, **k: FACTS)(test)
    return override_settings(PUBLISHER_LMS_LIBRARY_ID=786045, PUBLISHER_LMS_PLAYBACK_ENABLED=True,
                             PUBLISHER_LMS_SERVICE_TOKENS=[SERVICE_TOKEN])(test)


ROUTES = (("operations-publishing-monitor-health", {}), ("operations-publishing-monitor-alerts", {}),
          ("operations-publishing-monitor-summary", {}),
          ("operations-publishing-monitor-lectures", {}),
          ("operations-publishing-monitor-lecture", {"lecture_id": LECTURE}))


class AuthenticationTests(TestCase):
    def test_every_monitor_route_requires_authentication(self):
        anonymous = APIClient()
        for name, kwargs in ROUTES:
            self.assertEqual(anonymous.get(reverse(name, kwargs=kwargs)).status_code, 401, name)

    def test_an_lms_service_token_is_not_an_operator_login(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}")
        with override_settings(PUBLISHER_LMS_SERVICE_TOKENS=[SERVICE_TOKEN]):
            for name, kwargs in ROUTES:
                self.assertEqual(client.get(reverse(name, kwargs=kwargs)).status_code, 401, name)


class MonitorApiTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-monitor")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        CONNECTIONS.clear()
        views._cache.clear()

    @patched
    def test_every_route_is_get_only(self):
        for name, kwargs in ROUTES:
            for method in ("post", "put", "patch", "delete"):
                self.assertEqual(getattr(self.client, method)(reverse(name, kwargs=kwargs)).status_code, 405)

    @patched
    def test_summary_and_table_agree_and_never_carry_a_token(self):
        params = {"date_from": "2026-10-25", "date_to": "2026-10-25"}
        summary = self.client.get(reverse("operations-publishing-monitor-summary"), params)
        self.assertEqual(summary.status_code, 200)
        body = summary.json()
        self.assertEqual(body["total"], 31)
        self.assertEqual(body["library_id"], 786045)
        for kpi in body["kpis"]:
            page = self.client.get(reverse("operations-publishing-monitor-lectures"),
                                   {**params, "kpi": kpi["key"], "page_size": 100}).json()
            self.assertEqual(page["total"], kpi["count"], kpi["key"])
        attention = self.client.get(reverse("operations-publishing-monitor-lectures"),
                                    {**params, "attention": "1"}).json()
        self.assertEqual([r["lms"]["status"] for r in attention["results"]], ["CONFLICT"])
        table = self.client.get(reverse("operations-publishing-monitor-lectures"), params)
        for response in (summary, table):
            self.assertNotIn(SERVICE_TOKEN, response.content.decode())
        self.assertEqual(table.json()["results"][0]["playback"], {"available": False, "reason": "NO_VIDEO"})

    @patched
    def test_pagination_is_on_the_server(self):
        url = reverse("operations-publishing-monitor-lectures")
        first = self.client.get(url, {"date_from": "2026-10-25", "page_size": 10}).json()
        last = self.client.get(url, {"date_from": "2026-10-25", "page_size": 10, "page": 4}).json()
        self.assertEqual((first["total"], len(first["results"]), first["range_total"]), (31, 10, 31))
        self.assertEqual(len(last["results"]), 1)
        self.assertEqual(first["results"][0]["subject"], "Lecture 1")
        capped = self.client.get(url, {"date_from": "2026-10-25", "page_size": 5000}).json()
        self.assertEqual(capped["page_size"], 100)

    @patched
    def test_bad_input_is_a_400_not_a_server_error(self):
        url = reverse("operations-publishing-monitor-lectures")
        for params, code in (({"date_from": "25-10-2026"}, "INVALID_DATE"),
                             ({"date_from": "2026-10-25", "date_to": "2026-10-24"}, "INVALID_RANGE"),
                             ({"date_from": "2026-09-01", "date_to": "2026-10-25"}, "RANGE_TOO_LONG"),
                             ({"date_from": "2026-10-25", "kpi": "everything"}, "INVALID_FILTER"),
                             ({"date_from": "2026-10-25", "state": "HAPPY"}, "INVALID_FILTER"),
                             ({"date_from": "2026-10-25", "page": "0"}, "INVALID_PAGE")):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 400, params)
            self.assertEqual(response.json()["code"], code, params)

    @patched
    def test_the_default_range_is_today_in_cairo(self):
        response = self.client.get(reverse("operations-publishing-monitor-summary"))
        today = views.cairo_today().isoformat()
        self.assertEqual((response.json()["date_from"], response.json()["date_to"]), (today, today))

    @patched
    def test_health_and_detail_are_served_from_the_monitor(self):
        with mock.patch("operations.publishing_monitor_views.monitor.health",
                        return_value={"status": "STALE"}) as health:
            response = self.client.get(reverse("operations-publishing-monitor-health"))
        self.assertEqual(response.json(), {"status": "STALE"})
        self.assertEqual(health.call_args.kwargs, {"library_id": 786045, "playback_enabled": True})
        with mock.patch("operations.publishing_monitor_views.monitor.lecture_detail", return_value=None):
            missing = self.client.get(reverse("operations-publishing-monitor-lecture", kwargs={"lecture_id": LECTURE}))
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "LECTURE_NOT_FOUND"))

    @patched
    def test_alerts_are_served_as_stored_with_counts_by_severity(self):
        stored = [stored_alert(A1, detail={"bunny_video_id": "v"}), stored_alert(A2),
                  stored_alert(A3, severity="DIGEST", kind="DAILY_DIGEST")]
        with mock.patch("operations.publishing_monitor_views.monitor.open_alerts", return_value=stored), \
                mock.patch("operations.publishing_monitor_views.monitor.resolved_alerts", return_value=[]):
            response = self.client.get(reverse("operations-publishing-monitor-alerts"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["counts"], {"WARNING": 2, "DIGEST": 1})
        self.assertEqual([a["alert_id"] for a in response.json()["alerts"]], [A1, A2, A3])
        self.assertNotIn(SERVICE_TOKEN, response.content.decode())

    @patched
    def test_no_secret_stored_in_an_alert_reaches_the_browser(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXBhcnQ"
        rows = [(A1, "WARNING", "ORPHAN_BUNNY_VIDEO", f"Graph refused Bearer {jwt} at https://graph.example/x?sig=Q1",
                 {"token": "rec-0a1b2c3d", "nested": {"api_key": "AK-1", "list": [{"password": "pw-2"}]},
                  "job_id": "j-1"}, None, 1, NOW, NOW, "NOT_SENT", None, None, "OPEN", None) + (None,) * 21]

        @contextmanager
        def stored():
            connection = mock.MagicMock()
            connection.execute.side_effect = lambda sql, *a: mock.MagicMock(
                fetchall=mock.MagicMock(return_value=rows if "a.status = 'OPEN'" in sql else []))
            yield connection

        with mock.patch("operations.publishing_monitor_views.reading", stored):
            response = self.client.get(reverse("operations-publishing-monitor-alerts"))
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        for secret in (jwt, "graph.example", "AK-1", "pw-2", "sig=Q1", SERVICE_TOKEN):
            self.assertNotIn(secret, body)
        alert = response.json()["alerts"][0]
        self.assertEqual((alert["detail"]["token"], alert["job_id"]), ("rec-0a1b2c3d", "j-1"))

    def test_the_shape_of_a_lecture_detail_is_redacted_as_a_whole(self):
        detail = {"row": {"reason": "see Bearer abc123"}, "timeline": [{"detail": {"refresh_token": "rt-9"}}],
                  "jobs": [{"subject_key": "IMPORT_VIDEO:part-1", "last_error_code": "BUNNY_FETCH_FAILED"}]}
        text = json.dumps(monitor.redact(detail))
        self.assertNotIn("rt-9", text)
        self.assertNotIn("abc123", text)
        self.assertIn("IMPORT_VIDEO:part-1", text)                 # operational fields stay readable
        self.assertIn("BUNNY_FETCH_FAILED", text)

    @patched
    def test_an_unavailable_database_is_reported_not_hidden(self):
        with mock.patch("operations.publishing_monitor_views.monitor.load_facts",
                        side_effect=psycopg.OperationalError("down")):
            response = self.client.get(reverse("operations-publishing-monitor-summary"))
        self.assertEqual((response.status_code, response.json()["code"]), (503, "PLATFORM_UNAVAILABLE"))

    def test_without_a_delivery_library_the_monitor_says_so(self):
        with override_settings(PUBLISHER_LMS_LIBRARY_ID=None):
            response = self.client.get(reverse("operations-publishing-monitor-health"))
        self.assertEqual((response.status_code, response.json()["code"]), (503, "NOT_CONFIGURED"))

    def test_the_monitor_module_has_no_write_path(self):
        self.assertFalse(hasattr(views, "writing"))
        self.assertEqual(list(monitor.KPIS), ["discovered", "located", "uploaded", "verified", "released",
                                              "playable", "missing", "attention"])
