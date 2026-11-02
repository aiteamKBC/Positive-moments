"""
Historical Backfill API contract (Publishing Monitor).

The platform database is patched: these tests never open a platform connection and never reach Graph, the
LMS or Bunny. They pin the HTTP layer - authentication (every signed-in Publishing user may preview, start,
cancel and retry; nobody unauthenticated may do anything), range validation,
that a preview request only queues the preview (the publisher service discovers the range), that a start is
accepted only for a COMPLETE, fresh preview whose stored state has not changed, that a second active run is
refused, and that no secret reaches the browser. Discovery and the run itself are covered in
tests/integration/test_publisher_backfill*.py.
"""
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from operations import publisher_backfill_views as views

from .test_publishing_monitor_api import SERVICE_TOKEN

NOW = datetime.now(timezone.utc)
RUN_ID = "cccccccc-0000-0000-0000-000000000001"
FROM, TO = "2026-09-01", "2026-09-05"


def row(lecture_id, category, *, origin="TEAMS_NEW", basis="DRY_RUN", day="2026-09-01", subject="Lecture"):
    return {"lecture_id": lecture_id, "session_date": day, "subject": subject, "module": None, "origin": origin,
            "scheduled_start": f"{day}T09:00:00+00:00", "scheduled_end": f"{day}T11:00:00+00:00",
            "category": category, "basis": basis, "proposed_action": "QUEUE", "reason": "r",
            "verification": {"teams": "LIVE", "lms": "LIVE_DRY_RUN", "recording": "TO_BE_VERIFIED_AT_RUN"}}


L1, L2, L3 = (str(uuid.uuid5(uuid.NAMESPACE_URL, f"l{n}")) for n in (1, 2, 3))


class State:
    def __init__(self):
        self.runs: dict = {}
        self.items: dict = {}
        self.writes = 0
        self.stored = {L2: "RESUME_REQUIRED"}

    def add(self, run, items=()):
        self.runs[str(run["run_id"])] = run
        self.items[str(run["run_id"])] = list(items)
        return run


STATE = State()


def run_row(**over):
    base = {"run_id": RUN_ID, "mode": "EXECUTE", "preview_run_id": None, "date_from": date(2026, 9, 1),
            "date_to": date(2026, 9, 5), "library_id": 786045, "status": "QUEUED", "requested_by": "operator",
            "reason": "", "preview_fingerprint": "fp", "preview_summary": {}, "discovery": {}, "lms_matched_at": None,
            "lms_status": None, "last_tick_at": None, "last_tick": {}, "cancel_requested_at": None,
            "cancel_requested_by": None, "created_at": NOW, "started_at": None, "finished_at": None, "updated_at": NOW}
    base.update(over)
    return base


def finished_preview(*, status="COMPLETED", age=timedelta(minutes=1), eligible=2, rows=None):
    rows = rows if rows is not None else [row(L1, "READY_TO_QUEUE"), row(L2, "RESUME_REQUIRED", origin="TEAMS_EXISTING",
                                                                         basis="STORED"),
                                          row(L3, "MISSING_LMS_MATCH")]
    run = run_row(run_id=str(uuid.uuid4()), mode="PREVIEW", status=status, finished_at=NOW - age,
                  preview_summary={"eligible": eligible, "total": len(rows), "new": 2, "incomplete_days":
                                   [] if status == "COMPLETED" else ["2026-09-03"]},
                  discovery={"2026-09-01": {"status": "COMPLETE", "lectures": [{"subject": "x"}]}})
    return STATE.add(run, [{"detail": r, "lecture_id": r["lecture_id"], "status": "PREVIEWED"} for r in rows])


class FakeRepository:
    def create(self, connection, *, mode="EXECUTE", items=None, **kwargs):
        if any(r["mode"] == mode and r["status"] in ("QUEUED", "RUNNING") for r in STATE.runs.values()):
            return None
        run = run_row(run_id=str(uuid.uuid4()), mode=mode, date_from=kwargs["date_from"], date_to=kwargs["date_to"],
                      requested_by=kwargs["requested_by"], preview_run_id=kwargs.get("preview_run_id"))
        STATE.writes += 1
        return STATE.add(run, [{"detail": i, **i} for i in items or ()])

    def active(self, connection, mode="EXECUTE"):
        return next((r for r in STATE.runs.values() if r["mode"] == mode and r["status"] in ("QUEUED", "RUNNING")), None)

    def get(self, connection, run_id):
        return STATE.runs.get(str(run_id))

    def items(self, connection, run_id, statuses=None):
        return STATE.items.get(str(run_id), [])

    def counts(self, connection, run_id):
        return {s: 0 for s in ("PENDING", "PROCESSING", "COMPLETED", "SKIPPED", "BLOCKED", "FAILED", "CANCELLED")}

    def recent(self, connection, limit=25, mode="EXECUTE"):
        return [r for r in STATE.runs.values() if r["mode"] == mode][:limit]

    def request_cancel(self, connection, run_id, requested_by):
        run = STATE.runs.get(str(run_id))
        if run is None or run["status"] not in ("QUEUED", "RUNNING"):
            return None
        run.update(cancel_requested_at=NOW, cancel_requested_by=requested_by)
        return run


@contextmanager
def fake_connection():
    connection = mock.MagicMock()
    connection.transaction.return_value.__enter__ = lambda *a: None
    connection.transaction.return_value.__exit__ = lambda *a: False
    yield connection


def patched(test):
    for target, value in (("reading", fake_connection), ("writing", fake_connection),
                          ("stored_categories", lambda *a: dict(STATE.stored)),
                          ("BackfillRepository", FakeRepository), ("PublisherQueueRepository", mock.MagicMock)):
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
        self.member = users.create_user("member", password="unused-password-2")     # no group, staff or superuser

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        return client

    def request_preview(self, client, date_from=FROM, date_to=TO):
        return client.post(reverse("operations-publisher-backfill-preview"), {"date_from": date_from, "date_to": date_to},
                           format="json")

    def start(self, client, preview, **over):
        body = {"preview_run_id": str(preview["run_id"]), "reason": "September recordings",
                "confirm": {"library_id": 786045, "eligible": preview["preview_summary"]["eligible"]}}
        body.update(over)
        return client.post(reverse("operations-publisher-backfill-start"), body, format="json")


ROUTES = (("operations-publisher-backfill-runs", {}, "get"), ("operations-publisher-backfill-preview", {}, "post"),
          ("operations-publisher-backfill-preview-detail", {"run_id": RUN_ID}, "get"),
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

    def test_every_signed_in_publishing_user_may_operate_without_any_extra_role(self):
        member = self.client_for(self.member)
        self.assertFalse(self.member.groups.exists() or self.member.is_staff or self.member.is_superuser)
        body = member.get(reverse("operations-publisher-backfill-runs")).json()
        self.assertNotIn("operator", body)
        self.assertEqual(self.start(member, finished_preview()).status_code, 202)
        run_id = next(r["run_id"] for r in STATE.runs.values() if r["mode"] == "EXECUTE")
        cancel = member.post(reverse("operations-publisher-backfill-cancel", kwargs={"run_id": run_id}), {}, format="json")
        self.assertEqual(cancel.status_code, 202)
        with mock.patch("operations.publisher_backfill_views.backfill.retry_items",
                        return_value={"run_id": run_id, "lectures_reopened": 0, "jobs_reopened": 0, "refused": [],
                                      "lms_rematch": False}):
            retry = member.post(reverse("operations-publisher-backfill-retry", kwargs={"run_id": run_id}),
                                {"reason": "LMS session created"}, format="json")
        self.assertEqual(retry.status_code, 202)

    def test_an_unauthenticated_request_can_never_start_a_run(self):
        preview, forged = finished_preview(), APIClient()
        forged.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
        for client in (APIClient(), forged):
            self.assertEqual(self.start(client, preview).status_code, 401)
        self.assertFalse(any(r["mode"] == "EXECUTE" for r in STATE.runs.values()))
        self.assertEqual(STATE.writes, 0)


@patched
class PreviewTests(Base):
    def test_a_preview_request_only_queues_the_preview_and_reports_no_production_work(self):
        answer = self.request_preview(self.client_for(self.viewer))
        self.assertEqual(answer.status_code, 202)
        body = answer.json()
        self.assertEqual((body["preview"]["mode"], body["preview"]["status"]), ("PREVIEW", "QUEUED"))
        self.assertEqual([c["date"] for c in body["preview"]["coverage"]],
                         ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"])
        self.assertEqual((body["production_writes"], body["jobs_queued"], body["bunny_calls"]), (0, 0, 0))
        self.assertFalse(body["complete"] or body["confirmable"])

    def test_asking_again_for_the_same_range_returns_the_preview_in_progress(self):
        client = self.client_for(self.viewer)
        first = self.request_preview(client).json()["preview"]["run_id"]
        again = self.request_preview(client)
        self.assertEqual((again.status_code, again.json()["preview"]["run_id"]), (200, first))
        other = self.request_preview(client, FROM, "2026-09-02")
        self.assertEqual((other.status_code, other.json()["code"]), (409, "PREVIEW_IN_PROGRESS"))

    def test_a_finished_preview_shows_its_lectures_coverage_and_whether_it_can_be_confirmed(self):
        preview, viewer = finished_preview(), self.client_for(self.viewer)
        body = viewer.get(reverse("operations-publisher-backfill-preview-detail",
                                  kwargs={"run_id": preview["run_id"]})).json()
        self.assertTrue(body["complete"] and body["confirmable"])
        self.assertEqual([r["lecture_id"] for r in body["lectures"]], [L1, L2, L3])
        self.assertNotIn("lectures", body["preview"]["coverage"][0])   # per-day codes only, no lecture list
        incomplete = finished_preview(status="INCOMPLETE")
        body = viewer.get(reverse("operations-publisher-backfill-preview-detail",
                                  kwargs={"run_id": incomplete["run_id"]})).json()
        self.assertFalse(body["complete"] or body["confirmable"])

    def test_a_preview_lists_qa_only_lectures_and_each_days_two_sources_without_their_lists(self):
        qa_only = {"lecture_id": None, "qa_ref": "a1b2c3", "session_date": "2026-09-02", "subject": "Old course",
                   "origin": "QA_ONLY", "sources": ["QA"], "category": "MISSING_LMS_MATCH",
                   "code": "QA_ONLY_NO_LMS_SESSION", "missing_evidence": ["LMS_SESSION"]}
        preview = finished_preview()
        preview["preview_summary"]["qa_only_lectures"] = [qa_only]
        preview["discovery"]["2026-09-02"] = {
            "status": "PARTIAL", "teams": {"status": "FAILED", "error": "CALENDAR_QUERY_ERROR"},
            "qa": {"status": "COMPLETE", "records": 1, "entries": [{"qa_ref": "a1b2c3"}]}}
        body = self.client_for(self.viewer).get(reverse("operations-publisher-backfill-preview-detail",
                                                        kwargs={"run_id": preview["run_id"]})).json()
        self.assertEqual([r["lecture_id"] for r in body["lectures"]], [L1, L2, L3, None])
        self.assertEqual(body["lectures"][-1]["code"], "QA_ONLY_NO_LMS_SESSION")
        day = body["preview"]["coverage"][1]
        self.assertEqual((day["status"], day["teams"]["status"], day["qa"]), ("PARTIAL", "FAILED",
                                                                              {"status": "COMPLETE", "records": 1}))

    def test_a_run_shows_its_qa_only_lectures_as_blocked_with_the_missing_evidence(self):
        qa_only = {"qa_ref": "a1b2c3", "session_date": "2026-09-02", "subject": "Old course", "module": None,
                   "origin": "QA_ONLY", "sources": ["QA"], "category": "MISSING_LMS_MATCH",
                   "code": "QA_ONLY_NO_LMS_SESSION", "reason": "Only in the QA database.",
                   "missing_evidence": ["TEAMS_CALENDAR_OCCURRENCE", "EXACT_SCHEDULED_START", "LMS_SESSION"]}
        STATE.add(run_row(status="RUNNING"))
        with mock.patch("operations.publisher_backfill_views.backfill.run_qa_only", lambda c, run: [qa_only]):
            body = self.client_for(self.viewer).get(reverse("operations-publisher-backfill-run",
                                                            kwargs={"run_id": RUN_ID})).json()
        [row_] = body["lectures"]
        self.assertEqual((row_["lecture_id"], row_["status"], row_["reason"], row_["registered"]),
                         (None, "BLOCKED", "QA_ONLY_NO_LMS_SESSION", False))
        self.assertEqual(body["run"]["qa_only"], 1)

    def test_invalid_reversed_future_and_oversized_ranges_are_refused(self):
        client = self.client_for(self.viewer)
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        for date_from, date_to, code in ((TO, FROM, "INVALID_RANGE"), (FROM, future, "FUTURE_DATE"),
                                         ("2026-07-01", TO, "RANGE_TOO_LONG"), ("", TO, "INVALID_DATE"),
                                         ("01/09/2026", TO, "INVALID_DATE")):
            response = self.request_preview(client, date_from, date_to)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), (date_from, date_to))
        self.assertEqual(STATE.writes, 0)

    def test_no_secret_reaches_the_browser(self):
        preview = finished_preview(rows=[row(L1, "READY_TO_QUEUE", subject=f"https://x.invalid/?sig=a Bearer {SERVICE_TOKEN}")])
        text = self.client_for(self.viewer).get(reverse("operations-publisher-backfill-preview-detail",
                                                        kwargs={"run_id": preview["run_id"]})).content.decode()
        self.assertNotIn(SERVICE_TOKEN, text)
        self.assertNotIn("https://", text)


@patched
class StartTests(Base):
    def test_a_complete_preview_starts_one_run_including_lectures_only_in_teams(self):
        preview = finished_preview()
        response = self.start(self.client_for(self.member), preview)
        self.assertEqual(response.status_code, 202, response.content)
        run = next(r for r in STATE.runs.values() if r["mode"] == "EXECUTE")
        self.assertEqual((run["preview_run_id"], run["requested_by"]), (preview["run_id"], "member"))
        items = {i["lecture_id"]: i for i in STATE.items[str(run["run_id"])]}
        self.assertEqual((items[L1]["status"], items[L1]["origin"]), ("PENDING", "TEAMS_NEW"))
        self.assertEqual(items[L2]["status"], "PENDING")
        self.assertEqual(items[L3]["status"], "BLOCKED")

    def test_only_a_complete_finished_fresh_preview_can_be_confirmed(self):
        member = self.client_for(self.member)
        missing = self.start(member, {"run_id": "not-a-run", "preview_summary": {"eligible": 1}})
        self.assertEqual((missing.status_code, missing.json()["code"]), (400, "PREVIEW_REQUIRED"))
        for preview, code in ((finished_preview(status="INCOMPLETE"), "PREVIEW_INCOMPLETE"),
                              (finished_preview(age=timedelta(minutes=45)), "PREVIEW_EXPIRED")):
            refused = self.start(member, preview)
            self.assertEqual((refused.status_code, refused.json()["code"]), (409, code))
        running = STATE.add(run_row(run_id=str(uuid.uuid4()), mode="PREVIEW", status="RUNNING", preview_summary={"eligible": 1}))
        self.assertEqual(self.start(member, running).json()["code"], "PREVIEW_NOT_READY")
        self.assertFalse(any(r["mode"] == "EXECUTE" for r in STATE.runs.values()))

    def test_a_stale_preview_or_a_wrong_confirmation_is_refused(self):
        member = self.client_for(self.member)
        preview = finished_preview()
        wrong = self.start(member, preview, confirm={"library_id": 785080, "eligible": 2})
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (409, "CONFIRMATION_MISMATCH"))
        STATE.stored[L2] = "ALREADY_PROCESSING"                       # its stored state changed after the preview
        stale = self.start(member, preview)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "PREVIEW_STALE"))
        self.assertFalse(any(r["mode"] == "EXECUTE" for r in STATE.runs.values()))

    def test_a_second_start_is_refused_while_a_run_is_active(self):
        member = self.client_for(self.member)
        self.assertEqual(self.start(member, finished_preview()).status_code, 202)
        again = self.start(member, finished_preview())
        self.assertEqual((again.status_code, again.json()["code"]), (409, "BACKFILL_ALREADY_ACTIVE"))

    def test_nothing_eligible_is_not_started(self):
        refused = self.start(self.client_for(self.member), finished_preview(eligible=0, rows=[row(L3, "MISSING_LMS_MATCH")]))
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "NOTHING_TO_QUEUE"))


@patched
class CancelAndRetryTests(Base):
    def test_cancel_requests_the_stop_of_the_active_run_only(self):
        member = self.client_for(self.member)
        url = reverse("operations-publisher-backfill-cancel", kwargs={"run_id": RUN_ID})
        self.assertEqual(member.post(url, {}, format="json").status_code, 404)
        STATE.add(run_row(status="RUNNING"))
        response = member.post(url, {}, format="json")
        self.assertEqual((response.status_code, response.json()["run"]["cancel_requested"]), (202, True))

    def test_retry_needs_a_reason_and_goes_through_the_audited_path(self):
        member = self.client_for(self.member)
        url = reverse("operations-publisher-backfill-retry", kwargs={"run_id": RUN_ID})
        self.assertEqual(member.post(url, {}, format="json").json()["code"], "REASON_REQUIRED")
        with mock.patch("operations.publisher_backfill_views.backfill.retry_items",
                        return_value={"run_id": RUN_ID, "lectures_reopened": 2, "jobs_reopened": 1, "refused": [],
                                      "lms_rematch": False}) as retry:
            response = member.post(url, {"reason": "Bunny outage fixed"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(retry.call_args.kwargs, {"actor": "member", "reason": "Bunny outage fixed"})
