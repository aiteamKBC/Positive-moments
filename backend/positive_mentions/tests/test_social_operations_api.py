"""
Social Media management: prompts, reanalysis runs, AI usage and cost.

Dashboard routes accept any signed-in Social Media Dashboard user (the
project's normal token login) and refuse anonymous callers, invalid tokens and
n8n's service token; n8n routes accept only the service token. Nothing here may
write qa_social_media_opportunities, and nothing is seeded: a prompt has no
text until someone saves one and a cost exists only when reported.
"""
import json
import urllib.error
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions.models import (
    DoctorSession,
    SocialMediaAnalysisRun,
    SocialMediaOpportunity,
    SocialMediaPromptVersion,
    SocialMediaUsageEvent,
)
from positive_mentions.utils import encode_session_id

SERVICE_TOKEN = "s" * 40
# A test webhook only; every test mocks the HTTP call, nothing leaves the machine.
WEBHOOK = {
    "SOCIAL_MEDIA_REANALYSIS_WEBHOOK_URL": "https://n8n.test.invalid/webhook/kent-social-analysis-request",
}
# Test prices only; real prices come from SOCIAL_MEDIA_MODEL_PRICES.
TEST_PRICES = ('{"test-model": {"input": "2.00", "cached_input": "0.50", "output": "10.00",'
               ' "source": "test fixture"}}')
UNMANAGED = (DoctorSession, SocialMediaOpportunity)


def key(session_id):
    return encode_session_id(session_id)


@override_settings(SOCIAL_MEDIA_SERVICE_TOKEN=SERVICE_TOKEN, SOCIAL_MEDIA_MODEL_PRICES="", **WEBHOOK)
class SocialOperationsTestCase(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for model in UNMANAGED:
            model._meta.managed = True
        with connection.schema_editor() as editor:
            for model in UNMANAGED:
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(UNMANAGED):
                editor.delete_model(model)
        for model in UNMANAGED:
            model._meta.managed = False
        super().tearDownClass()

    def setUp(self):
        self.lecture = DoctorSession.objects.create(
            session_id="session-marketing", date=date(2026, 10, 13), subject="AI In Marketing",
            trainer="Sharon Shields", positive_clips=[])
        self.next_opportunity = 1
        self.opportunity(datetime(2026, 10, 1, tzinfo=timezone.utc))
        User = get_user_model()
        self.dashboard_user = User.objects.create_user(
            "mona", email="mona@kentbusinesscollege.com", first_name="Mona", last_name="Hassan")
        self.dashboard = self.client_for(self.dashboard_user)
        self.anonymous = APIClient()
        self.n8n = APIClient()
        self.n8n.credentials(HTTP_AUTHORIZATION=f"Bearer {SERVICE_TOKEN}")
        # The n8n webhook, mocked for every test: it acknowledges with 200.
        patcher = mock.patch("positive_mentions.social_operations.urllib.request.urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.urlopen.return_value.__enter__.return_value.status = 200

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        return client

    def opportunity(self, analyzed_at, session_id="session-marketing"):
        row = SocialMediaOpportunity.objects.create(
            opportunity_id=self.next_opportunity, session_id=session_id,
            opportunity_key=f"{self.next_opportunity}:{self.next_opportunity + 1}",
            start_cue=self.next_opportunity, end_cue=self.next_opportunity + 1,
            formats=["reel"], verification_status="accepted", discovery_source="independent_social",
            opportunity_data={"quote": "unchanged"}, analyzed_at=analyzed_at, updated_at=analyzed_at)
        self.next_opportunity += 1
        return row

    def snapshot_opportunities(self):
        return list(SocialMediaOpportunity.objects.order_by("opportunity_id").values())

    def make_run(self, **overrides):
        values = {"session_id": "session-marketing", "trigger": "on_demand"}
        values.update(overrides)
        return SocialMediaAnalysisRun.objects.create(**values)

    def age(self, run, delta):
        SocialMediaAnalysisRun.objects.filter(pk=run.pk).update(created_at=run.created_at - delta)
        run.refresh_from_db()
        return run


class PermissionTests(SocialOperationsTestCase):
    def dashboard_routes(self):
        run = self.make_run(status="failed", error_code="x")
        lecture = key("session-marketing")
        return [
            ("get", "/api/social-media/prompts/", None),
            ("get", "/api/social-media/prompts/social_discovery/", None),
            ("put", "/api/social-media/prompts/social_discovery/", {"content": "x", "expected_version": 0}),
            ("get", "/api/social-media/prompts/social_discovery/versions/1/", None),
            ("post", "/api/social-media/prompts/social_discovery/rollback/", {"version": 1, "expected_version": 0}),
            ("post", f"/api/social-media/lectures/{lecture}/reanalysis/", {}),
            ("get", f"/api/social-media/lectures/{lecture}/analysis-runs/", None),
            ("get", f"/api/social-media/lectures/{lecture}/costs/", None),
            ("get", f"/api/social-media/analysis-runs/{run.run_id}/", None),
        ]

    def internal_routes(self):
        run = self.make_run(status="failed", error_code="x")
        base = "/api/social-media/internal"
        return [
            ("get", f"{base}/prompts/social_discovery/", None),
            ("post", f"{base}/analysis-runs/", {"session_id": "session-marketing"}),
            ("post", f"{base}/analysis-runs/{run.run_id}/status/", {"status": "running"}),
            ("post", f"{base}/analysis-runs/{run.run_id}/usage/",
             {"event_key": "e", "stage": "discovery", "model": "m", "usage_source": "unavailable"}),
        ]

    def call(self, client, method, path, body):
        return getattr(client, method)(path, body, format="json") if body is not None else getattr(client, method)(path)

    def test_callers_without_a_dashboard_login_are_refused_and_change_nothing(self):
        invalid = APIClient()
        invalid.credentials(HTTP_AUTHORIZATION="Token " + "0" * 40)
        # n8n's service token is not a dashboard login.
        for client in (self.anonymous, invalid, self.n8n):
            for method, path, body in self.dashboard_routes():
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.call(client, method, path, body).status_code, 401)
        self.assertFalse(SocialMediaPromptVersion.objects.exists())
        self.assertFalse(SocialMediaAnalysisRun.objects.filter(status="queued").exists())

    def test_an_inactive_account_is_refused(self):
        self.dashboard_user.is_active = False
        self.dashboard_user.save()
        self.assertEqual(self.dashboard.get("/api/social-media/prompts/").status_code, 401)

    def test_any_dashboard_user_can_use_every_route(self):
        # No staff flag, no superuser, no allowlist: the same login as the rest of Social Media.
        plain = self.client_for(get_user_model().objects.create_user("lecturer"))
        lecture = key("session-marketing")
        self.assertEqual(plain.get("/api/social-media/lectures/").status_code, 200)
        saved = plain.put("/api/social-media/prompts/social_discovery/",
                          {"content": "first", "expected_version": 0}, format="json")
        self.assertEqual((saved.status_code, saved.json()["active"]["created_by"]), (200, "lecturer"))
        self.assertEqual(self.dashboard.put("/api/social-media/prompts/social_discovery/",
                                            {"content": "second", "expected_version": 1},
                                            format="json").status_code, 200)
        restored = plain.post("/api/social-media/prompts/social_discovery/rollback/",
                              {"version": 1, "expected_version": 2}, format="json")
        self.assertEqual((restored.status_code, restored.json()["active"]["created_by"]), (200, "lecturer"))
        requested = plain.post(f"/api/social-media/lectures/{lecture}/reanalysis/")
        self.assertEqual((requested.status_code, requested.json()["run"]["requested_by"]), (202, "lecturer"))
        run_id = requested.json()["run"]["run_id"]
        for path in ("/api/social-media/prompts/", "/api/social-media/prompts/social_discovery/",
                     "/api/social-media/prompts/social_discovery/versions/1/",
                     f"/api/social-media/lectures/{lecture}/analysis-runs/",
                     f"/api/social-media/lectures/{lecture}/costs/", f"/api/social-media/analysis-runs/{run_id}/"):
            with self.subTest(path=path):
                self.assertEqual(plain.get(path).status_code, 200)

    @override_settings(DASHBOARD_USERNAME="kbc-dashboard", DASHBOARD_PASSWORD="dashboard-password")
    def test_the_shared_dashboard_login_works_and_is_named_in_the_audit(self):
        login = APIClient().post("/api/auth/login/", {"username": "kbc-dashboard",
                                                      "password": "dashboard-password"}, format="json")
        self.assertEqual(login.status_code, 200)
        shared = APIClient()
        shared.credentials(HTTP_AUTHORIZATION=f"Token {login.json()['token']}")
        saved = shared.put("/api/social-media/prompts/social_verification/",
                           {"content": "text", "expected_version": 0}, format="json")
        # Its stored email is a placeholder, so the username is what the audit shows.
        self.assertEqual((saved.status_code, saved.json()["active"]["created_by"]), (200, "kbc-dashboard"))

    def test_there_is_no_separate_role_endpoint(self):
        self.assertEqual(self.dashboard.get("/api/social-media/access/").status_code, 404)

    def test_internal_routes_refuse_users_and_bad_tokens(self):
        wrong = APIClient()
        wrong.credentials(HTTP_AUTHORIZATION="Bearer " + "x" * 40)
        as_token = APIClient()
        as_token.credentials(HTTP_AUTHORIZATION=f"Token {SERVICE_TOKEN}")
        for client in (self.anonymous, self.dashboard, wrong, as_token):
            for method, path, body in self.internal_routes():
                with self.subTest(path=path):
                    self.assertEqual(self.call(client, method, path, body).status_code, 403)
        self.assertFalse(SocialMediaUsageEvent.objects.exists())

    def test_internal_routes_are_closed_without_a_strong_secret(self):
        for configured in ("", "short-secret"):
            with self.subTest(configured=configured), override_settings(SOCIAL_MEDIA_SERVICE_TOKEN=configured):
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {configured}")
                for method, path, body in self.internal_routes():
                    self.assertEqual(self.call(client, method, path, body).status_code, 403)


class PromptTests(SocialOperationsTestCase):
    url = "/api/social-media/prompts/social_discovery/"

    def save(self, content, expected, client=None):
        return (client or self.dashboard).put(self.url, {"content": content, "expected_version": expected},
                                            format="json")

    def test_nothing_is_seeded(self):
        listing = self.dashboard.get("/api/social-media/prompts/").json()
        self.assertEqual(listing, {"prompts": [{"prompt_key": "social_discovery", "active": None},
                                               {"prompt_key": "social_verification", "active": None}]})
        self.assertEqual(self.dashboard.get(self.url).json(),
                         {"prompt_key": "social_discovery", "active": None, "versions": []})
        self.assertEqual(self.n8n.get("/api/social-media/internal/prompts/social_discovery/").status_code, 404)

    def test_replace_keeps_the_exact_text_and_who_saved_it(self):
        text = "  You are the Social Media Discovery agent.\r\n\n\tReturn JSON — only.  \n"
        response = self.save(text, 0)
        self.assertEqual(response.status_code, 200)
        active = response.json()["active"]
        self.assertEqual(active["content"], text)
        self.assertEqual(active["version"], 1)
        self.assertEqual(active["created_by"], "Mona Hassan")
        self.assertIsNotNone(active["created_at"])
        self.assertEqual(len(active["content_sha256"]), 64)
        self.assertEqual(self.dashboard.get(self.url).json()["active"]["content"], text)
        # The other prompt is untouched.
        self.assertIsNone(self.dashboard.get("/api/social-media/prompts/social_verification/").json()["active"])

    def test_history_is_kept_and_rollback_adds_a_version(self):
        self.save("first", 0)
        self.save("second", 1)
        detail = self.dashboard.get(self.url).json()
        self.assertEqual(detail["active"]["content"], "second")
        self.assertEqual([v["version"] for v in detail["versions"]], [2, 1])
        self.assertNotIn("content", detail["versions"][0])
        self.assertEqual(self.dashboard.get(f"{self.url}versions/1/").json()["content"], "first")

        response = self.dashboard.post(f"{self.url}rollback/", {"version": 1, "expected_version": 2}, format="json")
        self.assertEqual(response.status_code, 200)
        active = response.json()["active"]
        self.assertEqual((active["version"], active["content"], active["restored_from_version"]), (3, "first", 1))
        self.assertEqual(list(SocialMediaPromptVersion.objects.order_by("version")
                              .values_list("version", "content")),
                         [(1, "first"), (2, "second"), (3, "first")])
        self.assertEqual(self.n8n.get("/api/social-media/internal/prompts/social_discovery/").json()["content"],
                         "first")

    def test_a_stale_editor_cannot_overwrite(self):
        self.save("first", 0)
        response = self.save("from a stale tab", 0)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")
        self.assertEqual(response.json()["active_version"], 1)
        response = self.dashboard.post(f"{self.url}rollback/", {"version": 1, "expected_version": 0}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(SocialMediaPromptVersion.objects.count(), 1)

    def test_invalid_saves_are_refused(self):
        self.save("first", 0)
        cases = [
            ({"content": "   ", "expected_version": 1}, 400),
            ({"content": "text"}, 400),
            ({"content": "text", "expected_version": "1"}, 400),
            ({"content": "text", "expected_version": True}, 400),
            ({"content": 5, "expected_version": 1}, 400),
            ({"content": "x" * 200_001, "expected_version": 1}, 400),
            ({"content": "first", "expected_version": 1}, 409),
        ]
        for body, expected in cases:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self.dashboard.put(self.url, body, format="json").status_code, expected)
        self.assertEqual(self.dashboard.post(f"{self.url}rollback/", {"version": 9, "expected_version": 1},
                                           format="json").status_code, 404)
        self.assertEqual(self.dashboard.get("/api/social-media/prompts/other/").status_code, 404)
        self.assertEqual(self.dashboard.put("/api/social-media/prompts/other/",
                                          {"content": "x", "expected_version": 0}, format="json").status_code, 404)
        self.assertEqual(SocialMediaPromptVersion.objects.count(), 1)


class ReanalysisTests(SocialOperationsTestCase):
    def request(self, session_id="session-marketing", client=None):
        return (client or self.dashboard).post(f"/api/social-media/lectures/{key(session_id)}/reanalysis/")

    def sent(self):
        """The one request sent to the webhook: (url, headers, body)."""
        self.assertEqual(self.urlopen.call_count, 1)
        request = self.urlopen.call_args.args[0]
        return request.full_url, dict(request.header_items()), json.loads(request.data)

    def test_dispatches_to_n8n_and_claims_nothing(self):
        before = self.snapshot_opportunities()
        response = self.request()
        self.assertEqual(response.status_code, 202)
        run = response.json()["run"]
        uuid.UUID(run["run_id"])
        # n8n's acknowledgement is not progress: still queued, only dispatched_at is set.
        self.assertEqual((run["status"], run["trigger"], run["requested_by"], run["stale"]),
                         ("queued", "on_demand", "Mona Hassan", False))
        self.assertIsNotNone(run["dispatched_at"])
        for field in ("started_at", "finished_at", "opportunities_verified"):
            self.assertIsNone(run[field])
        url, headers, body = self.sent()
        self.assertEqual(url, WEBHOOK["SOCIAL_MEDIA_REANALYSIS_WEBHOOK_URL"])
        # The run Django just created, and no other.
        self.assertEqual(SocialMediaAnalysisRun.objects.count(), 1)
        self.assertEqual(body, {"run_id": run["run_id"], "session_id": "session-marketing", "mode": "reanalyze"})
        self.assertEqual(str(SocialMediaAnalysisRun.objects.get().run_id), run["run_id"])
        # The n8n webhook has no authentication: JSON headers only, nothing else.
        self.assertEqual(headers, {"Content-type": "application/json", "Accept": "application/json"})
        self.assertEqual(self.snapshot_opportunities(), before)
        # The webhook URL is never returned to the browser.
        self.assertNotIn("n8n.test.invalid", response.content.decode())

    def test_a_refused_or_unreachable_webhook_fails_the_run(self):
        for failure in (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), urllib.error.URLError("down")):
            with self.subTest(failure=type(failure).__name__):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = failure
                response = self.request()
                self.assertEqual((response.status_code, response.json()["code"]),
                                 (502, "reanalysis_request_failed"))
                # The run that was failed is the one whose id was sent.
                failed = SocialMediaAnalysisRun.objects.order_by("-created_at").first()
                self.assertEqual(self.sent()[2], {"run_id": str(failed.run_id), "session_id": "session-marketing",
                                                  "mode": "reanalyze"})
                self.assertEqual((failed.status, failed.error_code), ("failed", "dispatch_failed"))
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value.status = 500
        self.assertEqual(self.request().status_code, 502)
        self.assertEqual(set(SocialMediaAnalysisRun.objects.values_list("status", "error_code")),
                         {("failed", "dispatch_failed")})
        # The lecture is free again.
        self.urlopen.return_value.__enter__.return_value.status = 200
        self.assertEqual(self.request().status_code, 202)

    def test_not_configured_means_no_run_and_no_call(self):
        for missing in WEBHOOK:
            with self.subTest(missing=missing), override_settings(**{missing: ""}):
                response = self.request()
                self.assertEqual((response.status_code, response.json()["code"]),
                                 (503, "reanalysis_not_configured"))
        with override_settings(SOCIAL_MEDIA_REANALYSIS_WEBHOOK_URL="http://n8n.test.invalid/webhook/x"):
            self.assertEqual(self.request().status_code, 503)
        self.urlopen.assert_not_called()
        self.assertFalse(SocialMediaAnalysisRun.objects.exists())

    def test_missing_lecture_is_refused(self):
        self.assertEqual(self.request("no-such-lecture").status_code, 404)
        self.assertEqual(self.dashboard.post("/api/social-media/lectures/%%%/reanalysis/").status_code, 404)
        self.assertFalse(SocialMediaAnalysisRun.objects.exists())
        self.urlopen.assert_not_called()

    def test_one_active_run_per_lecture_and_one_webhook_call(self):
        first = self.request().json()["run"]["run_id"]
        response = self.request()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"code": "analysis_in_progress",
                                           "detail": "This lecture is already being analysed.", "run_id": first})
        self.assertEqual(self.urlopen.call_count, 1)
        # Reading the runs never calls n8n.
        for _ in range(3):
            self.assertEqual(self.dashboard.get(
                f"/api/social-media/lectures/{key('session-marketing')}/analysis-runs/").status_code, 200)
        self.assertEqual(self.urlopen.call_count, 1)
        # The scheduled workflow is held to the same rule.
        response = self.n8n.post("/api/social-media/internal/analysis-runs/",
                                 {"session_id": "session-marketing"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(SocialMediaAnalysisRun.objects.count(), 1)

    def test_an_old_active_run_is_shown_as_stale(self):
        run = self.age(self.make_run(dispatched_at=datetime.now(timezone.utc)), timedelta(hours=2))
        runs = self.dashboard.get(f"/api/social-media/lectures/{key('session-marketing')}/analysis-runs/").json()
        self.assertEqual((runs["runs"][0]["run_id"], runs["runs"][0]["stale"]), (str(run.run_id), True))

    def test_database_enforces_one_active_run(self):
        self.make_run()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_run(status="running")

    def test_a_stale_run_is_failed_honestly_when_a_new_one_is_requested(self):
        never_sent = self.age(self.make_run(), timedelta(hours=2))
        self.assertEqual(self.request().status_code, 202)
        never_sent.refresh_from_db()
        self.assertEqual((never_sent.status, never_sent.error_code), ("failed", "not_dispatched"))

        SocialMediaAnalysisRun.objects.filter(status="queued").update(status="failed", error_code="x")
        sent = self.age(self.make_run(status="running", dispatched_at=datetime.now(timezone.utc)), timedelta(hours=2))
        self.assertEqual(self.request().status_code, 202)
        sent.refresh_from_db()
        self.assertEqual((sent.status, sent.error_code), ("failed", "timed_out"))

    def test_scheduled_runs_are_opened_by_n8n(self):
        response = self.n8n.post("/api/social-media/internal/analysis-runs/",
                                 {"session_id": "session-marketing", "n8n_execution_id": "exec-1"}, format="json")
        self.assertEqual(response.status_code, 201)
        run = response.json()["run"]
        self.assertEqual((run["trigger"], run["status"], run["requested_by"], run["n8n_execution_id"]),
                         ("scheduled", "queued", None, "exec-1"))
        for body in ({"session_id": "nope"}, {}):
            self.assertIn(self.n8n.post("/api/social-media/internal/analysis-runs/", body,
                                        format="json").status_code, (400, 404))


class RunStatusTests(SocialOperationsTestCase):
    def report(self, run, **body):
        return self.n8n.post(f"/api/social-media/internal/analysis-runs/{run.run_id}/status/", body, format="json")

    def test_running_then_verified_completion(self):
        run = self.make_run()
        response = self.report(run, status="running", n8n_execution_id="exec-7")
        self.assertEqual(response.status_code, 200)
        body = response.json()["run"]
        self.assertEqual(body["status"], "running")
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["dispatched_at"])
        self.assertEqual(self.report(run, status="running").json()["changed"], False)

        # Two rows written by this run; the older row does not count.
        now = datetime.now(timezone.utc)
        self.opportunity(now)
        self.opportunity(now)
        before = self.snapshot_opportunities()
        response = self.report(run, status="completed", opportunities_reported=2)
        self.assertEqual(response.status_code, 200)
        body = response.json()["run"]
        self.assertEqual((body["status"], body["opportunities_reported"], body["opportunities_verified"]),
                         ("completed", 2, 2))
        self.assertEqual(self.snapshot_opportunities(), before)
        self.assertEqual(self.report(run, status="completed", opportunities_reported=2).json()["changed"], False)
        self.assertEqual(self.report(run, status="failed", error_code="late").status_code, 409)
        self.assertEqual(self.report(run, status="running").status_code, 409)

    def test_completion_without_matching_rows_is_refused(self):
        run = self.make_run(status="running")
        response = self.report(run, status="completed", opportunities_reported=3)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "result_not_verified")
        self.assertEqual(response.json()["opportunities_found"], 0)
        run.refresh_from_db()
        self.assertEqual((run.status, run.finished_at), ("running", None))

    def test_a_lecture_with_no_new_opportunities_can_complete(self):
        run = self.make_run(status="running")
        self.assertEqual(self.report(run, status="completed", opportunities_reported=0).json()["run"]["status"],
                         "completed")

    def test_failure_needs_a_reason(self):
        run = self.make_run()
        self.assertEqual(self.report(run, status="failed").status_code, 400)
        response = self.report(run, status="failed", error_code="openai_error", error_message="HTTP 500")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["run"]["status"], response.json()["run"]["error_code"]),
                         ("failed", "openai_error"))
        self.assertEqual(self.report(run, status="running").status_code, 409)

    def test_a_timed_out_run_may_still_report_its_real_outcome(self):
        run = self.make_run(status="failed", error_code="timed_out")
        self.assertEqual(self.report(run, status="running").json()["run"]["status"], "running")
        run.refresh_from_db()
        self.assertIsNone(run.error_code)

    def test_invalid_reports(self):
        run = self.make_run(n8n_execution_id="exec-1")
        self.assertEqual(self.report(run, status="done").status_code, 400)
        self.assertEqual(self.report(run, status="completed").status_code, 400)
        self.assertEqual(self.report(run, status="completed", opportunities_reported=-1).status_code, 400)
        self.assertEqual(self.report(run, status="running", n8n_execution_id="exec-2").status_code, 409)
        missing = SocialMediaAnalysisRun(run_id=uuid.uuid4())
        self.assertEqual(self.report(missing, status="running").status_code, 404)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")


@override_settings(SOCIAL_MEDIA_MODEL_PRICES=TEST_PRICES)
class UsageAndCostTests(SocialOperationsTestCase):
    def usage(self, run, **body):
        values = {"event_key": "exec-1:discovery:0", "stage": "discovery", "model": "test-model",
                  "usage_source": "reported", "input_tokens": 10_000, "cached_input_tokens": 4_000,
                  "output_tokens": 2_000, "openai_response_id": "resp_1",
                  "occurred_at": "2026-10-19T09:00:00+00:00"}
        values.update(body)
        values = {name: value for name, value in values.items() if value is not ...}
        return self.n8n.post(f"/api/social-media/internal/analysis-runs/{run.run_id}/usage/", values, format="json")

    def test_reported_usage_is_a_measured_cost(self):
        response = self.usage(self.make_run())
        self.assertEqual(response.status_code, 201)
        event = response.json()["event"]
        # (6,000 x 2.00 + 4,000 x 0.50 + 2,000 x 10.00) / 1,000,000
        self.assertEqual(event["cost_usd"], "0.03400000")
        self.assertEqual(event["cost_status"], "measured")
        self.assertEqual(event["pricing"], {"input_per_million": "2.000000", "cached_input_per_million": "0.500000",
                                            "output_per_million": "10.000000", "source": "test fixture"})

    def test_estimated_and_unknown_are_kept_apart(self):
        run = self.make_run()
        self.assertEqual(self.usage(run, event_key="a", usage_source="estimated").json()["event"]["cost_status"],
                         "estimated")
        unknown = self.usage(run, event_key="b", usage_source="unavailable", input_tokens=None,
                             cached_input_tokens=None, output_tokens=None).json()["event"]
        self.assertEqual((unknown["cost_status"], unknown["cost_usd"], unknown["input_tokens"]),
                         ("unknown", None, None))
        unpriced = self.usage(run, event_key="c", model="unpriced-model").json()["event"]
        self.assertEqual((unpriced["cost_status"], unpriced["cost_usd"], unpriced["input_tokens"]),
                         ("unknown", None, 10_000))
        self.assertIsNone(unpriced["pricing"]["input_per_million"])
        with override_settings(SOCIAL_MEDIA_MODEL_PRICES="{not json"):
            self.assertEqual(self.usage(run, event_key="d").json()["event"]["cost_status"], "unknown")
        with override_settings(SOCIAL_MEDIA_MODEL_PRICES='{"test-model": {"input": "2", "output": "10"}}'):
            self.assertEqual(self.usage(run, event_key="e").json()["event"]["cost_status"], "unknown")
        # More precision than the price column holds is refused, not rounded.
        with override_settings(SOCIAL_MEDIA_MODEL_PRICES=(
                '{"test-model": {"input": "2.0000001", "cached_input": "0.5", "output": "10"}}')):
            self.assertEqual(self.usage(run, event_key="f").json()["event"]["cost_status"], "unknown")

    def test_a_retried_event_is_counted_once(self):
        run = self.make_run()
        self.assertEqual(self.usage(run).status_code, 201)
        replay = self.usage(run)
        self.assertEqual((replay.status_code, replay.json()["created"]), (200, False))
        self.assertEqual(self.usage(run, output_tokens=9).status_code, 409)
        self.assertEqual(SocialMediaUsageEvent.objects.count(), 1)

    def test_invalid_usage_is_refused(self):
        run = self.make_run()
        cases = [
            {"event_key": ...},
            {"stage": "publish"},
            {"model": ...},
            {"usage_source": "guess"},
            {"input_tokens": None},
            {"cached_input_tokens": ...},
            {"cached_input_tokens": 20_000},
            {"output_tokens": -1},
            {"output_tokens": True},
            {"output_tokens": 1.5},
            {"usage_source": "unavailable"},
            {"occurred_at": "yesterday"},
            {"occurred_at": "2026-10-19T09:00:00"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.usage(run, **case).status_code, 400)
        self.assertEqual(self.usage(SocialMediaAnalysisRun(run_id=uuid.uuid4())).status_code, 404)
        self.assertFalse(SocialMediaUsageEvent.objects.exists())

    def test_usage_of_a_failed_run_still_counts(self):
        run = self.make_run(status="failed", error_code="openai_error")
        self.assertEqual(self.usage(run).status_code, 201)

    def test_database_refuses_a_cost_status_without_a_value(self):
        run = self.make_run()
        with self.assertRaises(IntegrityError), transaction.atomic():
            SocialMediaUsageEvent.objects.create(run=run, event_key="x", stage="discovery", model="m",
                                                 usage_source="reported", cost_status="measured", cost_usd=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SocialMediaUsageEvent.objects.create(run=run, event_key="y", stage="discovery", model="m",
                                                 usage_source="reported", cost_status="unknown",
                                                 cost_usd=Decimal("0.1"))

    def test_lecture_costs_and_attempt_history(self):
        lecture = key("session-marketing")
        empty = self.dashboard.get(f"/api/social-media/lectures/{lecture}/costs/").json()
        self.assertEqual(empty, {
            "session_id": "session-marketing", "runs": 0, "runs_by_status": {}, "currency": "USD",
            "measured_cost_usd": "0.00000000", "estimated_cost_usd": "0.00000000",
            "known_cost_usd": "0.00000000", "usage_events": 0, "unknown_cost_events": 0, "cost_complete": True,
            "tokens": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        })

        older = self.make_run(status="failed", error_code="openai_error")
        older = self.age(older, timedelta(days=1))
        self.usage(older, event_key="o1")                                     # measured 0.034
        newer = self.make_run(status="running")
        self.usage(newer, event_key="n1", stage="verification", usage_source="estimated",
                   input_tokens=1_000, cached_input_tokens=0, output_tokens=100)   # estimated 0.003
        self.usage(newer, event_key="n2", usage_source="unavailable", input_tokens=None,
                   cached_input_tokens=None, output_tokens=None)              # unknown
        # Another lecture's cost is not this lecture's.
        DoctorSession.objects.create(session_id="session-other", positive_clips=[])
        self.usage(self.make_run(session_id="session-other"), event_key="x")

        costs = self.dashboard.get(f"/api/social-media/lectures/{lecture}/costs/").json()
        self.assertEqual(costs["runs"], 2)
        self.assertEqual(costs["runs_by_status"], {"failed": 1, "running": 1})
        self.assertEqual((costs["measured_cost_usd"], costs["estimated_cost_usd"], costs["known_cost_usd"]),
                         ("0.03400000", "0.00300000", "0.03700000"))
        self.assertEqual((costs["usage_events"], costs["unknown_cost_events"], costs["cost_complete"]), (3, 1, False))
        self.assertEqual(costs["tokens"], {"input_tokens": 11_000, "cached_input_tokens": 4_000,
                                           "output_tokens": 2_100})

        runs = self.dashboard.get(f"/api/social-media/lectures/{lecture}/analysis-runs/").json()["runs"]
        self.assertEqual([r["run_id"] for r in runs], [str(newer.run_id), str(older.run_id)])
        self.assertEqual((runs[0]["cost"]["estimated_cost_usd"], runs[0]["cost"]["unknown_cost_events"]),
                         ("0.00300000", 1))
        self.assertEqual((runs[1]["cost"]["measured_cost_usd"], runs[1]["cost"]["cost_complete"]),
                         ("0.03400000", True))

        detail = self.dashboard.get(f"/api/social-media/analysis-runs/{newer.run_id}/").json()
        self.assertEqual([e["event_key"] for e in detail["usage_events"]], ["n1", "n2"])
        self.assertEqual(detail["cost"]["known_cost_usd"], "0.00300000")
        self.assertEqual(self.dashboard.get(f"/api/social-media/analysis-runs/{uuid.uuid4()}/").status_code, 404)
        self.assertEqual(self.dashboard.get(f"/api/social-media/lectures/{key('nope')}/costs/").status_code, 404)
        self.assertEqual(self.dashboard.get(f"/api/social-media/lectures/{key('nope')}/analysis-runs/").status_code,
                         404)
