import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from positive_mentions.models import DoctorSession, PositiveClipAsset
from positive_mentions.utils import encode_session_id


class PositiveMentionsApiTests(TransactionTestCase):
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
        self.user = get_user_model().objects.create_user(
            username="staff", password="correct-horse", is_staff=True
        )
        self.client = APIClient()
        response = self.client.post("/api/auth/login/", {
            "username": "staff", "password": "correct-horse",
        }, format="json")
        self.token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")

    def make_lecture(self, session_id, **overrides):
        values = {
            "date": datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
            "subject": "Communication Skills",
            "trainer": "Dr Example",
            "positive_clips": [],
            "positive_clips_count": 0,
            "clips_status": "completed",
            "clips_analyzed_at": datetime(2026, 7, 26, 14, 50, tzinfo=timezone.utc),
            "clips_analysis_completeness": "positive_clips_v5_final",
            "has_positive_clips": False,
            "recording_url": "",
            "recording_link_status": "missing",
        }
        values.update(overrides)
        return DoctorSession.objects.create(session_id=session_id, **values)

    def make_asset(self, clip_key, session_id, **overrides):
        now = datetime.now(timezone.utc)
        values = {
            "clip_index": 1,
            "source_start": "00:00:01.000",
            "source_end": "00:00:02.000",
            "trim_start_seconds": "1.000",
            "trim_end_seconds": "2.000",
            "duration_seconds": "1.000",
            "clip_filename": "positive-clip.mp4",
            "clip_url": "https://tenant.sharepoint.com/clip.mp4",
            "trim_status": "completed",
            "created_at": now,
            "updated_at": now,
        }
        values.update(overrides)
        return PositiveClipAsset.objects.create(
            clip_key=clip_key,
            session_id=session_id,
            **values,
        )

    def test_endpoints_require_authentication(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get("/api/positive-mentions/summary/").status_code, 401)
        self.assertEqual(anonymous.get("/api/positive-mentions/lectures/").status_code, 401)

    def test_login_rejects_bad_credentials_and_logout_revokes_token(self):
        bad = APIClient().post("/api/auth/login/", {
            "username": "staff", "password": "wrong",
        }, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_list_scope_uses_all_v5_rows_without_an_implicit_date_window(self):
        self.make_lecture("included")
        self.make_lecture(
            "wrong-version", clips_analysis_completeness="positive_clips_v4_final"
        )
        self.make_lecture(
            "too-early",
            clips_analyzed_at=datetime(2026, 7, 26, 14, 47, tzinfo=timezone.utc),
        )
        self.make_lecture(
            "at-exclusive-end",
            clips_analyzed_at=datetime(2026, 7, 26, 14, 58, tzinfo=timezone.utc),
        )
        self.make_lecture(
            "outside-old-test-window",
            clips_analyzed_at=datetime(2026, 7, 26, 14, 47, tzinfo=timezone.utc),
        )
        self.make_lecture(
            "outside-new-test-window",
            clips_analyzed_at=datetime(2026, 7, 26, 14, 58, tzinfo=timezone.utc),
        )
        response = self.client.get("/api/positive-mentions/lectures/")
        self.assertEqual(response.data["count"], 5)
        self.assertEqual(
            {row["session_id"] for row in response.data["results"]},
            {
                "included",
                "too-early",
                "at-exclusive-end",
                "outside-old-test-window",
                "outside-new-test-window",
            },
        )

    def test_pagination_search_and_filters(self):
        self.make_lecture("alpha", subject="Alpha Course", trainer="Trainer A")
        self.make_lecture("beta", subject="Beta Course", trainer="Trainer B")
        response = self.client.get(
            "/api/positive-mentions/lectures/",
            {"search": "trainer", "trainer": "Trainer A", "page_size": 1},
        )
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["total_pages"], 1)
        self.assertEqual(response.data["results"][0]["session_id"], "alpha")
        self.assertNotIn("positive_clips", response.data["results"][0])

    def test_clip_production_filters_use_ready_asset_exists_without_duplicates(self):
        self.make_lecture(
            "ready",
            trainer="Trainer A",
            positive_clips=[{"start": "00:00:01"}, {"start": "00:00:02"}],
            positive_clips_count=2,
        )
        self.make_lecture(
            "pending-a",
            trainer="Trainer A",
            positive_clips=[{"start": "00:00:01"}],
            positive_clips_count=1,
        )
        self.make_lecture(
            "pending-b",
            trainer="Trainer B",
            positive_clips=[{"start": "00:00:01"}],
            positive_clips_count=1,
        )
        self.make_lecture("no-moments", trainer="Trainer A")

        self.make_asset("ready-1", "ready", clip_index=0)
        self.make_asset("ready-2", "ready", clip_index=1)
        self.make_asset("failed", "pending-a", trim_status="failed")
        self.make_asset("timed-out", "pending-a", trim_status="timed_out")
        self.make_asset("rendered", "pending-a", trim_status="rendered")
        self.make_asset("no-url", "pending-a", clip_url="")
        self.make_asset("invalid-url", "pending-a", clip_url="not-a-web-url")

        unfiltered = self.client.get("/api/positive-mentions/lectures/", {"page_size": 20})
        self.assertEqual(unfiltered.data["count"], 4)
        ready_row = next(row for row in unfiltered.data["results"] if row["session_id"] == "ready")
        self.assertTrue(ready_row["has_ready_clips"])
        self.assertEqual(ready_row["ready_clips_count"], 2)

        ready = self.client.get(
            "/api/positive-mentions/lectures/", {"clip_production_status": "ready"}
        )
        self.assertEqual(ready.data["count"], 1)
        self.assertEqual([row["session_id"] for row in ready.data["results"]], ["ready"])

        pending = self.client.get(
            "/api/positive-mentions/lectures/", {"clip_production_status": "pending"}
        )
        self.assertEqual(pending.data["count"], 2)
        self.assertEqual(
            {row["session_id"] for row in pending.data["results"]},
            {"pending-a", "pending-b"},
        )

        no_moments = self.client.get(
            "/api/positive-mentions/lectures/",
            {"clip_production_status": "no_positive_moments"},
        )
        self.assertEqual(no_moments.data["count"], 1)
        self.assertEqual(no_moments.data["results"][0]["session_id"], "no-moments")

        combined = self.client.get(
            "/api/positive-mentions/lectures/",
            {"clip_production_status": "pending", "trainer": "Trainer A"},
        )
        self.assertEqual(combined.data["count"], 1)
        self.assertEqual(combined.data["results"][0]["session_id"], "pending-a")

        summary = self.client.get(
            "/api/positive-mentions/summary/", {"clip_production_status": "ready"}
        )
        self.assertEqual(summary.data["processed_lectures"], 1)
        self.assertEqual(summary.data["total_positive_clips"], 2)

    def test_summary_is_calculated_from_database(self):
        self.make_lecture(
            "with",
            positive_clips=[{"start": "00:00:01"}, {"start": "00:00:02"}],
            positive_clips_count=99,
            has_positive_clips=False,
            recording_url="https://tenant.sharepoint.com/video",
        )
        self.make_lecture(
            "without", positive_clips=[], positive_clips_count=99, has_positive_clips=True
        )
        response = self.client.get("/api/positive-mentions/summary/")
        self.assertEqual(response.data, {
            "processed_lectures": 2,
            "lectures_with_positive_clips": 1,
            "lectures_without_positive_clips": 1,
            "total_positive_clips": 2,
            "recordings_available": 1,
            "recordings_missing": 1,
        })

    def test_summary_uses_the_same_filters_as_the_unpaginated_list_scope(self):
        self.make_lecture(
            "alpha-with",
            trainer="Trainer A",
            positive_clips=[{"start": "00:00:01"}],
        )
        self.make_lecture("alpha-without", trainer="Trainer A", positive_clips=[])
        self.make_lecture(
            "beta-with",
            trainer="Trainer B",
            positive_clips=[{"start": "00:00:01"}, {"start": "00:00:02"}],
        )
        response = self.client.get(
            "/api/positive-mentions/summary/", {"trainer": "Trainer A", "page_size": 1}
        )
        self.assertEqual(response.data["processed_lectures"], 2)
        self.assertEqual(response.data["lectures_with_positive_clips"], 1)
        self.assertEqual(response.data["total_positive_clips"], 1)

    def test_detail_adds_ready_asset_without_changing_full_recording_watch(self):
        session_id = "asset-mapping"
        recording_url = "https://tenant.sharepoint.com/full-lecture.mp4?source=app"
        lecture = self.make_lecture(
            session_id,
            positive_clips=[
                {
                    "start": "00:02:00.000",
                    "end": "00:02:20.000",
                    "start_cue": 20,
                    "end_cue": 21,
                    "positive_quote": "Later ready moment",
                },
                {
                    "start": "00:01:00.000",
                    "end": "00:01:10.000",
                    "start_cue": 10,
                    "end_cue": 11,
                    "positive_quote": "Earlier pending moment",
                },
            ],
            positive_clips_count=2,
            recording_url=recording_url,
        )
        clip_url = "https://tenant.sharepoint.com/trimmed-ready.mp4"
        self.make_asset(
            f"{session_id}:20:21",
            session_id,
            clip_index=1,
            source_start="00:02:00.000",
            source_end="00:02:20.000",
            duration_seconds="24.500",
            clip_filename="ready-later.mp4",
            clip_url=clip_url,
        )
        self.make_asset(
            f"{session_id}:10:11:failed",
            session_id,
            clip_index=2,
            source_start="00:01:00.000",
            source_end="00:01:10.000",
            trim_status="failed",
        )
        self.make_asset(
            f"{session_id}:10:11:temporary",
            session_id,
            clip_index=2,
            source_start="00:01:00.000",
            source_end="00:01:10.000",
            clip_url="https://creatomate.com/temporary-render.mp4",
        )

        session_key = encode_session_id(lecture.session_id)
        detail = self.client.get(f"/api/positive-mentions/lectures/{session_key}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["recording_url"], recording_url)
        self.assertEqual(detail.data["ready_clips_count"], 1)
        self.assertEqual(detail.data["clips"][0]["positive_quote"], "Earlier pending moment")
        self.assertIsNone(detail.data["clips"][0]["clip_asset"])
        ready = detail.data["clips"][1]["clip_asset"]
        self.assertEqual(ready["url"], clip_url)
        self.assertNotEqual(ready["url"], detail.data["recording_url"])
        self.assertEqual(ready["filename"], "ready-later.mp4")
        self.assertEqual(ready["duration_seconds"], 24.5)

        watch = self.client.get(
            f"/api/positive-mentions/lectures/{session_key}/clips/1/watch/"
        )
        self.assertEqual(watch.status_code, 200)
        self.assertIn("nav=", watch.data["url"])
        self.assertIn("source=app", watch.data["url"])
        self.assertNotEqual(watch.data["url"], clip_url)

    def test_details_normalize_json_and_watch_endpoint_uses_database_values(self):
        lecture = self.make_lecture(
            "special/id?yes",
            positive_clips=[
                {"start": "00:02:00.000", "positive_quote": "Second"},
                {"start": "00:01:00.500", "positive_quote": "First"},
            ],
            positive_clips_count=2,
            has_positive_clips=True,
            recording_url="https://tenant.sharepoint.com/video?source=app",
        )
        key = encode_session_id(lecture.session_id)
        detail = self.client.get(f"/api/positive-mentions/lectures/{key}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["clips"][0]["positive_quote"], "First")
        watch = self.client.get(
            f"/api/positive-mentions/lectures/{key}/clips/0/watch/"
        )
        self.assertEqual(watch.status_code, 200)
        self.assertIn("nav=", watch.data["url"])
        self.assertIn("source=app", watch.data["url"])
        nav_value = parse_qs(urlsplit(watch.data["url"]).query)["nav"][0]
        nav = json.loads(base64.urlsafe_b64decode(
            nav_value + "=" * (-len(nav_value) % 4)
        ))
        self.assertEqual(nav["playbackOptions"]["startTimeInSeconds"], 0.5)

    def test_watch_endpoint_never_seeks_before_recording_start(self):
        lecture = self.make_lecture(
            "early-moment",
            positive_clips=[{"start": "00:00:30.000", "positive_quote": "Early"}],
            recording_url="https://tenant.sharepoint.com/video",
        )
        key = encode_session_id(lecture.session_id)
        watch = self.client.get(
            f"/api/positive-mentions/lectures/{key}/clips/0/watch/"
        )
        nav_value = parse_qs(urlsplit(watch.data["url"]).query)["nav"][0]
        nav = json.loads(base64.urlsafe_b64decode(
            nav_value + "=" * (-len(nav_value) % 4)
        ))
        self.assertEqual(nav["playbackOptions"]["startTimeInSeconds"], 0)

    def test_missing_recording_and_invalid_indices_are_controlled(self):
        missing = self.make_lecture(
            "missing", positive_clips=[{"start": "00:00:10.000"}]
        )
        key = encode_session_id(missing.session_id)
        response = self.client.get(
            f"/api/positive-mentions/lectures/{key}/clips/0/watch/"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "recording_not_available")

        recorded = self.make_lecture(
            "recorded",
            recording_url="https://tenant.sharepoint.com/video",
            positive_clips=[{"start": "00:00:10.000"}],
        )
        recorded_key = encode_session_id(recorded.session_id)
        invalid_clip = self.client.get(
            f"/api/positive-mentions/lectures/{recorded_key}/clips/9/watch/"
        )
        self.assertEqual(invalid_clip.status_code, 404)
        invalid_lecture = self.client.get(
            f"/api/positive-mentions/lectures/{encode_session_id('unknown')}/"
        )
        self.assertEqual(invalid_lecture.status_code, 404)
