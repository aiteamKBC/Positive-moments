"""
Phase 5B tests: the guarded operator actions.

The question these answer is not "does the button work?" but "what can this
console be made to do that it should not?". So most of them are about refusals:
a settled lecture, a lecture that is not waiting, an action named in a request
body, a stale confirmation, and force-reprocess.
"""
import inspect
import os
import unittest

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.orchestration import actions as platform_actions
from operations import actions as api_actions


SETTLED = "5c84a940-ca3a-5901-9be4-c6db4d199ac4"   # Martech - Fri, finished
WAITING = "ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6"   # Ray-MSP, waiting on attendance
SUPPRESSED = "26e74d25-ea3f-5e3d-ab08-19ab949eb75f"
SEPTEMBER_18 = "2026-09-18"

HAS_DATABASE = bool(os.getenv("DATABASE_URL"))
requires_platform = unittest.skipUnless(
    HAS_DATABASE, "DATABASE_URL is not configured")


class _Authenticated(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-actions-test")
        token = Token.objects.create(user=user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


# --- the shape of the surface ------------------------------------------------------

class ActionSurfaceTests(TestCase):

    def test_there_are_exactly_three_operator_actions(self):
        self.assertEqual(platform_actions.OPERATOR_ACTIONS,
                         {"RETRY", "RECOVER_ATTENDANCE", "RECONCILE"})

    def test_force_reprocess_has_no_route_and_no_implementation(self):
        """
        It creates new provenance. It is an operator CLI decision and nothing
        reachable over HTTP may perform it.
        """
        for module in (api_actions, platform_actions):
            source = inspect.getsource(module)
            self.assertNotIn("FORCE_REPROCESS = ", source)
            self.assertNotIn("force_reprocess(", source)
        from operations import urls
        self.assertNotIn("force", inspect.getsource(urls).lower())

    def test_no_endpoint_accepts_an_action_name_from_the_caller(self):
        """
        The action is in the URL. A body-named action is one validation bug
        away from force-reprocessing a settled lecture.
        """
        source = inspect.getsource(api_actions)
        self.assertNotIn('data.get("action")', source)
        self.assertNotIn("data['action']", source)

    def test_every_action_route_requires_authentication(self):
        anonymous = APIClient()
        for name, kwargs, method in (
            ("operations-retry", {"lecture_id": WAITING}, "post"),
            ("operations-retry-plan", {"lecture_id": WAITING}, "get"),
            ("operations-recover-attendance", {"lecture_id": WAITING}, "post"),
            ("operations-recover-attendance-plan", {"lecture_id": WAITING}, "get"),
            ("operations-reconcile", {}, "post"),
        ):
            with self.subTest(endpoint=name):
                response = getattr(anonymous, method)(reverse(name, kwargs=kwargs))
                self.assertIn(response.status_code, (401, 403))

    def test_a_plan_endpoint_is_a_get_and_an_execution_is_a_post(self):
        """A plan must not be able to change anything, including by accident."""
        user = get_user_model().objects.create(username="ops-method-test")
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        self.assertEqual(
            client.post(reverse("operations-retry-plan",
                                kwargs={"lecture_id": WAITING})).status_code, 405)


# --- live: planning ------------------------------------------------------------------

@requires_platform
class RetryPlanTests(_Authenticated):

    def plan(self, lecture_id):
        return self.client.get(reverse("operations-retry-plan",
                                       kwargs={"lecture_id": lecture_id})).json()

    def test_a_waiting_lecture_is_not_retryable_and_says_why(self):
        plan = self.plan(WAITING)
        self.assertFalse(plan["retry_eligible"])
        self.assertEqual(plan["retry_reason"], "WAITING_ON_EXTERNAL_SOURCE")
        self.assertEqual(plan["resume_stage"], "ATTENDANCE")

    def test_the_plan_states_the_cost_before_anything_is_confirmed(self):
        cost = self.plan(WAITING)["cost"]
        for key in ("calls_microsoft_graph", "buys_model_generation",
                    "writes_legacy_production_row",
                    "affects_other_lectures_that_day"):
            self.assertIn(key, cost)

    def test_the_plan_names_the_stages_that_will_not_be_redone(self):
        """
        Retry resumes; it never reruns. Listing what is preserved is how the
        confirmation shows that.
        """
        plan = self.plan(WAITING)
        self.assertIn("DISCOVERY", plan["stages_preserved"])
        self.assertIn("TRANSCRIPT", plan["stages_preserved"])
        self.assertEqual(plan["semantics"], "RETRY")
        self.assertFalse(plan["force_reprocess"])

    def test_a_settled_lecture_offers_nothing_to_retry(self):
        plan = self.plan(SETTLED)
        self.assertFalse(plan["retry_eligible"])
        self.assertEqual(plan["retry_reason"], "NOTHING_TO_RETRY")

    def test_a_suppressed_duplicate_offers_nothing_to_retry(self):
        self.assertFalse(self.plan(SUPPRESSED)["retry_eligible"])


@requires_platform
class RecoverAttendancePlanTests(_Authenticated):

    def plan(self, lecture_id):
        return self.client.get(
            reverse("operations-recover-attendance-plan",
                    kwargs={"lecture_id": lecture_id})).json()

    def test_it_is_offered_only_when_the_lecture_is_actually_waiting(self):
        self.assertTrue(self.plan(WAITING)["available"])
        settled = self.plan(SETTLED)
        self.assertFalse(settled["available"])
        self.assertEqual(settled["reason"],
                         "LECTURE_IS_NOT_WAITING_ON_ATTENDANCE")

    def test_it_never_advertises_a_model_generation(self):
        """
        Clicking the attendance button must not buy a generation. The plan
        says so, and `execute` builds the orchestrator with the provider off.
        """
        cost = self.plan(WAITING)["cost"]
        self.assertFalse(cost["buys_model_generation"])
        self.assertFalse(cost["reruns_qa"])


# --- live: execution ---------------------------------------------------------------------

@requires_platform
class ExecutionTests(_Authenticated):

    def post(self, name, lecture_id, body=None):
        return self.client.post(reverse(name, kwargs={"lecture_id": lecture_id}),
                                body or {}, format="json")

    def test_retrying_a_settled_lecture_is_refused_with_the_platforms_reason(self):
        response = self.post("operations-retry", SETTLED)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "RETRY_NOT_ELIGIBLE")
        self.assertEqual(response.json()["detail"], "NOTHING_TO_RETRY")

    def test_recovering_attendance_for_a_settled_lecture_is_refused(self):
        response = self.post("operations-recover-attendance", SETTLED)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"],
                         "LECTURE_IS_NOT_WAITING_ON_ATTENDANCE")

    def test_a_stale_confirmation_is_refused_rather_than_executed(self):
        """
        The browser tab said the plan was one thing; the database says another.
        Executing the stale decision would be acting on a screenshot.
        """
        response = self.post("operations-retry", WAITING,
                             {"expected_action": "RUN_QA"})
        self.assertEqual(response.status_code, 409)
        self.assertIn(response.json()["code"],
                      ("RETRY_NOT_ELIGIBLE", "PLAN_CHANGED_SINCE_IT_WAS_SHOWN"))

    def test_recovering_an_empty_attendance_source_costs_nothing_and_writes_nothing(self):
        """
        The real Ray lecture: the source genuinely has no roster. The probe
        runs, finds nothing, and the lecture stays WAITING - which is the
        correct outcome, not a failure.
        """
        response = self.post("operations-recover-attendance", WAITING)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        run = body["run"]
        self.assertEqual(body["semantics"], "RETRY")
        self.assertFalse(body["force_reprocess"])
        self.assertEqual(run["provider_calls"], 0)
        self.assertEqual(run["graph_calls"], 0)
        self.assertEqual(run["legacy_rows_written"], 0)
        # Narrowed to the one lecture the operator asked about.
        self.assertEqual(len(run["lectures"]), 1)
        self.assertEqual(run["lectures"][0]["lecture_id"], WAITING)
        self.assertEqual(run["lectures"][0]["actions"], ["RECOVER_ATTENDANCE"])

    def test_execution_goes_through_the_real_orchestrator(self):
        """
        Not a lighter copy of it. The audit-shaped run summary is the evidence:
        a hand-rolled execution path would not produce one.
        """
        run = self.post("operations-recover-attendance", WAITING).json()["run"]
        for key in ("run_id", "run_type", "orchestration_version",
                    "retry_semantics", "force_reprocess", "passes",
                    "would_write_legacy_qa", "legacy_qa_precheck"):
            self.assertIn(key, run, key)
        self.assertEqual(run["retry_semantics"], "RETRY")
        self.assertFalse(run["force_reprocess"])


# --- live: reconcile is read-only -----------------------------------------------------------

@requires_platform
class ReconcileTests(_Authenticated):

    def test_reconcile_reports_and_runs_nothing(self):
        response = self.client.post(reverse("operations-reconcile"),
                                    {"date": SEPTEMBER_18}, format="json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["read_only"])
        self.assertFalse(body["executed_any_stage"])
        self.assertEqual(body["report"]["canonical_lecture_count"], 7)

    def test_reconcile_cannot_become_a_force_reprocess(self):
        """
        Structural: it does not touch the orchestrator at all, so there is no
        path by which it could execute a stage.
        """
        source = inspect.getsource(platform_actions.GuardedActionService.reconcile)
        body = source.split('"""')[2]  # past the docstring, which DISCUSSES this
        self.assertNotIn("run_window", body)
        self.assertNotIn("orchestrator", body)

    def test_a_malformed_reconcile_date_is_rejected(self):
        response = self.client.post(reverse("operations-reconcile"),
                                    {"date": "not-a-date"}, format="json")
        self.assertEqual(response.status_code, 400)
