"""Deterministic identity/titles, the caption transform, and the pilot manifest."""
from __future__ import annotations

import pytest

from app.bunny import captions as cap
from app.bunny.identity import (
    TITLE_MAX_LENGTH,
    build_title,
    caption_publication_id,
    clean_subject,
    recording_identity,
    video_record_id,
)
from app.bunny.manifest import ManifestError, PilotManifest
from tests.unit.bunny_fakes import LECTURE_ID, SPEAKERS, teams_vtt


# ---------------------------------------------------------------- identity
def test_recording_identity_is_deterministic_and_distinct():
    a = recording_identity("drive-1", "item-1")
    assert a == recording_identity("drive-1", "item-1")
    assert a.startswith("rec-") and len(a) == 12
    assert a != recording_identity("drive-1", "item-2")
    assert a != recording_identity("drive-2", "item-1")
    # The separator prevents ("ab","c") colliding with ("a","bc").
    assert recording_identity("ab", "c") != recording_identity("a", "bc")
    with pytest.raises(ValueError):
        recording_identity("", "item")


def test_record_and_publication_ids_are_stable_uuids():
    assert video_record_id(1, "d", "i") == video_record_id("1", "d", "i")
    assert video_record_id(1, "d", "i") != video_record_id(2, "d", "i")
    assert caption_publication_id(1, "ABC", "en", "f" * 64, "v1") == caption_publication_id(1, "abc", "en", "f" * 64, "v1")


def test_title_format_and_sanitization():
    identity = recording_identity("d", "i")
    title = build_title("AI in Project Control 2026", "2026-10-16", LECTURE_ID, identity)
    assert title == f"AI in Project Control 2026 | 2026-10-16 | bb2d676b | {identity}"
    nasty = build_title("Risk | <script>Mgmt</script>\n\t&amp; Ops™ ", "2026-10-16", LECTURE_ID, identity)
    assert nasty.count("|") == 3 and "<" not in nasty and "\n" not in nasty and "&" in nasty
    assert clean_subject("   ") == "Lecture"


def test_title_respects_length_and_keeps_identity():
    identity = recording_identity("d", "i")
    title = build_title("X" * 500, "2026-10-16", LECTURE_ID, identity)
    assert len(title) <= TITLE_MAX_LENGTH and title.endswith(identity)


# ---------------------------------------------------------------- captions
def test_caption_strips_voice_tags_and_preserves_cues():
    raw = teams_vtt(8)
    build = cap.build_caption(raw)
    assert build.removed_voice_tags == 16 and build.cue_count == build.source_cue_count == 8
    assert "<v" not in build.text and "\r" not in build.text and build.text.startswith("WEBVTT\n\n")
    assert all(name not in build.text for name in SPEAKERS)
    assert "&amp;" in build.text                     # entities stay escaped (valid WebVTT)
    result = cap.validate_caption(build, raw, media_duration_seconds=7941.632)
    assert result.ok, result.failures
    assert result.checks["timings_and_text_identical_to_source"]
    assert repr(build).startswith("CaptionBuild(") and "Spoken" not in repr(build)


def test_caption_offset_is_applied_and_negative_refused():
    raw = teams_vtt(4)
    shifted = cap.build_caption(raw, applied_offset_ms=1500)
    assert cap.validate_caption(shifted, raw, media_duration_seconds=8000).ok
    with pytest.raises(cap.CaptionBuildError) as error:
        cap.build_caption(raw, applied_offset_ms=-500_000)
    assert error.value.code == "NEGATIVE_CUE_AFTER_OFFSET"


def test_caption_refuses_cues_beyond_media():
    raw = teams_vtt(4, last_end_seconds=7950.0)
    result = cap.validate_caption(cap.build_caption(raw), raw, media_duration_seconds=7941.632)
    assert not result.ok and "last_cue_within_media" in result.failures


def test_caption_refuses_non_webvtt():
    with pytest.raises(cap.CaptionBuildError):
        cap.build_caption("1\n00:00:01,000 --> 00:00:02,000\nhello\n")


# ---------------------------------------------------------------- manifest
def test_manifest_round_trip_and_url_refusal(tmp_path):
    path = tmp_path / "m" / "manifest.json"
    manifest = PilotManifest(path)
    manifest.put_video({"bunny_video_record_id": "r1", "bunny_library_id": 1, "import_status": "PENDING"},
                       event="X")
    assert PilotManifest(path).video("r1")["import_status"] == "PENDING"
    with pytest.raises(ManifestError):
        manifest.put_video({"bunny_video_record_id": "r2", "bunny_library_id": 1,
                            "note": "https://x.sharepoint.com/download?tempauth=abc"}, event="X")
    assert PilotManifest(path).video("r2") is None


def test_manifest_current_pointer_requires_published_same_video(tmp_path):
    manifest = PilotManifest(tmp_path / "manifest.json")
    manifest.put_caption({"publication_id": "p1", "bunny_library_id": 1, "bunny_video_id": "v1",
                          "language_code": "en", "publication_status": "PREPARED"}, event="X")
    with pytest.raises(ManifestError):
        manifest.set_current_caption(1, "v1", "en", "p1")
    manifest.put_caption({**manifest.caption("p1"), "publication_status": "VERIFIED"}, event="X")
    with pytest.raises(ManifestError):
        manifest.set_current_caption(1, "v2", "en", "p1")
    manifest.set_current_caption(1, "v1", "en", "p1")
    assert manifest.current_caption(1, "V1", "en")["publication_id"] == "p1"
