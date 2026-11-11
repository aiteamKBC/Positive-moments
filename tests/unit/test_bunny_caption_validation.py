"""
Caption validation: remove speaker-attribution METADATA, keep every spoken word.

A name said aloud is content and must reach the captions unchanged; validation proves the
publication copy is the source minus voice tags (cue by cue, timing and text), never scans the
spoken text for participant names, and allows the last cue at most 1.0 s past the MEASURED media.
All names below are synthetic.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.bunny import captions as cap

RAW = ("WEBVTT\r\n\r\n"
       "1\r\n00:00:01.000 --> 00:00:03.000\r\n"
       "<v Synthetic Trainer Alpha>Thank you, Synthetic Learner Beta, good question.</v>\r\n\r\n"
       "2\r\n00:00:03.000 --> 00:00:05.500\r\n"
       "<v Synthetic Learner Beta>Synthetic Trainer Alpha: can you repeat that?</v>\r\n\r\n"
       "3\r\n00:00:05.000 --> 00:00:07.250\r\n"
       "<v Synthetic Trainer Alpha>Beta - you said it first &amp; best.</v>\r\n")
LAST_END = 7.25


def validate(build, media=10.0, **kw):
    return cap.validate_caption(build, RAW, media_duration_seconds=media, **kw)


def tampered(build, text):
    return replace(build, text=text, sha256=cap.sha256_text(text), size_bytes=len(text.encode()))


# -- metadata removed, words kept ---------------------------------------------------------------
def test_speaker_tags_are_removed():
    build = cap.build_caption(RAW)
    assert build.removed_voice_tags == 6 and "<v" not in build.text and "</v>" not in build.text
    result = validate(build)
    assert result.ok, result.failures
    assert result.checks["voice_tags_remaining"] == 0 and result.checks["speaker_labels_remaining"] == 0


def test_spoken_names_are_kept_exactly():
    build = cap.build_caption(RAW)
    assert "Thank you, Synthetic Learner Beta, good question." in build.text.split("\n")
    assert validate(build).checks["spoken_lines_identical_to_source"] is True


def test_name_colon_and_name_dash_in_speech_stay_unchanged():
    lines = cap.build_caption(RAW).text.split("\n")
    assert "Synthetic Trainer Alpha: can you repeat that?" in lines
    assert "Beta - you said it first &amp; best." in lines                    # entity kept as written


def test_names_are_never_scanned_as_pii():
    result = validate(cap.build_caption(RAW))
    assert result.ok
    assert not any("label_string" in key or "name" in key for key in result.checks)
    assert not any("label_string" in failure for failure in result.failures)


# -- anything the build would have injected or altered is rejected --------------------------------
def test_injected_attribution_is_rejected():
    build = cap.build_caption(RAW)
    injected = tampered(build, build.text.replace("Thank you,", "Synthetic Trainer Alpha: Thank you,", 1))
    failures = validate(injected).failures
    assert "spoken_lines_identical_to_source" in failures and "timings_and_text_identical_to_source" in failures


def test_a_surviving_voice_tag_is_rejected():
    build = cap.build_caption(RAW)
    leaked = tampered(build, build.text.replace("Thank you,", "<v Synthetic Trainer Alpha>Thank you,", 1))
    assert {"voice_tags_remaining", "speaker_labels_remaining"} <= set(validate(leaked).failures)


def test_altered_or_dropped_words_are_rejected():
    build = cap.build_caption(RAW)
    assert "spoken_lines_identical_to_source" in validate(tampered(build, build.text.replace("good", "great"))).failures
    assert "spoken_lines_identical_to_source" in validate(
        tampered(build, build.text.replace("Synthetic Learner Beta, ", ""))).failures


def test_cue_timing_must_match_the_source():
    build = cap.build_caption(RAW)
    shifted = tampered(build, build.text.replace("00:00:03.000 --> 00:00:05.500", "00:00:03.000 --> 00:00:05.600"))
    assert "timings_and_text_identical_to_source" in validate(shifted).failures


# -- tail against the measured media duration -------------------------------------------------------
def test_half_a_second_tail_passes_and_is_recorded():
    result = validate(cap.build_caption(RAW), media=LAST_END - 0.5)
    assert result.ok and result.checks["tail_overrun_seconds"] == 0.5 and result.checks["tail_tolerance_seconds"] == 1.0


def test_two_second_overrun_fails():
    result = validate(cap.build_caption(RAW), media=LAST_END - 2.0)
    assert "last_cue_within_media" in result.failures and result.checks["tail_overrun_seconds"] == 2.0


def test_tolerance_is_capped_at_one_second():
    assert "last_cue_within_media" in validate(cap.build_caption(RAW), media=LAST_END - 2.0,
                                               tail_tolerance_seconds=5.0).failures


def test_measured_mp4_duration_not_bunnys_rounded_length():
    """AI In Marketing 2026-10-23: last cue 7654.252 s, MP4 7653.760 s, Bunny length 7653."""
    raw = "WEBVTT\n\n00:00:01.000 --> 02:07:34.252\n<v Synthetic Trainer Alpha>Goodbye.</v>\n"
    build = cap.build_caption(raw)
    measured = cap.validate_caption(build, raw, media_duration_seconds=7653.760)
    rounded = cap.validate_caption(build, raw, media_duration_seconds=7653.0)
    assert measured.ok and measured.checks["tail_overrun_seconds"] == 0.492
    assert "last_cue_within_media" in rounded.failures


def test_timestamps_are_never_trimmed_or_clamped():
    build = cap.build_caption(RAW)
    validate(build, media=LAST_END - 0.5)
    assert "00:00:05.000 --> 00:00:07.250" in build.text


# -- malformed input stays rejected ---------------------------------------------------------------
@pytest.mark.parametrize("raw, code", [
    ("not a transcript", "SOURCE_NOT_WEBVTT"),
    ("WEBVTT\n\n<v A>words with no timing</v>\n", "SOURCE_BLOCK_WITHOUT_TIMING"),
])
def test_malformed_webvtt_is_rejected(raw, code):
    with pytest.raises(cap.CaptionBuildError) as error:
        cap.build_caption(raw)
    assert error.value.code == code or error.value.code.startswith("SOURCE_PARSE_")
