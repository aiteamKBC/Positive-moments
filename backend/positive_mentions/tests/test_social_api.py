"""
The read-only Social Media API.

Lectures come from `qa_social_media_opportunities` (accepted rows only), the
list and detail counts agree, the saved payload is returned whole and nothing
is ever written.
"""
import base64
import copy
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions.models import DoctorSession, PositiveClipAsset, SocialMediaOpportunity
from positive_mentions.utils import encode_session_id

ANALYZED = datetime(2026, 10, 18, 11, 5, tzinfo=timezone.utc)
RECORDING = "https://tenant.sharepoint.com/sites/kbc/Recordings/lecture.mp4"
# The real lecture's accepted split by primary format: 53 in total.
MARKETING_SPLIT = {"reel": 24, "carousel": 19, "quote_post": 7, "educational_post": 3}


def payload(start_cue, end_cue, primary, **extra):
    return {
        "start": "00:21:25.288", "end": "00:22:05.128", "start_cue": start_cue, "end_cue": end_cue,
        "quote": "Data quality can turn AI into a confident generator of poor decisions.",
        "speaker": "Sharon Shields", "formats": [primary, "educational_post"],
        "primary_format": primary, "content_angle": "Why data veracity matters",
        "why_selected": "A hooky line with an actionable checklist.",
        "discovery_source": "independent_social", "also_in_evidence": False,
        "reel_edit_mode": "contiguous" if primary == "reel" else "not_applicable",
        "reel_segments": [{"start_cue": start_cue, "end_cue": end_cue}] if primary == "reel" else [],
        "carousel_slide_outline": ["Slide 1: Accurate", "Slide 2: Complete"] if primary == "carousel" else [],
        "verification_status": "accepted", "verification_reason": "Self-contained and clear.",
        **extra,
    }


class SocialMediaApiTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for model in (DoctorSession, PositiveClipAsset, SocialMediaOpportunity):
            model._meta.managed = True
        with connection.schema_editor() as editor:
            for model in (DoctorSession, PositiveClipAsset, SocialMediaOpportunity):
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in (SocialMediaOpportunity, PositiveClipAsset, DoctorSession):
                editor.delete_model(model)
        for model in (DoctorSession, PositiveClipAsset, SocialMediaOpportunity):
            model._meta.managed = False
        super().tearDownClass()

    def setUp(self):
        self.next_id = 1
        self.marketing = self.lecture(
            "session-marketing", date(2026, 10, 13), "AI In Marketing", "Sharon Shields",
            recording_url=RECORDING,
        )
        cue = 0
        for primary, total in MARKETING_SPLIT.items():
            for _ in range(total):
                cue += 10
                self.opportunity(self.marketing, cue, cue + 2, primary)
        # Never shown: not accepted.
        self.opportunity(self.marketing, 900, 901, "reel", status="rejected")
        self.opportunity(self.marketing, 910, 911, "carousel", status="pending")

        self.older = self.lecture("session-older", date(2026, 9, 1), "Data Ethics", "Dr Example")
        self.opportunity(self.older, 5, 6, "quote_post")
        self.rejected_only = self.lecture("session-rejected", date(2026, 10, 1), "Branding", "Dr Example")
        self.opportunity(self.rejected_only, 5, 6, "reel", status="rejected")
        self.without = self.lecture("session-none", date(2026, 10, 2), "Finance", "Dr Example")

        user = get_user_model().objects.create_user("viewer", email="viewer@example.invalid")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")

    def lecture(self, session_id, day, subject, trainer, **overrides):
        values = {
            "session_id": session_id, "date": day, "subject": subject, "trainer": trainer,
            # Deliberately NOT the Positive Moments V5 marker: the social page
            # must not depend on it.
            "clips_analysis_completeness": None, "positive_clips": [], "recording_url": "",
            "recording_filename": "lecture.mp4", "recording_link_status": "available",
        }
        values.update(overrides)
        return DoctorSession.objects.create(**values)

    def opportunity(self, lecture, start_cue, end_cue, primary, status="accepted", **data):
        stored = payload(start_cue, end_cue, primary, **data)
        row = SocialMediaOpportunity.objects.create(
            opportunity_id=self.next_id, session_id=lecture.session_id,
            opportunity_key=f"{start_cue}:{end_cue}", start_cue=start_cue, end_cue=end_cue,
            start_time=stored["start"], end_time=stored["end"], duration_seconds=Decimal("39.84"),
            speaker=stored["speaker"], quote=stored["quote"], content_angle=stored["content_angle"],
            why_selected=stored["why_selected"], primary_format=primary, formats=stored["formats"],
            verification_status=status, verification_reason=stored["verification_reason"],
            discovery_source=stored["discovery_source"], evidence_type=None,
            opportunity_data=stored, analyzed_at=ANALYZED, updated_at=ANALYZED,
        )
        self.next_id += 1
        return row

    def get(self, path, expected=200, **params):
        response = self.client.get(f"/api/social-media/{path}", params)
        self.assertEqual(response.status_code, expected, response.data)
        return response.data

    def detail(self, lecture, expected=200):
        return self.get(f"lectures/{encode_session_id(lecture.session_id)}/", expected)

    # --- list ---------------------------------------------------------------

    def test_the_list_holds_only_lectures_with_accepted_opportunities_newest_first(self):
        data = self.get("lectures/")
        self.assertEqual(data["count"], 2)
        self.assertEqual([row["subject"] for row in data["results"]], ["AI In Marketing", "Data Ethics"])

    def test_the_list_counts_each_accepted_opportunity_once_by_primary_format(self):
        row = self.get("lectures/")["results"][0]
        self.assertEqual(row["opportunity_count"], 53)
        self.assertEqual(row["format_counts"], {**MARKETING_SPLIT, "other": 0})
        self.assertEqual(row["date"], "2026-10-13")
        self.assertEqual(row["trainer"], "Sharon Shields")
        self.assertTrue(row["recording_available"])
        self.assertEqual(row["session_key"], encode_session_id("session-marketing"))

    def test_the_format_filter_keeps_lectures_with_that_primary_format(self):
        self.assertEqual([r["subject"] for r in self.get("lectures/", content_format="quote_post")["results"]],
                         ["AI In Marketing", "Data Ethics"])
        only = self.get("lectures/", content_format="reel")["results"]
        self.assertEqual([r["subject"] for r in only], ["AI In Marketing"])
        # Filtering selects lectures; it does not change their counts.
        self.assertEqual(only[0]["opportunity_count"], 53)

    def test_search_and_date_filters(self):
        self.assertEqual(self.get("lectures/", search="sharon")["count"], 1)
        self.assertEqual(self.get("lectures/", search="ethics")["count"], 1)
        self.assertEqual(self.get("lectures/", date_from="2026-10-13", date_to="2026-10-13")["count"], 1)
        self.assertEqual(self.get("lectures/", date_to="2026-09-30")["results"][0]["subject"], "Data Ethics")

    def test_invalid_filters_are_refused_not_ignored(self):
        self.assertEqual(self.get("lectures/", 400, content_format="tiktok")["code"], "invalid_format")
        self.assertEqual(self.get("lectures/", 400, date_from="13/10/2026")["code"], "invalid_date")
        self.assertEqual(self.get("summary/", 400, content_format="tiktok")["code"], "invalid_format")

    def test_pages_are_stable_and_complete(self):
        for index in range(25):
            lecture = self.lecture(f"session-same-day-{index:02}", date(2026, 8, 1), "Same day", "Dr Example")
            self.opportunity(lecture, 1, 2, "reel")
        first = self.get("lectures/", page_size=20)
        second = self.get("lectures/", page=2, page_size=20)
        self.assertEqual(first["count"], 27)
        ids = [row["session_id"] for row in first["results"] + second["results"]]
        self.assertEqual(len(ids), 27)
        self.assertEqual(len(set(ids)), 27)

    def test_the_list_query_count_does_not_grow_with_the_page(self):
        for index in range(15):
            lecture = self.lecture(f"session-bulk-{index:02}", date(2026, 8, 1), "Bulk", "Dr Example")
            self.opportunity(lecture, 1, 2, "carousel")
        with CaptureQueriesContext(connection) as captured:
            data = self.get("lectures/")
        self.assertEqual(len(data["results"]), 17)
        # auth token, page count, page rows, grouped opportunity counts.
        self.assertLessEqual(len(captured), 4)

    def test_the_summary_totals_every_page(self):
        self.assertEqual(self.get("summary/"), {
            "lectures": 2, "opportunity_count": 54,
            "format_counts": {"reel": 24, "carousel": 19, "quote_post": 8, "educational_post": 3, "other": 0},
        })
        self.assertEqual(self.get("summary/", search="Sharon")["opportunity_count"], 53)

    # --- detail -------------------------------------------------------------

    def test_list_and_detail_agree_on_counts(self):
        row = self.get("lectures/")["results"][0]
        data = self.detail(self.marketing)
        self.assertEqual(data["opportunity_count"], row["opportunity_count"])
        self.assertEqual(data["format_counts"], row["format_counts"])
        self.assertEqual(len(data["opportunities"]), 53)
        primaries = [item["primary_format"] for item in data["opportunities"]]
        self.assertEqual({name: primaries.count(name) for name in MARKETING_SPLIT}, MARKETING_SPLIT)
        self.assertEqual({item["verification_status"] for item in data["opportunities"]}, {"accepted"})

    def test_the_detail_returns_the_saved_payload_whole(self):
        stored = self.opportunity(self.older, 40, 44, "carousel", original_evidence={"reason": "kept"},
                                  merged_ranges=["40:44"])
        item = next(o for o in self.detail(self.older)["opportunities"] if o["opportunity_id"] == stored.opportunity_id)
        self.assertEqual(item["opportunity_data"], stored.opportunity_data)
        for key in ("content_angle", "why_selected", "quote", "speaker", "start_time", "end_time",
                    "discovery_source", "verification_reason"):
            self.assertEqual(item[key], getattr(stored, key))
        self.assertEqual(item["formats"], ["carousel", "educational_post"])
        self.assertEqual(item["carousel_slide_outline"], ["Slide 1: Accurate", "Slide 2: Complete"])
        self.assertEqual(item["duration_seconds"], 39.84)

    def test_opportunities_are_in_lecture_order(self):
        cues = [item["start_cue"] for item in self.detail(self.marketing)["opportunities"]]
        self.assertEqual(cues, sorted(cues))

    def test_reel_segment_times_come_only_from_stored_timestamps(self):
        own = self.opportunity(self.older, 70, 72, "reel")
        split = self.opportunity(self.older, 80, 90, "reel", reel_segments=[
            {"start_cue": 80, "end_cue": 82}, {"start_cue": 88, "end_cue": 90}])
        items = {o["opportunity_id"]: o for o in self.detail(self.older)["opportunities"]}
        self.assertEqual(items[own.opportunity_id]["reel_segments"],
                         [{"start_cue": 70, "end_cue": 72, "start": "00:21:25.288", "end": "00:22:05.128"}])
        self.assertEqual(items[split.opportunity_id]["reel_segments"], [
            {"start_cue": 80, "end_cue": 82, "start": "00:21:25.288", "end": None},
            {"start_cue": 88, "end_cue": 90, "start": None, "end": "00:22:05.128"},
        ])

    def test_the_recording_opens_at_the_stored_start_time(self):
        data = self.detail(self.marketing)
        self.assertEqual(data["recording_url"], RECORDING)
        parts = urlsplit(data["opportunities"][0]["watch_url"])
        self.assertTrue(parts.path.endswith("/_layouts/15/stream.aspx"))
        nav = json.loads(base64.b64decode(parse_qs(parts.query)["nav"][0]))
        self.assertAlmostEqual(nav["playbackOptions"]["startTimeInSeconds"], 21 * 60 + 25.288)

    def test_no_recording_means_no_watch_link(self):
        self.assertIsNone(self.detail(self.older)["opportunities"][0]["watch_url"])
        self.assertFalse(self.detail(self.older)["recording_available"])

    def test_lectures_without_accepted_opportunities_are_not_found(self):
        self.assertEqual(self.detail(self.rejected_only, 404)["code"], "not_found")
        self.assertEqual(self.detail(self.without, 404)["code"], "not_found")
        self.assertEqual(self.get("lectures/not-base64!!/", 404)["code"], "not_found")

    # --- safety -------------------------------------------------------------

    def test_reading_never_writes(self):
        before = copy.deepcopy(list(SocialMediaOpportunity.objects.order_by("opportunity_id")
                                    .values_list("opportunity_data", "updated_at")))
        with CaptureQueriesContext(connection) as captured:
            self.get("summary/")
            self.get("lectures/")
            self.detail(self.marketing)
        statements = [query["sql"].lstrip().split()[0].upper() for query in captured]
        self.assertEqual(set(statements), {"SELECT"})
        after = list(SocialMediaOpportunity.objects.order_by("opportunity_id")
                     .values_list("opportunity_data", "updated_at"))
        self.assertEqual(after, before)

    def test_the_api_is_read_only_and_signed_in(self):
        path = f"/api/social-media/lectures/{encode_session_id(self.marketing.session_id)}/"
        for method in (self.client.post, self.client.put, self.client.patch, self.client.delete):
            self.assertEqual(method(path, {}, format="json").status_code, 405)
        self.assertEqual(APIClient().get("/api/social-media/lectures/").status_code, 401)

    def test_positive_moments_still_lists_only_v5_lectures(self):
        response = self.client.get("/api/positive-mentions/lectures/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
