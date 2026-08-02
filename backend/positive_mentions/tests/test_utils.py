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
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

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

