"""
Historical Backfill API contract (Publishing Monitor).

The platform database is patched: these tests never open a platform connection and never reach Graph, the
LMS or Bunny. They pin the HTTP layer - authentication, the operator role on every write, range validation,
that the preview is read-only, that a start is bound to a fresh, signed, matching confirmation, that a second
active run is refused, and that no secret reaches the browser. The run itself is covered in
tests/integration/test_publisher_backfill.py.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.publisher import backfill
from operations import publisher_backfill_views as views

from .test_publishing_monitor_api import SERVICE_TOKEN, fact

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
RUN_ID = "cccccccc-0000-0000-0000-000000000001"
FROM, TO = "2026-09-01", "2026-09-10"


def facts():
    return [fact(1, session_date=date(2026, 9, 1), match_status=None, recording_status=None, occurrence_id=None),
            fact(2, session_date=date(2026, 9, 10), match_status=None, recording_status=None, occurrence_id=None),
            fact(3, session_date=date(2026, 9, 5), match_status="NO_LMS_SESSION", match_review=True)]


class State:
    """What the patched platform holds between requests."""

    def __init__(self):
        self.facts = facts()
        self.created, self.cancelled, self.writes, self.reads = [], [], 0, 0
        self.active = None


STATE = State()


def fake_preview(date_from, date_to):
    rows = [f for f in STATE.facts if date_from <= f["session_date"] <= date_to]
    return backfill.build_preview(rows, {}, {}, date_from=date_from, date_to=date_to, library_id=786045, now=NOW)


def run_row(**over):
    base = {"run_id": RUN_ID, "date_from": date(2026, 9, 1), "date_to": date(2026, 9, 10), "library_id": 786045,
            "status": "QUEUED", "requested_by": "operator", "reason": "", "preview_fingerprint": "x",
            "preview_summary": {}, "lms_matched_at": None, "lms_status": None, "last_tick_at": None,
            "last_tick": {}, "cancel_requested_at": None, "cancel_requested_by": None, "created_at": NOW,
            "started_at": None, "finished_at": None, "updated_at": NOW}
    base.update(over)
    return base


class FakeRepository:
    def create(self, connection, **kwargs):
        if STATE.active is not None:
            return None
        STATE.created.append(kwargs)
        STATE.active = run_row(requested_by=kwargs["requested_by"])
        return STATE.active

    def active(self, connection):
        return STATE.active

    def counts(self, connection, run_id):
        return {s: 0 for s in ("PENDING", "PROCESSING", "COMPLETED", "SKIPPED", "BLOCKED", "FAILED", "CANCELLED")}

    def recent(self, connection, limit=25):
        return [STATE.active] if STATE.active else []

    def get(self, connection, run_id):
        return STATE.active

    def items(self, connection, run_id, statuses=None):
        return []

    def request_cancel(self, connection, run_id, requested_by):
        if STATE.active is None:
            return None
        STATE.cancelled.append(requested_by)
        return {**STATE.active, "cancel_requested_at": NOW, "cancel_requested_by": requested_by}


@contextmanager
def fake_reading():
    STATE.reads += 1
    yield mock.MagicMock()


@contextmanager
def fake_writing():
    STATE.writes += 1
    connection = mock.MagicMock()
    connection.transaction.return_value.__enter__ = lambda *a: None
    connection.transaction.return_value.__exit__ = lambda *a: False
    yield connection


def patched(test):
    for target, value in (("reading", fake_reading), ("writing", fake_writing), ("compute_preview", fake_preview),
                          ("BackfillRepository", FakeRepository),
                          ("PublisherQueueRepository", mock.MagicMock)):
        test = mock.patch(f"operations.publisher_backfill_views.{target}", value)(test)
    test = mock.patch("operations.publishing_monitor_views._rows", lambda *a: ([], NOW.isoformat()))(test)
    return override_settings(PUBLISHER_LMS_LIBRARY_ID=786045, PUBLISHER_LMS_SERVICE_TOKENS=[SERVICE_TOKEN],
                             PUBLISHER_BACKFILL_MAX_DAYS=31)(test)


class Base(TestCase):
    def setUp(self):
        global STATE
        STATE = State()
        users = get_user_model().objects
        self.viewer = users.create_user("viewer", password="unused-password-1")
        self.operator = users.create_user("operator", password="unused-password-2")
        call_command("publisher_backfill_operator", "--grant", "operator", stdout=mock.MagicMock())

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        return client

    def preview(self, client, date_from=FROM, date_to=TO):
        return client.post(reverse("operations-publisher-backfill-preview"), {"date_from": date_from, "date_to": date_to},
                           format="json")

    def start(self, client, answer, **over):
        body = {"date_from": answer["date_from"], "date_to": answer["date_to"], "preview_token": answer["preview_token"],
                "confirm": {"library_id": answer["library_id"], "eligible": answer["summary"]["eligible"]},
                "reason": "September recordings"}
        body.update(over)
        return client.post(reverse("operations-publisher-backfill-start"), body, format="json")


ROUTES = (("operations-publisher-backfill-runs", {}, "get"), ("operations-publisher-backfill-preview", {}, "post"),
          ("operations-publisher-backfill-start", {}, "post"),
          ("operations-publisher-backfill-run", {"run_id": RUN_ID}, "get"),
          ("operations-publisher-backfill-cancel", {"run_id": RUN_ID}, "post"),
          ("operations-publisher-backfill-retry", {"run_id": RUN_ID}, "post"))


@patched
class AccessTests(Base):
    def test_every_route_requires_a_signed_in_user_and_rejects_the_lms_service_token(self):
        service = APIClient()
        service.credentials(HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}")
        for name, kwargs, method in ROUTES:
            for client in (APIClient(), service):
                response = getattr(client, method)(reverse(name, kwargs=kwargs), {}, format="json")
                self.assertEqual(response.status_code, 401, name)

    def test_writes_need_the_operator_role_and_reads_do_not(self):
        viewer = self.client_for(self.viewer)
        self.assertEqual(self.preview(viewer).status_code, 200)
        self.assertEqual(viewer.get(reverse("operations-publisher-backfill-runs")).json()["operator"], False)
        for name, kwargs, method in ROUTES:
            if method == "post" and name != "operations-publisher-backfill-preview":
                response = viewer.post(reverse(name, kwargs=kwargs), {}, format="json")
                self.assertEqual(response.status_code, 403, name)
        self.assertEqual(STATE.writes, 0)
        operator = self.client_for(self.operator)
        self.assertEqual(operator.get(reverse("operations-publisher-backfill-runs")).json()["operator"], True)

    def test_the_operator_role_is_granted_and_revoked_by_command_only_for_existing_users(self):
        out = mock.MagicMock()
        call_command("publisher_backfill_operator", "--revoke", "operator", stdout=out)
        self.assertFalse(views.is_operator(self.operator))
        call_command("publisher_backfill_operator", "--grant", "operator", stdout=out)
        self.assertTrue(views.is_operator(self.operator))
        self.assertFalse(self.operator.is_staff or self.operator.is_superuser)
        with self.assertRaises(CommandError):
            call_command("publisher_backfill_operator", "--grant", "nobody", stdout=out)


@patched
class PreviewTests(Base):
    def test_the_preview_includes_both_boundary_dates_and_writes_nothing(self):
        answer = self.preview(self.client_for(self.viewer)).json()
        self.assertEqual((answer["date_from"], answer["date_to"], answer["days"]), (FROM, TO, 10))
        self.assertEqual(sorted(r["session_date"] for r in answer["lectures"]), [FROM, "2026-09-05", TO])
        self.assertEqual(answer["summary"]["eligible"], 2)
        self.assertEqual(answer["summary"]["blocked"], 1)
        self.assertEqual((answer["read_only"], answer["database_writes"], answer["jobs_queued"], answer["bunny_calls"]),
                         (True, 0, 0, 0))
        self.assertTrue(answer["preview_token"])
        self.assertEqual(STATE.writes, 0)

    def test_a_single_day_preview(self):
        answer = self.preview(self.client_for(self.viewer), FROM, FROM).json()
        self.assertEqual((answer["days"], [r["session_date"] for r in answer["lectures"]]), (1, [FROM]))

    def test_invalid_reversed_future_and_oversized_ranges_are_refused(self):
        client = self.client_for(self.viewer)
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        for date_from, date_to, code in ((TO, FROM, "INVALID_RANGE"), (FROM, future, "FUTURE_DATE"),
                                         ("2026-07-01", TO, "RANGE_TOO_LONG"), ("", TO, "INVALID_DATE"),
                                         ("01/09/2026", TO, "INVALID_DATE")):
            response = self.preview(client, date_from, date_to)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), (date_from, date_to))

    def test_no_secret_reaches_the_browser(self):
        STATE.facts[0]["subject"] = f"Lecture https://example.invalid/x?sig=abc Bearer {SERVICE_TOKEN}"
        text = self.preview(self.client_for(self.viewer)).content.decode()
        self.assertNotIn(SERVICE_TOKEN, text)
        self.assertNotIn("https://", text)


@patched
class StartTests(Base):
    def test_a_confirmed_preview_starts_one_run_with_every_lecture_recorded(self):
        operator = self.client_for(self.operator)
        answer = self.preview(operator).json()
        response = self.start(operator, answer)
        self.assertEqual(response.status_code, 202, response.content)
        [created] = STATE.created
        self.assertEqual((created["date_from"], created["date_to"], created["library_id"]),
                         (date(2026, 9, 1), date(2026, 9, 10), 786045))
        self.assertEqual(created["requested_by"], "operator")
        self.assertEqual(sorted(i["status"] for i in created["items"]), ["BLOCKED", "PENDING", "PENDING"])
        self.assertEqual(response.json()["run"]["status"], "QUEUED")

    def test_a_second_start_is_refused_while_a_run_is_active(self):
        operator = self.client_for(self.operator)
        answer = self.preview(operator).json()
        self.assertEqual(self.start(operator, answer).status_code, 202)
        again = self.start(operator, self.preview(operator).json())
        self.assertEqual((again.status_code, again.json()["code"]), (409, "BACKFILL_ALREADY_ACTIVE"))
        self.assertEqual(len(STATE.created), 1)

    def test_a_start_needs_a_preview_of_the_same_range(self):
        operator = self.client_for(self.operator)
        answer = self.preview(operator).json()
        missing = self.start(operator, answer, preview_token="")
        self.assertEqual((missing.status_code, missing.json()["code"]), (400, "PREVIEW_REQUIRED"))
        forged = self.start(operator, answer, preview_token=answer["preview_token"][:-2] + "xx")
        self.assertEqual(forged.json()["code"], "PREVIEW_REQUIRED")
        other = self.start(operator, answer, date_to="2026-09-09")
        self.assertEqual((other.status_code, other.json()["code"]), (400, "PREVIEW_MISMATCH"))
        self.assertEqual(STATE.created, [])

    def test_a_stale_preview_or_a_wrong_confirmation_is_refused(self):
        operator = self.client_for(self.operator)
        answer = self.preview(operator).json()
        wrong = self.start(operator, answer, confirm={"library_id": 785080, "eligible": 2})
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (409, "CONFIRMATION_MISMATCH"))
        STATE.facts[1]["match_status"] = "NO_LMS_SESSION"            # the range changed after the preview
        stale = self.start(operator, answer)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "PREVIEW_STALE"))
        self.assertEqual(stale.json()["summary"]["eligible"], 1)
        self.assertEqual(STATE.created, [])

    def test_an_expired_preview_is_refused(self):
        operator = self.client_for(self.operator)
        answer = self.preview(operator).json()
        later = datetime.now(timezone.utc).timestamp() + views.PREVIEW_TTL + 60
        with mock.patch("django.core.signing.time.time", return_value=later):
            expired = self.start(operator, answer)
        self.assertEqual((expired.status_code, expired.json()["code"]), (409, "PREVIEW_EXPIRED"))

    def test_nothing_eligible_is_not_started(self):
        STATE.facts = [fact(3, session_date=date(2026, 9, 5), match_status="NO_LMS_SESSION", match_review=True)]
        operator = self.client_for(self.operator)
        refused = self.start(operator, self.preview(operator).json())
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "NOTHING_TO_QUEUE"))


@patched
class CancelAndRetryTests(Base):
    def test_cancel_requests_the_stop_of_the_active_run_only(self):
        operator = self.client_for(self.operator)
        url = reverse("operations-publisher-backfill-cancel", kwargs={"run_id": RUN_ID})
        self.assertEqual(operator.post(url, {}, format="json").status_code, 404)        # no such run
        STATE.active = run_row(status="RUNNING")
        response = operator.post(url, {}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual((STATE.cancelled, response.json()["run"]["cancel_requested"]), (["operator"], True))

    def test_retry_needs_a_reason_and_goes_through_the_audited_path(self):
        operator = self.client_for(self.operator)
        url = reverse("operations-publisher-backfill-retry", kwargs={"run_id": RUN_ID})
        self.assertEqual(operator.post(url, {}, format="json").json()["code"], "REASON_REQUIRED")
        with mock.patch("operations.publisher_backfill_views.backfill.retry_items",
                        return_value={"run_id": RUN_ID, "lectures_reopened": 2, "jobs_reopened": 1, "refused": [],
                                      "lms_rematch": False}) as retry:
            response = operator.post(url, {"reason": "Bunny outage fixed"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(retry.call_args.kwargs, {"actor": "operator", "reason": "Bunny outage fixed"})
