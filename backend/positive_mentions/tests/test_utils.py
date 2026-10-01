import base64
import json
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from positive_mentions.utils import (
    decode_session_id,
    encode_session_id,
    normalize_clips,
    timestamp_to_seconds,
    timestamped_sharepoint_url,
)


class TimestampTests(SimpleTestCase):
    def test_timestamp_conversion(self):
        self.assertEqual(timestamp_to_seconds("01:21:20.134"), 4880.134)
        self.assertEqual(timestamp_to_seconds("00:00:05"), 5)
        self.assertIsNone(timestamp_to_seconds("1:99:00"))
        self.assertIsNone(timestamp_to_seconds(None))

    def test_session_key_round_trip_with_special_characters(self):
        session_id = "Meeting/2026+07?trainer=A & subject=QA"
        self.assertEqual(decode_session_id(encode_session_id(session_id)), session_id)


class SharePointUrlTests(SimpleTestCase):
    def decode_nav(self, url):
        value = parse_qs(urlsplit(url).query)["nav"][0]
        return json.loads(base64.b64decode(value))

    def test_preserves_query_and_referral_data(self):
        original_nav = base64.urlsafe_b64encode(json.dumps({
            "referrer": "StreamWebApp",
            "playbackOptions": {"autoplay": True},
        }).encode()).decode().rstrip("=")
        url = f"https://tenant.sharepoint.com/video?foo=bar&nav={original_nav}"
        result = timestamped_sharepoint_url(url, 4880.134)
        query = parse_qs(urlsplit(result).query)
        self.assertEqual(query["foo"], ["bar"])
        nav = self.decode_nav(result)
        self.assertEqual(nav["referrer"], "StreamWebApp")
        self.assertTrue(nav["playbackOptions"]["autoplay"])
        self.assertEqual(nav["playbackOptions"]["startTimeInSeconds"], 4880.134)

    def test_a_direct_file_link_opens_in_the_stream_player_at_the_moment(self):
        """The 2026-09-18 MSP shape: a drive item web URL to the .mp4 itself."""
        url = ("https://tenant.sharepoint.com/sites/MSP/Shared%20Documents/"
               "Ray%20%E2%80%93%20MSP/Recordings/Lecture-20260918_085900UTC-Meeting%20Recording.mp4")
        result = timestamped_sharepoint_url(url, 1701.0)
        parts = urlsplit(result)
        self.assertEqual(parts.netloc, "tenant.sharepoint.com")
        self.assertEqual(parts.path, "/sites/MSP/_layouts/15/stream.aspx")
        query = parse_qs(parts.query)
        self.assertEqual(query["id"], [
            "/sites/MSP/Shared Documents/Ray – MSP/Recordings/"
            "Lecture-20260918_085900UTC-Meeting Recording.mp4"])
        self.assertEqual(self.decode_nav(result)["playbackOptions"]["startTimeInSeconds"], 1701.0)

    def test_a_onedrive_recording_uses_the_onedrive_player(self):
        url = ("https://tenant-my.sharepoint.com/personal/someone_tenant_com/Documents/"
               "Recordings/Lecture.mp4")
        parts = urlsplit(timestamped_sharepoint_url(url, 60))
        self.assertEqual(parts.path, "/personal/someone_tenant_com/_layouts/15/stream.aspx")
        self.assertEqual(parse_qs(parts.query)["id"],
                         ["/personal/someone_tenant_com/Documents/Recordings/Lecture.mp4"])

    def test_a_sharing_link_keeps_its_shape(self):
        url = "https://tenant.sharepoint.com/:v:/s/MSP/IQDVxF9YZFCuSrTUCXfVQlzn"
        parts = urlsplit(timestamped_sharepoint_url(url, 90))
        self.assertEqual(parts.path, "/:v:/s/MSP/IQDVxF9YZFCuSrTUCXfVQlzn")
        self.assertEqual(self.decode_nav(timestamped_sharepoint_url(url, 90))
                         ["playbackOptions"]["startTimeInSeconds"], 90)

    def test_nav_uses_streams_standard_padded_base64(self):
        result = timestamped_sharepoint_url("https://tenant.sharepoint.com/:v:/s/X/abc", 1701.0)
        value = parse_qs(urlsplit(result).query)["nav"][0]
        self.assertEqual(len(value) % 4, 0)
        self.assertNotIn("-", value)
        self.assertNotIn("_", value)

    def test_malformed_nav_is_replaced_safely(self):
        result = timestamped_sharepoint_url(
            "https://tenant.sharepoint.com/video?nav=not-base64&x=1", 12.5
        )
        self.assertEqual(self.decode_nav(result)["playbackOptions"]["startTimeInSeconds"], 12.5)


class ClipNormalizationTests(SimpleTestCase):
    def test_malformed_fields_do_not_break_normalization_and_clips_are_sorted(self):
        result = normalize_clips([
            {"start": "00:02:00.000", "positive_quote": "Later", "dialogue": "bad"},
            "not-an-object",
            {
                "start": "00:01:00.000",
                "positive_quote": "Earlier",
                "semantic_verification": None,
            },
            {"positive_quote": "No timestamp"},
        ])
        self.assertEqual([clip["positive_quote"] for clip in result], [
            "Earlier", "Later", "No timestamp"
        ])
        self.assertEqual(result[0]["semantic_verification"], {})
        self.assertEqual(result[1]["dialogue"], [])

    def test_accepts_wrapped_clip_payload(self):
        self.assertEqual(
            normalize_clips({"positive_clips": [{"positive_quote": "Great"}]})[0][
                "positive_quote"
            ],
            "Great",
        )

