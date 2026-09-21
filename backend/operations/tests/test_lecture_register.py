"""
Tests for the two descriptive read endpoints added for the Lectures workspace.

The question they answer is not "is the SQL right?" but "did a second opinion
about pipeline state sneak into a metadata endpoint?". The registry endpoints
exist precisely so the console can show a trainer name without paying for a
stage resolution, and the moment one of them starts reporting whether a
lecture is complete there are two answers to that question in the codebase.
"""
import inspect
import os
import unittest

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.db.repositories import lecture_directory
from operations import views


HAS_DATABASE = bool(os.getenv("DATABASE_URL"))
requires_platform = unittest.skipUnless(
    HAS_DATABASE, "DATABASE_URL is not configured")


class _Authenticated(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="register-test")
        token = Token.objects.create(user=user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


class RegisterContractTests(TestCase):

    def test_both_endpoints_require_authentication(self):
        anonymous = APIClient()
        for name in ("operations-calendar", "operations-directory"):
            with self.subTest(endpoint=name):
                self.assertIn(anonymous.get(reverse(name)).status_code, (401, 403))

    def test_neither_endpoint_accepts_a_write(self):
        user = get_user_model().objects.create(username="register-method-test")
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        for name in ("operations-calendar", "operations-directory"):
            with self.subTest(endpoint=name):
                self.assertEqual(client.post(reverse(name)).status_code, 405)

    def test_the_directory_query_derives_no_pipeline_state(self):
        """
        It may say who taught a lecture. It may not say whether it worked.

        A metadata query that started classifying stages would be a second
        implementation of `app.orchestration`, and the second one drifts.
        """
        sql = lecture_directory.CALENDAR + lecture_directory.DIRECTORY
        for forbidden in ("COMPLETE", "WAITING", "REVIEW_REQUIRED", "FAILED",
                          "eligib", "downstream_ready", "bucket", "CASE"):
            self.assertNotIn(forbidden, sql, forbidden)
        # And no Python branch reintroduces one outside the SQL either.
        body = inspect.getsource(lecture_directory.LectureDirectoryRepository)
        self.assertNotIn("if ", body.split('"""')[-1])

    def test_the_directory_joins_legacy_rows_on_the_occurrence_not_the_series(self):
        """
        A recurring meeting keeps one id across every week. Joining on the id
        alone would attach some arbitrary occurrence's trainer to today's.
        """
        self.assertIn("q.meeting_id = l.meeting_id",
                      lecture_directory.DIRECTORY)
        self.assertIn("q.date = l.session_date", lecture_directory.DIRECTORY)

    def test_the_directory_never_returns_a_meeting_identifier(self):
        self.assertNotIn("meeting_id", lecture_directory.FIELDS)


@requires_platform
class CalendarTests(_Authenticated):

    def test_it_names_the_latest_day_that_actually_has_lectures(self):
        """
        This is what an empty day offers instead of a wall of zeroes. It must
        be a real day from the registry, never today-minus-a-guess.
        """
        body = self.client.get(
            reverse("operations-calendar"),
            {"date_from": "2026-08-01", "date_to": "2026-09-30"}).json()
        dates = [day["session_date"] for day in body["days"]]
        self.assertEqual(body["latest_lecture_date"], "2026-09-18")
        self.assertIn("2026-09-18", dates)
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_a_suppressed_duplicate_is_counted_but_not_as_a_lecture(self):
        body = self.client.get(
            reverse("operations-calendar"),
            {"date_from": "2026-09-18", "date_to": "2026-09-18"}).json()
        day = body["days"][0]
        self.assertEqual(day["lecture_count"], 7)
        self.assertEqual(day["suppressed_duplicate_count"], 1)
        self.assertEqual(day["business_lecture_count"], 6)

    def test_an_empty_window_is_empty_rather_than_an_error(self):
        body = self.client.get(
            reverse("operations-calendar"),
            {"date_from": "2026-01-01", "date_to": "2026-01-31"}).json()
        self.assertEqual(body["days"], [])
        self.assertIsNone(body["latest_lecture_date"])


@requires_platform
class DirectoryTests(_Authenticated):

    def rows(self, **params):
        return self.client.get(reverse("operations-directory"), params).json()

    def test_it_carries_the_trainer_the_legacy_workflows_recorded(self):
        body = self.rows(date_from="2026-09-18", date_to="2026-09-18")
        trainers = {row["subject"]: row["trainer"] for row in body["lectures"]}
        self.assertEqual(trainers["Martech - Fri"], "Keith Rowland")
        self.assertEqual(trainers["Risk Management"], "Andrew millington")

    def test_every_canonical_occurrence_is_present_including_the_duplicate(self):
        """
        The directory is the registry, not the business list. Filtering the
        suppressed occurrence out here would make it invisible to the audit
        view that is supposed to show it.
        """
        body = self.rows(date_from="2026-09-18", date_to="2026-09-18")
        self.assertEqual(body["count"], 7)

    def test_it_reports_the_scheduled_window(self):
        row = self.rows(date_from="2026-09-18", date_to="2026-09-18")["lectures"][0]
        self.assertTrue(row["scheduled_start"])
        self.assertTrue(row["scheduled_end"])

    def test_a_lecture_with_no_legacy_row_reports_no_trainer_rather_than_guessing(self):
        body = self.rows(date_from="2026-09-18", date_to="2026-09-18")
        ray = [row for row in body["lectures"] if row["subject"].startswith("Ray-")]
        self.assertTrue(ray)
        self.assertTrue(any(row["trainer"] is None for row in ray))


class RangeValidationTests(_Authenticated):

    def test_an_inverted_range_is_rejected(self):
        response = self.client.get(reverse("operations-directory"),
                                   {"date_from": "2026-09-18",
                                    "date_to": "2026-09-01"})
        self.assertEqual(response.status_code, 400)

    def test_a_malformed_date_is_rejected(self):
        response = self.client.get(reverse("operations-calendar"),
                                   {"date_from": "yesterday"})
        self.assertEqual(response.status_code, 400)

    def test_the_directory_refuses_an_unbounded_range(self):
        """
        It costs one query, but the query returns a row per lecture. An
        operator must not be able to ask for every lecture ever by accident.
        """
        response = self.client.get(reverse("operations-directory"),
                                   {"date_from": "2020-01-01",
                                    "date_to": "2026-09-18"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(views.DIRECTORY_MAX_DAYS, 31)
