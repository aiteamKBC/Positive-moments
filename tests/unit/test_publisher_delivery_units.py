"""
Production video delivery, unit level: Bunny token helpers, the learner-delivery security check,
secret scrubbing of the pull-zone read, feed cursors and the signed caption read-back. No socket,
no database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.bunny import tokens
from app.bunny.client import BunnyCoreClient
from app.publisher import delivery
from app.publisher.bunny_jobs import CaptionWorker
from app.publisher.library_security import check_delivery_security, read_delivery_security
from app.publisher.operations import OperatorActionRefused

KEY = "4742a81b-bf15-42fe-8b1c-8fcb9024c550"          # Bunny's documentation sample, not a real key
VIDEO = "32d140e2-e4f4-4eec-9d53-20371e9be607"

SECURE_LIBRARY = {"Id": 786045, "PlayerTokenAuthenticationEnabled": True, "AllowDirectPlay": False,
                  "BlockNoneReferrer": True, "EnableMP4Fallback": False, "AllowedReferrers": ["lms.example.invalid"],
                  "PullZoneId": 6840885, "KeepOriginalFiles": True, "EnableDRM": False}
SECURE_ZONE = {"Id": 6840885, "ZoneSecurityEnabled": True, "ZoneSecurityIncludeHashRemoteIP": False}


# =========================================================================== embed token (LMS signing)
def test_embed_token_is_the_documented_sha256_hex_of_key_video_and_expiry():
    expected = hashlib.sha256(f"{KEY}{VIDEO}1623440202".encode()).hexdigest()
    assert tokens.embed_token(KEY, VIDEO, 1623440202) == expected and len(expected) == 64


def test_signed_embed_url_is_short_lived_and_never_carries_the_key():
    url = tokens.signed_embed_url(786045, VIDEO, KEY, ttl_seconds=600, now=1_800_000_000)
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.netloc == "player.mediadelivery.net" and parts.path == f"/embed/786045/{VIDEO}"
    assert query["expires"] == "1800000600" and query["token"] == tokens.embed_token(KEY, VIDEO, 1800000600)
    assert KEY not in url
    for bad in (0, tokens.MAX_EMBED_TTL_SECONDS + 1):
        with pytest.raises(ValueError):
            tokens.signed_embed_url(786045, VIDEO, KEY, ttl_seconds=bad)
    with pytest.raises(ValueError):
        tokens.embed_token(KEY, VIDEO, 1_800_000_000_000)          # milliseconds are not supported


def test_a_token_for_one_video_or_expiry_does_not_fit_another():
    token = tokens.embed_token(KEY, VIDEO, 1800000600)
    assert token != tokens.embed_token(KEY, VIDEO, 1800000601)
    assert token != tokens.embed_token(KEY, "00000000-0000-0000-0000-000000000000", 1800000600)
    assert token != tokens.embed_token("another-key", VIDEO, 1800000600)


# =========================================================================== CDN token (caption read-back)
def test_cdn_token_is_the_documented_hmac_sha256_base64url():
    message = f"/{VIDEO}/captions/en.vtt1800000300ver=2"
    digest = hmac.new(b"zone-key", message.encode(), hashlib.sha256).digest()
    expected = "HS256-" + base64.b64encode(digest).decode().replace("+", "-").replace("/", "_").rstrip("=")
    token = tokens.cdn_token("zone-key", f"/{VIDEO}/captions/en.vtt", 1800000300, params={"ver": 2, "token": "x"})
    assert token == expected and "=" not in token and "+" not in token and "/" not in token
    assert tokens.cdn_token("zone-key", "/a", 1, user_ip="10.0.0.1").startswith("HS256-1-")


def test_signed_cdn_url_keeps_the_cache_buster_and_adds_token_and_expiry():
    url = tokens.signed_cdn_url("cdn.example.invalid", f"/{VIDEO}/captions/en.vtt", "zone-key",
                                params={"ver": 3}, ttl_seconds=300, now=1_800_000_000)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["ver"] == "3" and query["expires"] == "1800000300"
    assert query["token"] == tokens.cdn_token("zone-key", f"/{VIDEO}/captions/en.vtt", 1800000300, params={"ver": 3})
    assert "zone-key" not in url


class _Cdn:
    def __init__(self, text):
        self.text, self.urls = text, []

    def __call__(self, request, timeout):
        import io
        self.urls.append(request.full_url)

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Response(self.text.encode("utf-8"))


def _worker(config, served):
    bunny = SimpleNamespace(get_video=lambda video_id: {"captions": [{"srclang": "en", "label": "English",
                                                                       "version": 4}]})
    cdn = _Cdn(served)
    return CaptionWorker(SimpleNamespace(config=config, bunny=bunny, open_url=cdn)), cdn


def test_production_read_back_signs_the_caption_url_with_the_pull_zone_key():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n"
    config = SimpleNamespace(caption_language="en", caption_label="English", mode="PRODUCTION_WRITE",
                             cdn_hostname="cdn.example.invalid", cdn_token_key="zone-key")
    worker, cdn = _worker(config, text)
    readback = worker._read_back(VIDEO, SimpleNamespace(text=text))
    assert readback["content_check"] in ("IDENTICAL", "EQUIVALENT")
    [url] = cdn.urls
    assert "token=HS256-" in url and "expires=" in url and "ver=4" in url and "zone-key" not in url


def test_staging_read_back_without_a_key_is_unsigned_as_before():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n"
    config = SimpleNamespace(caption_language="en", caption_label="English", mode="STAGING_WRITE",
                             staging_cdn_hostname="cdn.example.invalid")
    worker, cdn = _worker(config, text)
    worker._read_back(VIDEO, SimpleNamespace(text=text))
    assert cdn.urls == [f"https://cdn.example.invalid/{VIDEO}/captions/en.vtt?ver=4"]


# =========================================================================== delivery security check
def test_a_fully_locked_down_library_passes():
    report = check_delivery_security(SECURE_LIBRARY, SECURE_ZONE)
    assert report == {"secure": True, "problems": [], "observed": report["observed"]}
    assert report["observed"]["AllowedReferrers_count"] == 1 and "AllowedReferrers" not in report["observed"]


@pytest.mark.parametrize("field,value,code", [
    ("PlayerTokenAuthenticationEnabled", False, "LIBRARY_EMBED_TOKEN_AUTHENTICATION"),
    ("AllowDirectPlay", True, "LIBRARY_DIRECT_PLAY"),
    ("BlockNoneReferrer", False, "LIBRARY_BLOCK_NONE_REFERRER"),
    ("EnableMP4Fallback", True, "LIBRARY_MP4_FALLBACK"),
    ("AllowedReferrers", [], "LIBRARY_ALLOWED_REFERRERS"),
])
def test_each_open_library_setting_is_reported(field, value, code):
    report = check_delivery_security({**SECURE_LIBRARY, field: value}, SECURE_ZONE)
    assert not report["secure"] and [p.split(":")[0] for p in report["problems"]] == [code]


def test_cdn_token_authentication_off_or_unreadable_is_reported():
    off = check_delivery_security(SECURE_LIBRARY, {**SECURE_ZONE, "ZoneSecurityEnabled": False})
    assert [p.split(":")[0] for p in off["problems"]] == ["PULL_ZONE_TOKEN_AUTHENTICATION"]
    assert [p.split(":")[0] for p in check_delivery_security(SECURE_LIBRARY, None)["problems"]] == \
        ["PULL_ZONE_UNREADABLE"]


def test_library_786045_as_read_on_2026_10_24_is_not_yet_secure():
    observed = {"Id": 786045, "PlayerTokenAuthenticationEnabled": False, "AllowDirectPlay": True,
                "BlockNoneReferrer": True, "EnableMP4Fallback": False, "AllowedReferrers": ["one.invalid"],
                "PullZoneId": 6840885}
    codes = {p.split(":")[0] for p in check_delivery_security(observed, None)["problems"]}
    assert codes == {"LIBRARY_EMBED_TOKEN_AUTHENTICATION", "LIBRARY_DIRECT_PLAY", "PULL_ZONE_UNREADABLE"}


def test_the_pull_zone_read_drops_its_secret_key():
    client = BunnyCoreClient("account-key")
    client._http = SimpleNamespace(call=lambda method, path, idempotent: SimpleNamespace(body={
        "Id": 6840885, "ZoneSecurityEnabled": True, "ZoneSecurityKey": "SECRET-ZONE-KEY",
        "ZoneSecurityIncludeHashRemoteIP": False, "ApiKey": "SECRET", "StorageZoneId": 1}))
    zone = client.get_pull_zone(6840885)
    assert zone == {"Id": 6840885, "ZoneSecurityEnabled": True, "ZoneSecurityIncludeHashRemoteIP": False,
                    "StorageZoneId": 1}
    core = SimpleNamespace(get_video_library=lambda library: dict(SECURE_LIBRARY, ApiKey="LIB-SECRET"),
                           get_pull_zone=lambda zone_id: zone)
    report = read_delivery_security(core, 786045)
    assert report["secure"] and report["library_id"] == 786045
    assert "SECRET" not in repr(report)


# =========================================================================== feed cursor
def test_cursor_round_trips_and_refuses_tampering():
    at = datetime(2026, 10, 24, 9, 30, 1, 123456, tzinfo=timezone.utc)
    cursor = delivery.encode_cursor(at, "OCC-4493AC65DF5041459BC5A14DA8B357A0")
    assert delivery.decode_cursor(cursor) == (at, "OCC-4493AC65DF5041459BC5A14DA8B357A0")
    naive = base64.urlsafe_b64encode(b'{"t":"2026-10-24T09:30:01","o":"OCC-1"}').decode().rstrip("=")
    injected = base64.urlsafe_b64encode(b'{"t":"2026-10-24T09:30:01+00:00","o":"x\' OR 1=1"}').decode().rstrip("=")
    for bad in ("", "%%%", "bm90LWpzb24", naive, injected):
        with pytest.raises(OperatorActionRefused) as refused:
            delivery.decode_cursor(bad)
        assert refused.value.code == "INVALID_CURSOR"
