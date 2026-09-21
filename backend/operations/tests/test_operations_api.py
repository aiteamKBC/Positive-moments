"""
Phase 5A tests: the Operations API contract.

Two kinds of test live here, answering different questions.

The CONTRACT tests ask "does the HTTP layer authenticate, refuse bad input and
route correctly?". They need no lecture data.

The LIVE tests ask "does the API tell the truth about the platform?". They read
the real pilot rows through the platform's own psycopg connection - read-only,
independent of Django's test database - because a stub would simply agree with
whatever the view expects. They skip when DATABASE_URL is unset.

What neither kind may do is re-derive a pipeline rule. If a test here needs to
decide for itself what COMPLETE means, that is a sign the rule has leaked out
of `app.orchestration`.
"""
import inspect
import os
import unittest

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.orchestration.stages import STAGE_ORDER
from operations import views


SEPTEMBER_18 = "2026-09-18"
RAY_SUPPRESSED = "26e74d25-ea3f-5e3d-ab08-19ab949eb75f"
RAY_WAITING = "ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6"

HAS_DATABASE = bool(os.getenv("DATABASE_URL"))
requires_platform = unittest.skipUnless(
    HAS_DATABASE, "DATABASE_URL is not configured")


class _Authenticated(TestCase):
    """A signed-in operator. The console is internal; nothing here is public."""

    def setUp(self):
        user = get_user_model().objects.create(username="ops-test")
        token = Token.objects.create(user=user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def get(self, name, params=None, **kwargs):
        return self.client.get(reverse(name, kwargs=kwargs), params or {})


# --- authentication ---------------------------------------------------------------

class AuthenticationTests(TestCase):
    ENDPOINTS = (
        ("operations-day", {}),
        ("operations-lectures", {}),
        ("operations-runs", {}),
        ("operations-pending-attendance", {}),
        ("operations-review-required", {}),
        ("operations-errors", {}),
        ("operations-precheck", {}),
        ("operations-lecture", {"lecture_id": RAY_WAITING}),
        ("operations-lecture-history", {"lecture_id": RAY_WAITING}),
    )

    def test_every_endpoint_requires_authentication(self):
        """
        DRF's project default is IsAuthenticated. This asserts that no view has
        quietly opted out of it - which is the one-line change that would turn
        an internal console into a public one.
        """
        anonymous = APIClient()
        for name, kwargs in self.ENDPOINTS:
            with self.subTest(endpoint=name):
                response = anonymous.get(reverse(name, kwargs=kwargs))
                self.assertIn(response.status_code, (401, 403))


# --- parameters ---------------------------------------------------------------------

class ParameterTests(_Authenticated):

    def test_a_malformed_date_is_the_callers_mistake_not_a_server_error(self):
        response = self.get("operations-day", {"date": "18-09-2026"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_parameter")

    def test_a_malformed_limit_is_rejected(self):
        response = self.get("operations-runs", {"limit": "lots"})
        self.assertEqual(response.status_code, 400)

    def test_an_unparseable_lecture_id_never_reaches_the_platform(self):
        """The URL converter refuses it, so no connection is ever opened."""
        self.assertEqual(
            self.client.get("/api/operations/lectures/not-a-uuid/").status_code, 404)


# --- structural -----------------------------------------------------------------------

class ReadPathTests(TestCase):

    def test_no_view_reaches_for_graph_a_provider_or_a_writer(self):
        """
        Loading a page must never cost money or change production data. The
        cheapest way to keep that true is for the imports not to exist.
        """
        source = inspect.getsource(views)
        for forbidden in ("OpenAIChatProvider", "build_graph_client",
                          "TranscriptGateway", "LegacyQaWriter", "run_cycle",
                          "AttendanceRecoveryService", "run_window"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_read_helper_is_the_only_one_the_views_use(self):
        """`writing()` exists for the guarded actions and nothing else yet."""
        source = inspect.getsource(views)
        self.assertIn("reading", source)
        self.assertNotIn("writing", source)


# --- live: the day -----------------------------------------------------------------------

@requires_platform
class DayTests(_Authenticated):

    def test_the_day_endpoint_reports_the_real_2026_09_18(self):
        body = self.get("operations-day", {"date": SEPTEMBER_18}).json()
        self.assertEqual(body["canonical_lecture_count"], 7)
        self.assertEqual(body["business_lecture_count"], 6)
        self.assertEqual(body["suppressed_duplicate_count"], 1)
        self.assertEqual(body["failed_count"], 0)
        self.assertEqual(body["review_count"], 0)

    def test_the_counts_still_partition_the_day_over_http(self):
        body = self.get("operations-day", {"date": SEPTEMBER_18}).json()
        total = (body["complete_count"] + body["waiting_count"]
                 + body["review_count"] + body["failed_count"]
                 + body["in_progress_count"] + body["suppressed_duplicate_count"])
        self.assertEqual(total, body["canonical_lecture_count"])

    def test_date_navigation_is_offered_in_the_business_timezone(self):
        navigation = self.get(
            "operations-day", {"date": SEPTEMBER_18}).json()["navigation"]
        self.assertEqual(navigation["previous_date"], "2026-09-17")
        self.assertEqual(navigation["next_date"], "2026-09-19")
        self.assertEqual(navigation["timezone"], "Africa/Cairo")

    def test_a_day_with_no_lectures_is_an_empty_day_not_an_error(self):
        body = self.get("operations-day", {"date": "2026-09-13"}).json()
        self.assertEqual(body["canonical_lecture_count"], 0)
        self.assertEqual(body["lectures"], [])


# --- live: the lecture list ------------------------------------------------------------------

@requires_platform
class LectureListTests(_Authenticated):

    def test_a_suppressed_duplicate_is_not_listed_as_a_lecture(self):
        body = self.get("operations-lectures", {"date": SEPTEMBER_18}).json()
        listed = {row["lecture_id"] for row in body["lectures"]}
        self.assertNotIn(RAY_SUPPRESSED, listed)
        self.assertIn(RAY_WAITING, listed)
        self.assertEqual(len(body["lectures"]), 6)
        self.assertEqual(body["business_lecture_count"], 6)
        self.assertNotIn("suppressed_duplicates", body)
        self.assertEqual(body["suppressed_duplicate_count"], 1)

    def test_the_suppressed_source_event_is_available_when_asked_for(self):
        """
        Excluded from the lecture table, never hidden. The source calendar
        really did contain the extra event.
        """
        body = self.get("operations-lectures",
                        {"date": SEPTEMBER_18, "include_suppressed": "true"}).json()
        suppressed = body["suppressed_duplicates"]
        self.assertEqual([row["lecture_id"] for row in suppressed],
                         [RAY_SUPPRESSED])
        self.assertEqual(suppressed[0]["duplicate_winner_lecture_id"], RAY_WAITING)
        self.assertEqual(suppressed[0]["next_action"], "NOTHING_TO_DO")

    def test_each_row_carries_what_the_table_has_to_display(self):
        row = next(item for item in
                   self.get("operations-lectures", {"date": SEPTEMBER_18})
                   .json()["lectures"] if item["lecture_id"] == RAY_WAITING)
        for key in ("subject", "session_date", "bucket", "next_action",
                    "next_executable_action", "blocking_stage", "stages",
                    "attendance_coverage_status",
                    "attendance_source_authoritative", "reason_codes"):
            self.assertIn(key, row, key)


# --- live: one lecture --------------------------------------------------------------------

@requires_platform
class LectureDetailTests(_Authenticated):

    def detail(self, lecture_id=RAY_WAITING):
        return self.get("operations-lecture", lecture_id=lecture_id).json()

    def test_all_fifteen_stages_are_returned_in_the_declared_order(self):
        body = self.detail()
        self.assertEqual(list(body["stage_order"]), list(STAGE_ORDER))
        self.assertEqual(set(body["stages"]), set(STAGE_ORDER))
        self.assertEqual(len(body["stages"]), 15)

    def test_a_waiting_lecture_is_reported_as_waiting_and_never_as_failed(self):
        """
        The distinction the whole console rests on. A lecture waiting on an
        external source is not broken, and showing it as failed would send an
        operator chasing a problem that does not exist.
        """
        body = self.detail()
        self.assertEqual(body["stages"]["ATTENDANCE"]["state"], "WAITING")
        self.assertEqual(body["next_executable_action"],
                         "WAIT_FOR_ATTENDANCE_SOURCE")
        self.assertNotIn("FAILED",
                         {item["state"] for item in body["stages"].values()})

    def test_the_platform_states_what_may_be_done_rather_than_the_ui_guessing(self):
        body = self.detail()
        self.assertFalse(body["retry_eligibility"]["retry_eligible"])
        self.assertEqual(body["retry_eligibility"]["retry_reason"],
                         "WAITING_ON_EXTERNAL_SOURCE")
        self.assertFalse(
            body["force_reprocess_eligibility"]["available_to_scheduler"])

    def test_the_versions_travel_with_the_lecture(self):
        versions = self.detail()["versions"]
        self.assertEqual(versions["perfect_eligibility_version"],
                         "kbc_perfect_v2_attendance_required")
        self.assertIn("evidence_policy_version", versions)
        self.assertIn("duplicate_resolution_version", versions)

    def test_a_suppressed_duplicate_is_still_inspectable_by_id(self):
        """Excluded from the table, but an operator can still audit it."""
        body = self.detail(RAY_SUPPRESSED)
        self.assertEqual(body["next_executable_action"], "NOTHING_TO_DO")
        self.assertEqual({item["state"] for item in body["stages"].values()},
                         {"NOT_APPLICABLE"})
        self.assertEqual(body["stages"]["DISCOVERY"]["reason"],
                         "DUPLICATE_EVENT_SUPPRESSED")


# --- live: the working lists ----------------------------------------------------------------

@requires_platform
class WorkingListTests(_Authenticated):

    def test_pending_attendance_lists_only_non_authoritative_sources(self):
        body = self.get("operations-pending-attendance",
                        {"date": SEPTEMBER_18}).json()
        self.assertIn(RAY_WAITING, {row["lecture_id"] for row in body["lectures"]})
        for row in body["lectures"]:
            self.assertFalse(row["attendance_source_authoritative"])

    def test_the_day_currently_has_nothing_requiring_review(self):
        self.assertEqual(
            self.get("operations-review-required",
                     {"date": SEPTEMBER_18}).json()["count"], 0)

    def test_recent_runs_are_returned_and_bounded(self):
        body = self.get("operations-runs", {"limit": "5"}).json()
        self.assertLessEqual(body["count"], 5)
        self.assertIsInstance(body["runs"], list)

    def test_run_history_is_available_for_a_real_lecture(self):
        response = self.get("operations-lecture-history", lecture_id=RAY_WAITING)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["runs"], list)


# --- live: the read path cannot write ----------------------------------------------------------

@requires_platform
class ReadOnlyEnforcementTests(TestCase):

    def test_the_read_connection_is_refused_by_postgresql_if_it_writes(self):
        """
        Not a promise - a refusal. `reading()` opens the connection with
        SET TRANSACTION READ ONLY, so a view that somehow reached a writing
        code path is stopped by the database rather than by a convention.
        """
        from operations.platform import reading

        with reading() as connection:
            with self.assertRaises(Exception) as caught:
                connection.execute(
                    "UPDATE public.lecture_sessions SET subject = subject")
            self.assertIn("read-only", str(caught.exception).lower())
