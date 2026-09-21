"""
QA Core RC2 tests: the Operations Backfill API contract.

These ask three things of the HTTP layer and nothing more:

  * is every route authenticated?
  * does a bad date range come back as the operator's mistake, not a 500?
  * does a POST return promptly with a queued run rather than doing the work?

What they deliberately do NOT test is the pipeline. A backfill day is a
`PipelineOrchestrator.run_window` call, and that is covered by the orchestrator's
own suite plus `tests/unit/test_backfill.py`. Re-asserting it here through HTTP
would be a second, weaker copy of those tests.
"""
import os
import unittest
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.db.repositories.backfill import MODE_EXECUTE, MODE_PREVIEW

RUN_ID = "11111111-2222-3333-4444-555555555555"

HAS_DATABASE = bool(os.getenv("DATABASE_URL"))
requires_platform = unittest.skipUnless(
    HAS_DATABASE, "DATABASE_URL is not configured")


def a_run(**overrides):
    return {"backfill_run_id": RUN_ID, "requested_from": date(2026, 9, 1),
            "requested_to": date(2026, 9, 21), "status": "PENDING",
            "mode": MODE_EXECUTE, "created_by": "ops-test",
            "total_days": 21, "completed_days": 0, **overrides}


class _Authenticated(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops-test")
        token = Token.objects.create(user=user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


# --- authentication -----------------------------------------------------------

class AuthenticationTests(TestCase):
    ENDPOINTS = (
        ("get", "operations-backfills", {}),
        ("post", "operations-backfill-preview", {}),
        ("post", "operations-backfill-start", {}),
        ("get", "operations-backfill-detail", {"run_id": RUN_ID}),
        ("post", "operations-backfill-cancel", {"run_id": RUN_ID}),
    )

    def test_every_backfill_endpoint_requires_authentication(self):
        """
        Including the two that write. An anonymous caller must not be able to
        queue a month of Graph calls and model generations.
        """
        client = APIClient()
        for method, name, kwargs in self.ENDPOINTS:
            with self.subTest(endpoint=name):
                response = getattr(client, method)(reverse(name, kwargs=kwargs))
                self.assertIn(response.status_code, (401, 403),
                              f"{name} answered {response.status_code} anonymously")


# --- the range is validated before anything is written -------------------------

class RangeValidationTests(_Authenticated):
    """
    Every case here must be refused BEFORE a row is created, so the repository
    is patched to explode if it is reached.
    """

    def post(self, payload, name="operations-backfill-preview"):
        with mock.patch("operations.backfill_views._repository",
                        side_effect=AssertionError("a row was created")):
            return self.client.post(reverse(name), payload, format="json")

    def test_a_reversed_range_is_the_operators_mistake_not_a_server_error(self):
        response = self.post({"from": "2026-09-21", "to": "2026-09-01"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_BACKFILL_RANGE")

    def test_an_implausible_range_is_refused_with_its_own_code(self):
        response = self.post({"from": "2026-01-01", "to": "2026-12-31"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "BACKFILL_RANGE_TOO_LARGE")

    def test_a_missing_date_is_refused(self):
        response = self.post({"from": "2026-09-01"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_parameter")

    def test_an_unparseable_date_is_refused(self):
        response = self.post({"from": "1st September", "to": "2026-09-21"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_parameter")

    def test_the_same_validation_guards_the_start_endpoint(self):
        response = self.post({"from": "2026-09-21", "to": "2026-09-01"},
                             name="operations-backfill-start")
        self.assertEqual(response.status_code, 400)


# --- creating a run -------------------------------------------------------------

class CreateTests(_Authenticated):

    def _post(self, name, mode, payload=None):
        repository = mock.Mock()
        repository.create.return_value = RUN_ID
        repository.get.return_value = a_run(mode=mode)
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.writing"):
            response = self.client.post(
                reverse(name), payload or {"from": "2026-09-01", "to": "2026-09-21"},
                format="json")
        return response, repository

    def test_preview_queues_a_preview_run_and_returns_immediately(self):
        """
        202, not 200: the work has not happened yet and the response must not
        imply that it has.
        """
        response, repository = self._post(
            "operations-backfill-preview", MODE_PREVIEW)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(repository.create.call_args.kwargs["mode"], MODE_PREVIEW)
        self.assertEqual(response.data["backfill_run"]["mode"], MODE_PREVIEW)

    def test_start_queues_an_execute_run(self):
        response, repository = self._post(
            "operations-backfill-start", MODE_EXECUTE)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(repository.create.call_args.kwargs["mode"], MODE_EXECUTE)

    def test_the_whole_requested_range_is_counted_as_days(self):
        _, repository = self._post("operations-backfill-start", MODE_EXECUTE)
        self.assertEqual(repository.create.call_args.kwargs["total_days"], 21)

    def test_the_signed_in_operator_is_recorded(self):
        """Who asked for a month of writes is part of the audit, not optional."""
        _, repository = self._post("operations-backfill-start", MODE_EXECUTE)
        self.assertEqual(repository.create.call_args.kwargs["created_by"], "ops-test")

    def test_a_single_day_range_is_accepted(self):
        response, repository = self._post(
            "operations-backfill-preview", MODE_PREVIEW,
            payload={"from": "2026-09-16", "to": "2026-09-16"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(repository.create.call_args.kwargs["total_days"], 1)

    def test_creating_a_run_does_not_run_a_pipeline_cycle(self):
        """
        The endpoint must not do the work. If it ever starts calling the
        orchestrator, a September request will hang until gunicorn kills it.
        """
        with mock.patch("app.orchestration.orchestrator.PipelineOrchestrator"
                        ".run_window") as run_window:
            self._post("operations-backfill-start", MODE_EXECUTE)
        run_window.assert_not_called()


# --- reading --------------------------------------------------------------------

class ReadTests(_Authenticated):

    def test_the_list_returns_runs_and_the_active_one(self):
        repository = mock.Mock()
        repository.recent.return_value = [a_run()]
        repository.active.return_value = a_run(status="RUNNING")
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.reading"):
            response = self.client.get(reverse("operations-backfills"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["backfill_runs"]), 1)
        self.assertEqual(response.data["active"]["status"], "RUNNING")

    def test_progress_is_computed_for_the_console(self):
        repository = mock.Mock()
        repository.get.return_value = a_run(total_days=21, completed_days=7)
        repository.days.return_value = []
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.reading"):
            response = self.client.get(
                reverse("operations-backfill-detail", kwargs={"run_id": RUN_ID}))

        self.assertEqual(response.data["backfill_run"]["progress_percent"], 33)
        self.assertFalse(response.data["backfill_run"]["is_finished"])

    def test_a_finished_run_says_so(self):
        repository = mock.Mock()
        repository.get.return_value = a_run(status="COMPLETED",
                                            total_days=21, completed_days=21)
        repository.days.return_value = []
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.reading"):
            response = self.client.get(
                reverse("operations-backfill-detail", kwargs={"run_id": RUN_ID}))

        self.assertTrue(response.data["backfill_run"]["is_finished"])
        self.assertEqual(response.data["backfill_run"]["progress_percent"], 100)

    def test_an_unknown_run_is_a_404(self):
        repository = mock.Mock()
        repository.get.return_value = None
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.reading"):
            response = self.client.get(
                reverse("operations-backfill-detail", kwargs={"run_id": RUN_ID}))

        self.assertEqual(response.status_code, 404)

    def test_dates_are_serialised_as_iso_strings(self):
        repository = mock.Mock()
        repository.get.return_value = a_run()
        repository.days.return_value = []
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.reading"):
            response = self.client.get(
                reverse("operations-backfill-detail", kwargs={"run_id": RUN_ID}))

        self.assertEqual(response.data["backfill_run"]["requested_from"],
                         "2026-09-01")


# --- cancel ------------------------------------------------------------------------

class CancelTests(_Authenticated):

    def _cancel(self, accepted, run):
        repository = mock.Mock()
        repository.request_cancel.return_value = accepted
        repository.get.return_value = run
        with mock.patch("operations.backfill_views._repository",
                        return_value=repository), \
             mock.patch("operations.backfill_views.writing"):
            response = self.client.post(
                reverse("operations-backfill-cancel", kwargs={"run_id": RUN_ID}))
        return response, repository

    def test_cancelling_a_running_run_is_accepted(self):
        response, repository = self._cancel(True, a_run(status="CANCEL_REQUESTED"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(repository.request_cancel.call_args.kwargs["requested_by"],
                         "ops-test")
        self.assertIn("finish", response.data["detail"])

    def test_cancelling_a_finished_run_says_so_rather_than_pretending(self):
        response, _ = self._cancel(False, a_run(status="COMPLETED"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "ALREADY_FINISHED")

    def test_cancelling_an_unknown_run_is_a_404(self):
        response, _ = self._cancel(False, None)
        self.assertEqual(response.status_code, 404)


# --- against the real platform -------------------------------------------------------

@requires_platform
class LiveTests(_Authenticated):
    """
    Read-only, against the real database. These prove the routes work end to
    end without creating a run - nothing here queues work.
    """

    def test_the_list_endpoint_answers_from_the_real_database(self):
        response = self.client.get(reverse("operations-backfills"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("backfill_runs", response.data)

    def test_a_real_range_validation_rejects_before_touching_the_database(self):
        far = (date.today() + timedelta(days=400)).isoformat()
        response = self.client.post(
            reverse("operations-backfill-preview"),
            {"from": date.today().isoformat(), "to": far}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "BACKFILL_RANGE_TOO_LARGE")
