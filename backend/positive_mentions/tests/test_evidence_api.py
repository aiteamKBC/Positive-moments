"""
The lecture detail API with verified-evidence clips.

Read-only display: the new fields and totals arrive with the lecture, and the
existing recording and trimmed-media links are unchanged.
"""
import copy
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions.models import DoctorSession, PositiveClipAsset
from positive_mentions.tests.test_evidence import legacy_clip, verified_clip
from positive_mentions.utils import encode_session_id


class EvidenceDetailApiTests(TransactionTestCase):
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
            session_id="session-evidence", date=datetime(2026, 10, 1, tzinfo=timezone.utc),
            subject="Ofsted Case Studies", trainer="Dr Example",
            positive_clips=copy.deepcopy(self.clips), positive_clips_count=2,
            clips_status="completed", clips_analysis_completeness="positive_clips_v5_final",
            has_positive_clips=True, recording_url="https://tenant.sharepoint.com/sites/kbc/r.mp4",
            recording_link_status="available",
        )
        self.key = encode_session_id(self.lecture.session_id)
        user = get_user_model().objects.create_user("viewer", email="viewer@example.invalid")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")

    def detail(self):
        response = self.client.get(f"/api/positive-mentions/lectures/{self.key}/")
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_the_detail_carries_the_new_metadata_and_totals(self):
        data = self.detail()
        ofsted = next(clip for clip in data["clips"] if clip["start_cue"] == 200)
        self.assertEqual(ofsted["final_evidence_type"], "OFSTED_CASE_STUDY")
        self.assertEqual(ofsted["selector_evidence_type"], "POSITIVE_MOMENT")
        self.assertEqual(ofsted["social_formats"], ["reel", "carousel"])
        self.assertEqual(ofsted["carousel_slide_outline"][0], "The problem")
        self.assertEqual(data["evidence_summary"]["ofsted_case_studies"], 1)
        self.assertEqual(data["evidence_summary"]["positive_moments"], 1)
        self.assertEqual(data["evidence_summary"]["social_candidates"], 1)
        self.assertNotIn("review", ofsted)
        self.assertNotIn("can_review", data)

    def test_reading_the_detail_never_writes_the_stored_clips(self):
        self.detail()
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.positive_clips, self.clips)

    def test_recording_and_trimmed_media_links_still_work(self):
        now = datetime.now(timezone.utc)
        PositiveClipAsset.objects.create(
            clip_key="session-evidence:200:210", session_id="session-evidence", clip_index=2,
            source_start="00:20:00.000", source_end="00:21:05.000",
            trim_start_seconds="1200.000", trim_end_seconds="1265.000", duration_seconds="65.000",
            clip_filename="clip.mp4", clip_url="https://tenant.sharepoint.com/clip.mp4",
            trim_status="completed", created_at=now, updated_at=now)
        data = self.detail()
        self.assertEqual(data["recording_url"], "https://tenant.sharepoint.com/sites/kbc/r.mp4")
        ofsted = next(clip for clip in data["clips"] if clip["start_cue"] == 200)
        self.assertEqual(ofsted["clip_asset"]["url"], "https://tenant.sharepoint.com/clip.mp4")
        watch = self.client.get(f"/api/positive-mentions/lectures/{self.key}/clips/0/watch/")
        self.assertEqual(watch.status_code, 200)
        self.assertIn("stream.aspx", watch.data["url"])

    def test_a_review_endpoint_does_not_exist(self):
        response = self.client.put(f"/api/positive-mentions/lectures/{self.key}/clip-reviews/",
                                   {"start_cue": 200, "end_cue": 210, "status": "approved"}, format="json")
        self.assertEqual(response.status_code, 404)
