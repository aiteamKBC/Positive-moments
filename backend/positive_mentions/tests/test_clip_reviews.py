"""
Human review of positive clips, and the detail response around it.

A review is stored in `positive_clip_reviews`, keyed by session + cues. These
tests pin that a decision never touches the n8n-written `positive_clips`, that
only a named reviewer may decide, and that the existing recording and trimmed
media links survive the new fields.
"""
import copy
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions.models import ClipReview, DoctorSession, PositiveClipAsset
from positive_mentions.tests.test_evidence import legacy_clip, verified_clip
from positive_mentions.utils import encode_session_id

REVIEWER_EMAIL = "reviewer@kentbusinesscollege.com"


@override_settings(
    DASHBOARD_USERNAME="shared-dashboard",
    DASHBOARD_PASSWORD="correct-horse-battery-staple",
    POSITIVE_CLIP_REVIEWERS=[REVIEWER_EMAIL],
)
class ClipReviewApiTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DoctorSession._meta.managed = True
        PositiveClipAsset._meta.managed = True
        with connection.schema_editor() as editor:
            editor.create_model(DoctorSession)
            editor.create_model(PositiveClipAsset)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(PositiveClipAsset)
            editor.delete_model(DoctorSession)
        DoctorSession._meta.managed = False
        PositiveClipAsset._meta.managed = False
        super().tearDownClass()

    def setUp(self):
        self.clips = [legacy_clip(), verified_clip()]
        self.lecture = DoctorSession.objects.create(
            session_id="session-review", date=datetime(2026, 10, 1, tzinfo=timezone.utc),
            subject="Ofsted Case Studies", trainer="Dr Example",
            positive_clips=copy.deepcopy(self.clips), positive_clips_count=2,
            clips_status="completed", clips_analysis_completeness="positive_clips_v5_final",
            has_positive_clips=True, recording_url="https://tenant.sharepoint.com/sites/kbc/r.mp4",
            recording_link_status="available",
        )
        self.key = encode_session_id(self.lecture.session_id)
        users = get_user_model().objects
        self.reviewer = users.create_user("reviewer", email=REVIEWER_EMAIL,
                                          first_name="Rae", last_name="Viewer")
        self.viewer = users.create_user("viewer", email="viewer@kentbusinesscollege.com")
        self.staff = users.create_user("staff", email="staff@kentbusinesscollege.com", is_staff=True)

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get_or_create(user=user)[0].key}")
        return client

    def detail(self, user):
        response = self.client_for(user).get(f"/api/positive-mentions/lectures/{self.key}/")
        self.assertEqual(response.status_code, 200)
        return response.data

    def review(self, user, **body):
        body = {"start_cue": 200, "end_cue": 210, "status": "approved", **body}
        return self.client_for(user).put(
            f"/api/positive-mentions/lectures/{self.key}/clip-reviews/", body, format="json")

    # --- persistence ----------------------------------------------------------

    def test_an_unreviewed_clip_is_pending(self):
        data = self.detail(self.reviewer)
        self.assertEqual([clip["review"]["status"] for clip in data["clips"]], ["pending", "pending"])
        self.assertEqual(data["review_summary"], {"pending": 2, "approved": 0, "rejected": 0})

    def test_a_reviewer_approval_is_stored_separately_with_identity_and_time(self):
        response = self.review(self.reviewer, note="Good for the newsletter.")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "approved")
        self.assertEqual(response.data["reviewer"], "Rae Viewer")
        row = ClipReview.objects.get()
        self.assertEqual((row.session_id, row.start_cue, row.end_cue),
                         ("session-review", 200, 210))
        self.assertEqual(row.reviewer, self.reviewer)
        self.assertIsNotNone(row.reviewed_at)

        detail = self.detail(self.viewer)
        by_cue = {clip["start_cue"]: clip for clip in detail["clips"]}
        self.assertEqual(by_cue[200]["review"]["status"], "approved")
        self.assertEqual(by_cue[100]["review"]["status"], "pending")

    def test_reviewing_never_writes_the_stored_ai_evidence(self):
        self.review(self.reviewer, status="rejected")
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.positive_clips, self.clips)

    def test_a_decision_can_be_changed_and_returned_to_pending(self):
        self.review(self.reviewer, status="rejected")
        self.review(self.reviewer, status="pending")
        self.assertEqual(ClipReview.objects.get().status, "pending")
        self.assertEqual(ClipReview.objects.count(), 1)

    def test_human_approval_is_independent_of_the_ai_fields(self):
        self.review(self.reviewer, status="rejected")
        clip = next(clip for clip in self.detail(self.viewer)["clips"] if clip["start_cue"] == 200)
        self.assertEqual(clip["review"]["status"], "rejected")
        self.assertEqual(clip["semantic_status"], "accepted")
        self.assertTrue(clip["social_media_ready"])

    # --- permissions -------------------------------------------------------------

    def test_a_signed_in_user_who_is_not_a_reviewer_is_refused(self):
        response = self.review(self.viewer)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ClipReview.objects.exists())
        self.assertFalse(self.detail(self.viewer)["can_review"])

    def test_the_shared_dashboard_account_cannot_review(self):
        client = APIClient()
        token = client.post("/api/auth/login/", {"username": "shared-dashboard",
                                                 "password": "correct-horse-battery-staple"},
                            format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        response = client.put(f"/api/positive-mentions/lectures/{self.key}/clip-reviews/",
                              {"start_cue": 200, "end_cue": 210, "status": "approved"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_staff_and_listed_reviewers_may_review(self):
        self.assertTrue(self.detail(self.reviewer)["can_review"])
        self.assertTrue(self.detail(self.staff)["can_review"])
        self.assertEqual(self.review(self.staff).status_code, 200)

    def test_anonymous_requests_are_refused(self):
        response = APIClient().put(f"/api/positive-mentions/lectures/{self.key}/clip-reviews/",
                                   {"start_cue": 200, "end_cue": 210, "status": "approved"}, format="json")
        self.assertEqual(response.status_code, 401)

    # --- validation --------------------------------------------------------------

    def test_an_unknown_status_is_refused(self):
        self.assertEqual(self.review(self.reviewer, status="published").status_code, 400)

    def test_a_clip_that_is_not_in_this_lecture_is_refused(self):
        self.assertEqual(self.review(self.reviewer, start_cue=1, end_cue=2).status_code, 404)
        self.assertFalse(ClipReview.objects.exists())

    def test_non_numeric_cues_are_refused(self):
        self.assertEqual(self.review(self.reviewer, start_cue="200").status_code, 400)

    # --- the detail response -------------------------------------------------

    def test_the_detail_carries_the_new_metadata_and_totals(self):
        data = self.detail(self.viewer)
        ofsted = next(clip for clip in data["clips"] if clip["start_cue"] == 200)
        self.assertEqual(ofsted["final_evidence_type"], "OFSTED_CASE_STUDY")
        self.assertEqual(ofsted["selector_evidence_type"], "POSITIVE_MOMENT")
        self.assertEqual(ofsted["social_formats"], ["reel", "carousel"])
        self.assertEqual(ofsted["carousel_slide_outline"][0], "The problem")
        self.assertEqual(data["evidence_summary"]["ofsted_case_studies"], 1)
        self.assertEqual(data["evidence_summary"]["positive_moments"], 1)
        self.assertEqual(data["evidence_summary"]["social_candidates"], 1)

    def test_recording_and_trimmed_media_links_still_work(self):
        now = datetime.now(timezone.utc)
        PositiveClipAsset.objects.create(
            clip_key="session-review:200:210", session_id="session-review", clip_index=2,
            source_start="00:20:00.000", source_end="00:21:05.000",
            trim_start_seconds="1200.000", trim_end_seconds="1265.000", duration_seconds="65.000",
            clip_filename="clip.mp4", clip_url="https://tenant.sharepoint.com/clip.mp4",
            trim_status="completed", created_at=now, updated_at=now)
        data = self.detail(self.viewer)
        self.assertEqual(data["recording_url"], "https://tenant.sharepoint.com/sites/kbc/r.mp4")
        ofsted = next(clip for clip in data["clips"] if clip["start_cue"] == 200)
        self.assertEqual(ofsted["clip_asset"]["url"], "https://tenant.sharepoint.com/clip.mp4")
        watch = self.client_for(self.viewer).get(
            f"/api/positive-mentions/lectures/{self.key}/clips/0/watch/")
        self.assertEqual(watch.status_code, 200)
        self.assertIn("stream.aspx", watch.data["url"])
