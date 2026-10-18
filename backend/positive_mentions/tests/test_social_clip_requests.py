"""
Create Clip for Social Media reels through the existing clip-on-demand webhook.

Every webhook call is mocked: no test reaches n8n. The webhook receives only
{session_id, opportunity_id}; Django refuses what n8n would refuse, never asks
twice for a clip in flight or already produced, and reads the result n8n saves
in opportunity_data.social_clip. Positive Moments keeps its own payload.
"""
import copy
import json
import urllib.error
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions import social_clip_requests, views
from positive_mentions.models import DoctorSession, PositiveClipAsset, SocialMediaOpportunity
from positive_mentions.utils import encode_session_id

MODELS = (DoctorSession, PositiveClipAsset, SocialMediaOpportunity)
WEBHOOK = "https://n8n.example/webhook/qa-clip-on-demand"
CLIP_URL = "https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/EabcSocialClip"
ANALYZED = datetime(2026, 10, 18, 11, 5, tzinfo=timezone.utc)


class SocialClipFixtures(TransactionTestCase):
    """Tables, a lecture with one reel, a signed-in client and a mocked webhook. No tests."""
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for model in MODELS:
            model._meta.managed = True
        with connection.schema_editor() as editor:
            for model in MODELS:
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(MODELS):
                editor.delete_model(model)
        for model in MODELS:
            model._meta.managed = False
        super().tearDownClass()

    def setUp(self):
        self.next_id = 13
        self.lecture = self.make_lecture("session-marketing")
        self.reel = self.opportunity()
        user = get_user_model().objects.create_user("social-team", email="social@example.invalid")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")

    def make_lecture(self, session_id, **overrides):
        values = {
            "session_id": session_id, "date": date(2026, 10, 13), "subject": "AI In Marketing",
            "trainer": "Sharon Shields", "clips_analysis_completeness": "positive_clips_v5_final",
            "positive_clips": [{"start": "00:05:50.000", "end": "00:06:00.000", "start_cue": 350,
                                "end_cue": 360, "positive_quote": "A positive moment."}],
            "recording_url": "https://tenant.sharepoint.com/sites/kbc/Recordings/lecture.mp4",
            "recording_drive_id": "b!source-drive", "recording_item_id": "SOURCE-ITEM",
        }
        values.update(overrides)
        return DoctorSession.objects.create(**values)

    def opportunity(self, *, start="00:46:49.768", end="00:47:10.568", start_cue=270, end_cue=272,
                    formats=("reel", "carousel"), status="accepted", segments=None, lecture=None, data=None):
        lecture = lecture or self.lecture
        segments = [{"start_cue": start_cue, "end_cue": end_cue}] if segments is None else segments
        row = SocialMediaOpportunity.objects.create(
            opportunity_id=self.next_id, session_id=lecture.session_id,
            opportunity_key=f"{start_cue}:{end_cue}:{self.next_id}", start_cue=start_cue, end_cue=end_cue,
            start_time=start, end_time=end, duration_seconds=Decimal("20.8"), speaker="Sharon Shields",
            quote="A quote.", content_angle="An angle", why_selected="A reason.",
            primary_format=formats[0], formats=list(formats), verification_status=status,
            verification_reason="Fine.", discovery_source="independent_social",
            opportunity_data={"reel_segments": segments, "reel_edit_mode": "contiguous", **(data or {})},
            analyzed_at=ANALYZED, updated_at=ANALYZED,
        )
        self.next_id += 1
        return row

    @contextmanager
    def webhook(self, *, reply_status=200, error=None):
        sent = []

        def urlopen(req, timeout):
            sent.append({"url": req.full_url, "method": req.get_method(),
                         "content_type": req.headers.get("Content-type"), "body": json.loads(req.data)})
            if error:
                raise error
            response = mock.MagicMock(status=reply_status)
            response.__enter__.return_value = response
            return response

        with mock.patch.object(views.urllib.request, "urlopen", side_effect=urlopen):
            yield sent

    def request(self, opportunity=None, lecture=None, client=None):
        opportunity = opportunity or self.reel
        key = encode_session_id((lecture or self.lecture).session_id)
        return (client or self.client).post(
            f"/api/social-media/lectures/{key}/opportunities/{opportunity.opportunity_id}/clip-requests/",
            {}, format="json")

    def clips(self):
        response = self.client.get(f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/clips/")
        self.assertEqual(response.status_code, 200)
        return response.data["clips"]

    def stored(self, opportunity=None):
        return SocialMediaOpportunity.objects.get(pk=(opportunity or self.reel).pk).opportunity_data

    def save_result(self, opportunity=None, **result):
        """What n8n's Save Social Clip Link node writes."""
        row = SocialMediaOpportunity.objects.get(pk=(opportunity or self.reel).pk)
        row.opportunity_data = {**row.opportunity_data, "social_clip": {
            "clip_key": f"social:{row.session_id}:{row.opportunity_id}", "updated_at": "2026-10-18 12:00:00.5+00",
            **result}}
        row.save(update_fields=["opportunity_data"])



@override_settings(CLIP_ON_DEMAND_WEBHOOK_URL=WEBHOOK)
class SocialClipRequestTests(SocialClipFixtures):
    # --- the request ------------------------------------------------------------

    def test_one_click_sends_only_session_and_opportunity_to_the_existing_webhook(self):
        with self.webhook() as sent:
            response = self.request()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["status"], "requested")
        self.assertEqual(response.data["clip"]["state"], "requested")
        self.assertEqual(sent, [{"url": WEBHOOK, "method": "POST", "content_type": "application/json",
                                 "body": {"session_id": "session-marketing", "opportunity_id": 13}}])
        marker = self.stored()["social_clip_request"]
        self.assertEqual(marker["requested_by"], "social-team")
        self.assertNotIn("social_clip", self.stored())

    def test_the_request_records_only_the_marker(self):
        before = copy.deepcopy(self.stored())
        other = self.opportunity(start_cue=300, end_cue=302)
        other_before = copy.deepcopy(self.stored(other))
        with self.webhook():
            self.request()
        after = self.stored()
        after.pop("social_clip_request")
        self.assertEqual(after, before)
        self.assertEqual(self.stored(other), other_before)
        self.assertEqual(SocialMediaOpportunity.objects.get(pk=self.reel.pk).updated_at, ANALYZED)

    def test_any_2xx_is_accepted_and_anything_else_is_a_failure(self):
        for reply in (200, 201, 204):
            self.save_result(trim_status="failed", trim_error="reset", updated_at="2030-01-01 00:00:00+00")
            with self.webhook(reply_status=reply):
                self.assertEqual(self.request().status_code, 202, reply)

    # --- validation (n8n's own rules) ---------------------------------------------

    def assert_refused(self, opportunity, status, code, lecture=None):
        with self.webhook() as sent:
            response = self.request(opportunity, lecture=lecture)
        self.assertEqual((response.status_code, response.data["code"]), (status, code), response.data)
        self.assertEqual(sent, [])
        self.assertNotIn("social_clip_request", self.stored(opportunity))

    def test_invalid_timestamps_are_refused(self):
        for start, end in (("21:25", "00:22:05.128"), ("00:21:25.2888", "00:22:05.128"),
                           ("00:22:05.128", "00:21:25.288"), (None, "00:22:05.128")):
            self.assert_refused(self.opportunity(start=start, end=end), 422, "invalid_timestamps")

    def test_duration_must_be_5_to_180_seconds(self):
        self.assert_refused(self.opportunity(start="00:10:00.000", end="00:10:04.900"), 422, "invalid_duration")
        self.assert_refused(self.opportunity(start="00:10:00.000", end="00:13:00.001"), 422, "invalid_duration")
        with self.webhook():
            self.assertEqual(self.request(self.opportunity(start="00:10:00.000", end="00:10:05.000")).status_code, 202)
            self.assertEqual(self.request(self.opportunity(start="00:10:00.000", end="00:13:00.000")).status_code, 202)

    def test_segments_must_be_one_interval_matching_the_saved_cues(self):
        self.assert_refused(self.opportunity(start_cue=80, end_cue=90, segments=[
            {"start_cue": 80, "end_cue": 82}, {"start_cue": 88, "end_cue": 90}]), 422, "unsupported_segments")
        self.assert_refused(self.opportunity(start_cue=80, end_cue=90, segments=[{"start_cue": 81, "end_cue": 90}]),
                            422, "unsupported_segments")
        with self.webhook():
            self.assertEqual(self.request(self.opportunity(segments=[])).status_code, 202)

    def test_non_reels_cannot_request_clips(self):
        carousel = self.opportunity(formats=("carousel", "educational_post"))
        self.assert_refused(carousel, 409, "not_a_reel")
        self.assertNotIn(str(carousel.opportunity_id), self.clips())

    def test_a_recording_is_required(self):
        DoctorSession.objects.filter(pk=self.lecture.pk).update(recording_item_id=" ")
        self.assert_refused(self.reel, 422, "recording_missing")

    def test_the_opportunity_must_be_accepted_and_belong_to_the_lecture(self):
        self.assert_refused(self.opportunity(status="rejected"), 404, "not_found")
        elsewhere = self.make_lecture("session-other")
        self.assert_refused(self.reel, 404, "not_found", lecture=elsewhere)
        with self.webhook() as sent:
            response = self.client.post(
                f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/opportunities/999/clip-requests/")
        self.assertEqual((response.status_code, sent), (404, []))

    def test_signed_in_users_only_and_post_only(self):
        with self.webhook() as sent:
            self.assertEqual(self.request(client=APIClient()).status_code, 401)
            self.assertEqual(APIClient().get(
                f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/clips/").status_code, 401)
            path = (f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}"
                    f"/opportunities/{self.reel.opportunity_id}/clip-requests/")
            for method in (self.client.get, self.client.put, self.client.delete):
                self.assertEqual(method(path).status_code, 405)
        self.assertEqual(sent, [])

    # --- duplicates -----------------------------------------------------------------

    def test_a_clip_in_flight_is_not_requested_twice(self):
        with self.webhook() as sent:
            self.request()
            again = self.request()
        self.assertEqual((again.status_code, again.data["code"]), (409, "clip_in_progress"))
        self.assertEqual(again.data["clip"]["state"], "requested")
        self.assertEqual(len(sent), 1)

    def test_a_produced_clip_is_never_requested_again(self):
        self.save_result(trim_status="completed", clip_url=CLIP_URL, clip_filename="x_social-13_cue-270-272.mp4")
        with self.webhook() as sent:
            response = self.request()
        self.assertEqual((response.status_code, response.data["code"]), (409, "clip_already_produced"))
        self.assertEqual(response.data["clip"]["clip_url"], CLIP_URL)
        self.assertEqual(sent, [])

    def test_a_request_with_no_result_after_the_window_may_be_repeated(self):
        old = (datetime.now(timezone.utc) - social_clip_requests.REQUEST_WINDOW - timedelta(minutes=1)).isoformat()
        row = SocialMediaOpportunity.objects.get(pk=self.reel.pk)
        row.opportunity_data = {**row.opportunity_data, "social_clip_request": {"requested_at": old}}
        row.save(update_fields=["opportunity_data"])
        self.assertEqual(self.clips()["13"]["state"], "stale")
        with self.webhook() as sent:
            self.assertEqual(self.request().status_code, 202)
        self.assertEqual(len(sent), 1)

    # --- failures ---------------------------------------------------------------------

    def test_a_webhook_failure_is_a_clear_error_and_leaves_no_request_behind(self):
        for error in (urllib.error.HTTPError(WEBHOOK, 500, "err", {}, None), urllib.error.URLError("down")):
            with self.webhook(error=error):
                response = self.request()
            self.assertEqual((response.status_code, response.data["code"]), (502, "clip_request_failed"))
            self.assertNotIn("social_clip_request", self.stored())
            self.assertEqual(self.clips()["13"]["state"], "available")
        with self.webhook(reply_status=304):
            self.assertEqual(self.request().status_code, 502)

    def test_clearing_a_failed_request_never_removes_a_newer_one(self):
        row = SocialMediaOpportunity.objects.get(pk=self.reel.pk)
        row.opportunity_data = {**row.opportunity_data, "social_clip_request": {"requested_at": "newer"}}
        row.save(update_fields=["opportunity_data"])
        with connection.cursor() as cursor:
            cursor.execute(social_clip_requests.CLEAR_REQUESTED, {"opportunity_id": 13, "requested_at": "older"})
        self.assertEqual(self.stored()["social_clip_request"], {"requested_at": "newer"})

    def test_a_failed_render_shows_its_error_and_may_be_retried(self):
        with self.webhook():
            self.request()
        self.save_result(trim_status="failed", trim_error="Creatomate render failed: timeout\ndetail",
                         updated_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat())
        clip = self.clips()["13"]
        self.assertEqual((clip["state"], clip["error"], clip["can_request"]),
                         ("failed", "Creatomate render failed: timeout", True))
        with self.webhook() as sent:
            self.assertEqual(self.request().status_code, 202)
        self.assertEqual(len(sent), 1)

    # --- result retrieval -------------------------------------------------------------

    def test_view_clip_only_for_a_completed_sharepoint_link(self):
        self.save_result(trim_status="completed", clip_url=CLIP_URL, clip_filename="x_social-13_cue-270-272.mp4")
        clip = self.clips()["13"]
        self.assertEqual((clip["state"], clip["clip_url"], clip["clip_filename"]),
                         ("ready", CLIP_URL, "x_social-13_cue-270-272.mp4"))
        for result in ({"trim_status": "completed", "clip_url": "https://evil.example.com/x.mp4"},
                       {"trim_status": "completed", "clip_url": None},
                       {"trim_status": "uploaded_needs_link", "clip_url": CLIP_URL}):
            self.save_result(**result)
            clip = self.clips()["13"]
            self.assertNotEqual(clip["state"], "ready", result)
            self.assertIsNone(clip["clip_url"], result)

    def test_the_lecture_page_carries_the_clip_state_but_not_the_bookkeeping(self):
        self.save_result(trim_status="completed", clip_url=CLIP_URL, clip_item_id="ITEM", clip_drive_id="DRIVE")
        carousel = self.opportunity(formats=("carousel",))
        response = self.client.get(f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/")
        items = {item["opportunity_id"]: item for item in response.data["opportunities"]}
        self.assertEqual(items[13]["clip"]["state"], "ready")
        self.assertIsNone(items[carousel.opportunity_id]["clip"])
        self.assertNotIn("social_clip", items[13]["opportunity_data"])
        self.assertNotIn("ITEM", json.dumps(items[13], default=str))
        self.assertEqual(items[13]["opportunity_data"]["reel_edit_mode"], "contiguous")

    def test_reading_never_calls_the_webhook(self):
        with self.webhook() as sent:
            self.clips()
            self.client.get(f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/")
            self.client.get("/api/social-media/lectures/")
        self.assertEqual(sent, [])

    # --- Positive Moments is unchanged --------------------------------------------------

    def test_positive_moments_still_sends_its_own_three_fields(self):
        key = encode_session_id(self.lecture.session_id)
        with self.webhook() as sent:
            response = self.client.post(f"/api/positive-mentions/lectures/{key}/clip-requests/",
                                        {"start_cue": 350, "end_cue": 360}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(sent, [{"url": WEBHOOK, "method": "POST", "content_type": "application/json",
                                 "body": {"session_id": "session-marketing", "start_cue": 350, "end_cue": 360}}])
        self.assertNotIn("social_clip_request", self.stored())


EVIDENCE_URL = "https://kentbusinesscollege.sharepoint.com/:v:/s/kbc/EabcEvidenceClip"


@override_settings(CLIP_ON_DEMAND_WEBHOOK_URL=WEBHOOK)
class SocialEvidenceClipTests(SocialClipFixtures):
    """Which video a Social Media card can offer, read without writing anything."""

    def asset(self, opportunity=None, *, start=None, end=None, cues=None, status="completed", url=EVIDENCE_URL):
        opportunity = opportunity or self.reel
        start_cue, end_cue = cues or (opportunity.start_cue, opportunity.end_cue)
        source_start, source_end = start or opportunity.start_time, end or opportunity.end_time
        return PositiveClipAsset.objects.create(
            clip_key=f"{opportunity.session_id}:{start_cue}:{end_cue}", session_id=opportunity.session_id,
            clip_index=3, source_start=source_start, source_end=source_end,
            trim_start_seconds=Decimal("2749.768"), trim_end_seconds=Decimal("2890.568"),
            duration_seconds=Decimal("140.800"), clip_filename="20261013_AI-In-Marketing_x_clip-03_cue-270-272.mp4",
            clip_url=url, trim_status=status, created_at=ANALYZED, updated_at=ANALYZED)

    def card(self, opportunity=None):
        opportunity = opportunity or self.reel
        response = self.client.get(f"/api/social-media/lectures/{encode_session_id(self.lecture.session_id)}/")
        self.assertEqual(response.status_code, 200)
        return next(item for item in response.data["opportunities"] if item["opportunity_id"] == opportunity.opportunity_id)

    def test_own_exact_clip_only(self):
        self.save_result(trim_status="completed", clip_url=CLIP_URL, clip_filename="x_social-13_cue-270-272.mp4")
        item = self.card()
        self.assertEqual((item["clip"]["state"], item["clip"]["clip_url"]), ("ready", CLIP_URL))
        self.assertIsNone(item["evidence_clip"])

    def test_existing_evidence_clip_only(self):
        self.asset()
        item = self.card()
        self.assertEqual(item["evidence_clip"]["url"], EVIDENCE_URL)
        # 00:46:49.768 = 2809.768 s; trim 2749.768 .. 2890.568 -> 60 s either side.
        self.assertEqual((item["evidence_clip"]["context_before_seconds"],
                          item["evidence_clip"]["context_after_seconds"]), (60.0, 60.0))
        self.assertEqual(item["clip"]["state"], "available")
        self.assertIsNone(item["clip"]["clip_url"])

    def test_both_clips_stay_distinct(self):
        self.asset()
        self.save_result(trim_status="completed", clip_url=CLIP_URL)
        item = self.card()
        self.assertEqual(item["clip"]["clip_url"], CLIP_URL)
        self.assertEqual(item["evidence_clip"]["url"], EVIDENCE_URL)
        self.assertNotEqual(item["clip"]["clip_url"], item["evidence_clip"]["url"])

    def test_no_clip_at_all(self):
        item = self.card()
        self.assertIsNone(item["evidence_clip"])
        self.assertEqual(item["clip"]["state"], "available")

    def test_evidence_clip_needs_both_the_cue_key_and_the_exact_times(self):
        # A re-analysis kept the cues but moved the times, or the reverse.
        moved = self.opportunity(start_cue=300, end_cue=302)
        self.asset(moved, start="00:46:50.000")
        renumbered = self.opportunity(start_cue=310, end_cue=312, start="00:50:00.000", end="00:50:20.000")
        self.asset(renumbered, cues=(311, 312))
        self.assertIsNone(self.card(moved)["evidence_clip"])
        self.assertIsNone(self.card(renumbered)["evidence_clip"])

    def test_unfinished_or_non_sharepoint_evidence_clips_are_ignored(self):
        failed = self.opportunity(start_cue=400, end_cue=402)
        self.asset(failed, status="failed")
        elsewhere = self.opportunity(start_cue=410, end_cue=412)
        self.asset(elsewhere, url="https://evil.example.com/clip.mp4")
        self.assertIsNone(self.card(failed)["evidence_clip"])
        self.assertIsNone(self.card(elsewhere)["evidence_clip"])

    def test_non_reels_can_show_an_existing_clip_but_never_request_one(self):
        quote = self.opportunity(start_cue=420, end_cue=422, formats=("quote_post",))
        self.asset(quote)
        item = self.card(quote)
        self.assertEqual(item["evidence_clip"]["url"], EVIDENCE_URL)
        self.assertIsNone(item["clip"])

    def test_reading_never_writes_a_social_clip_from_an_evidence_clip(self):
        self.asset()
        before = copy.deepcopy(self.stored())
        self.card()
        self.clips()
        self.assertEqual(self.stored(), before)
        self.assertNotIn("social_clip", self.stored())
        self.assertEqual(PositiveClipAsset.objects.count(), 1)

    def test_the_create_clip_api_is_unchanged_by_an_evidence_clip(self):
        self.asset()
        with self.webhook() as sent:
            response = self.request()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(sent[0]["body"], {"session_id": "session-marketing", "opportunity_id": 13})
