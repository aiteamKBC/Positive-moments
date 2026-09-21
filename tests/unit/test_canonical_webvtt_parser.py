"""
Phase 2C1 canonical WebVTT parser.

Deterministic cue extraction from the Phase 2B combined transcript. The parser
stores RAW provider speaker labels only: no identity inference of any kind
happens here.
"""
import logging

import pytest

from app.common.hashing import content_sha256
from app.transcripts.webvtt import (
    EMPTY_TRANSCRIPT,
    INVALID_WEBVTT,
    PARSED,
    PARSED_WITH_WARNINGS,
    PARSER_VERSION,
    UNSUPPORTED_STRUCTURE,
    parse_combined_webvtt,
)


def vtt(*blocks: str) -> str:
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def codes(document):
    return sorted({item["code"] for item in document.warnings})


# --- structure ----------------------------------------------------------------


def test_simple_valid_webvtt():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.500\n<v Jane Doe>Good morning.</v>",
        "00:00:03.000 --> 00:00:04.250\n<v Ali Hassan>Morning.</v>",
    ))
    assert document.status == PARSED
    assert document.warnings == []
    assert [(c.cue_index, c.start_ms, c.end_ms) for c in document.cues] == [
        (1, 1000, 2500), (2, 3000, 4250)]
    assert document.metrics["cue_count"] == 2
    assert document.metrics["first_cue_start_ms"] == 1000
    assert document.metrics["last_cue_end_ms"] == 4250
    assert document.metrics["duration_ms"] == 3250


def test_optional_cue_identifiers_are_kept_as_provenance():
    document = parse_combined_webvtt(vtt(
        "cue-42\n00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Hello.</v>"))
    assert document.status == PARSED
    assert document.cues[0].metadata["cue_identifier"] == "cue-42"
    assert document.cues[0].text == "Hello."
    assert document.metrics["cue_identifier_count"] == 1


def test_multi_line_cue_text_is_joined_not_truncated():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:05.000\n<v Jane Doe>First line.\nSecond line.</v>"))
    assert document.cues[0].text == "First line.\nSecond line."


def test_cue_settings_after_the_timestamps_are_preserved():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000 align:start position:0%\n<v Jane Doe>Hi.</v>"))
    assert document.cues[0].metadata["cue_settings"] == "align:start position:0%"
    assert document.cues[0].text == "Hi."


def test_note_style_and_region_blocks_are_not_malformed_cues():
    document = parse_combined_webvtt(
        "WEBVTT\n\nNOTE this is a comment\n\n"
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Hi.</v>\n")
    assert document.status == PARSED
    assert document.metrics["malformed_block_count"] == 0
    assert document.metrics["cue_count"] == 1


# --- timestamps ----------------------------------------------------------------


def test_dot_milliseconds():
    document = parse_combined_webvtt(vtt("00:00:01.234 --> 00:00:02.567\nhello"))
    assert (document.cues[0].start_ms, document.cues[0].end_ms) == (1234, 2567)


def test_comma_milliseconds_are_accepted_defensively():
    document = parse_combined_webvtt(vtt("00:00:01,234 --> 00:00:02,567\nhello"))
    assert (document.cues[0].start_ms, document.cues[0].end_ms) == (1234, 2567)


def test_hours_beyond_twenty_three_are_supported():
    document = parse_combined_webvtt(vtt("25:01:01.500 --> 100:00:00.000\nlate cue"))
    assert document.cues[0].start_ms == 90_061_500
    assert document.cues[0].end_ms == 360_000_000


def test_zero_length_cue_is_kept_and_counted():
    document = parse_combined_webvtt(vtt("00:00:05.000 --> 00:00:05.000\nblip"))
    assert document.status == PARSED
    assert document.metrics["zero_length_cue_count"] == 1
    assert document.cues[0].start_ms == document.cues[0].end_ms == 5000


def test_end_before_start_is_rejected_not_repaired():
    document = parse_combined_webvtt(vtt(
        "00:00:09.000 --> 00:00:02.000\nbackwards",
        "00:00:10.000 --> 00:00:11.000\n<v Jane Doe>fine</v>"))
    assert document.status == PARSED_WITH_WARNINGS
    assert "END_BEFORE_START" in codes(document)
    assert document.metrics["malformed_block_count"] == 1
    # The bad block is not stored, and the good cue still gets index 1.
    assert [c.cue_index for c in document.cues] == [1]
    assert document.cues[0].start_ms == 10_000


def test_a_block_with_no_timing_line_is_reported_as_malformed():
    document = parse_combined_webvtt(vtt(
        "this block has no arrow at all",
        "00:00:01.000 --> 00:00:02.000\nok"))
    assert document.status == PARSED_WITH_WARNINGS
    assert "BLOCK_WITHOUT_TIMING" in codes(document)
    assert document.metrics["malformed_block_count"] == 1
    assert document.metrics["cue_count"] == 1


# --- speaker labels -------------------------------------------------------------


def test_raw_voice_tag_label_is_extracted():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Mohamed Elmasry>Good morning, all.</v>"))
    assert document.cues[0].speaker_label_raw == "Mohamed Elmasry"
    assert document.cues[0].text == "Good morning, all."
    assert document.metrics["unique_raw_speaker_label_count"] == 1


def test_cue_without_a_speaker_has_a_null_label_and_no_invented_speaker():
    document = parse_combined_webvtt(vtt("00:00:01.000 --> 00:00:02.000\nAnonymous line."))
    assert document.cues[0].speaker_label_raw is None
    assert document.cues[0].text == "Anonymous line."
    assert document.metrics["unique_raw_speaker_label_count"] == 0


def test_unclosed_voice_tag_still_yields_the_label():
    document = parse_combined_webvtt(vtt("00:00:01.000 --> 00:00:02.000\n<v Jane Doe>No close tag"))
    assert document.cues[0].speaker_label_raw == "Jane Doe"
    assert document.cues[0].text == "No close tag"


def test_repeated_identical_voice_label_is_one_speaker():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>One.</v> <v Jane Doe>Two.</v>"))
    assert document.cues[0].speaker_label_raw == "Jane Doe"
    assert document.metrics["multi_speaker_cue_count"] == 0


def test_conflicting_voice_labels_never_pick_a_person():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:04.000\n<v Jane Doe>Hello.</v> <v Ali Hassan>Hi.</v>"))
    cue = document.cues[0]
    assert cue.speaker_label_raw is None
    assert cue.metadata["multiple_speaker_labels"] == ["Jane Doe", "Ali Hassan"]
    assert document.status == PARSED_WITH_WARNINGS
    assert "MULTIPLE_SPEAKER_LABELS" in codes(document)
    # Both utterances survive; nothing is discarded to resolve the conflict.
    assert cue.text == "Hello. Hi."


def test_unique_speaker_label_count_is_distinct_not_total():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>a</v>",
        "00:00:03.000 --> 00:00:04.000\n<v Ali Hassan>b</v>",
        "00:00:05.000 --> 00:00:06.000\n<v Jane Doe>c</v>"))
    assert document.metrics["unique_raw_speaker_label_count"] == 2


# --- text handling ---------------------------------------------------------------


def test_html_entities_are_decoded_and_wording_preserved():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>R&amp;D costs &lt; 5% &gt; target</v>"))
    assert document.cues[0].text == "R&D costs < 5% > target"


def test_other_markup_is_removed_but_recorded_as_a_diagnostic():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>This is <b>very</b> <i>clear</i></v>"))
    cue = document.cues[0]
    assert cue.text == "This is very clear"
    assert cue.metadata["other_markup_tags"] == ["b", "i"]
    assert document.metrics["other_markup_tag_count"] == 1


def test_empty_cue_text_is_kept_counted_and_warned():
    """Evidence is preserved: the cue is stored, not dropped like the legacy parser."""
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe></v>",
        "00:00:03.000 --> 00:00:04.000\n<v Jane Doe>real</v>"))
    assert document.metrics["cue_count"] == 2
    assert document.metrics["empty_text_cue_count"] == 1
    assert document.cues[0].text == ""
    assert document.cues[0].metadata["empty_text"] is True
    assert document.status == PARSED_WITH_WARNINGS
    assert "EMPTY_CUE_TEXT" in codes(document)


def test_cue_text_hash_is_the_hash_of_the_canonical_text():
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Hello there.</v>"))
    assert document.cues[0].cue_text_sha256 == content_sha256(b"Hello there.")


# --- ordering and overlap ---------------------------------------------------------


def test_cues_keep_file_order_and_are_indexed_from_one():
    document = parse_combined_webvtt(vtt(
        *[f"00:00:0{n}.000 --> 00:00:0{n + 1}.000\nline {n}" for n in range(1, 6)]))
    assert [c.cue_index for c in document.cues] == [1, 2, 3, 4, 5]
    assert all(document.cues[i].start_ms <= document.cues[i + 1].start_ms
               for i in range(len(document.cues) - 1))


def test_overlapping_cues_are_preserved_and_reported_not_a_warning():
    """Two people talking over each other is ordinary Teams output."""
    document = parse_combined_webvtt(vtt(
        "00:00:01.000 --> 00:00:10.000\n<v Jane Doe>long</v>",
        "00:00:05.000 --> 00:00:07.000\n<v Ali Hassan>interjection</v>"))
    assert document.status == PARSED
    assert document.warnings == []
    assert document.metrics["overlapping_cue_count"] == 1
    assert document.cues[1].metadata["overlaps_previous_cue"] is True
    # Timings untouched.
    assert (document.cues[1].start_ms, document.cues[1].end_ms) == (5000, 7000)


def test_a_cue_starting_before_the_previous_one_is_flagged_not_reordered():
    document = parse_combined_webvtt(vtt(
        "00:00:10.000 --> 00:00:12.000\nsecond in time",
        "00:00:01.000 --> 00:00:02.000\nfirst in time"))
    assert "NON_MONOTONIC_CUE_START" in codes(document)
    assert document.metrics["non_monotonic_cue_count"] == 1
    # File order is preserved; the parser does not silently sort.
    assert [c.start_ms for c in document.cues] == [10_000, 1_000]


# --- document statuses -------------------------------------------------------------


def test_empty_content_is_empty_transcript():
    for value in (None, "", "   \n\n"):
        assert parse_combined_webvtt(value).status == EMPTY_TRANSCRIPT


def test_header_only_content_is_empty_transcript():
    assert parse_combined_webvtt("WEBVTT\n\n").status == EMPTY_TRANSCRIPT


def test_missing_header_is_invalid_webvtt():
    document = parse_combined_webvtt("00:00:01.000 --> 00:00:02.000\nno header\n")
    assert document.status == INVALID_WEBVTT
    assert codes(document) == ["MISSING_WEBVTT_HEADER"]
    assert document.cues == []


def test_header_with_a_byte_order_mark_is_accepted():
    assert parse_combined_webvtt(
        "﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n").status == PARSED


def test_structure_with_no_parsable_cue_at_all_is_unsupported():
    document = parse_combined_webvtt(vtt("no timing here", "nor here"))
    assert document.status == UNSUPPORTED_STRUCTURE
    assert document.metrics["cue_count"] == 0
    assert document.metrics["malformed_block_count"] == 2


def test_status_never_disagrees_with_the_warning_count():
    clean = parse_combined_webvtt(vtt("00:00:01.000 --> 00:00:02.000\nfine"))
    assert (clean.status, clean.metrics["warning_count"]) == (PARSED, 0)
    noisy = parse_combined_webvtt(vtt("no timing", "00:00:01.000 --> 00:00:02.000\nfine"))
    assert noisy.status == PARSED_WITH_WARNINGS
    assert noisy.metrics["warning_count"] >= 1


# --- determinism and safety ----------------------------------------------------------


def test_parsing_is_deterministic_for_identical_input():
    source = vtt(
        "00:00:01.000 --> 00:00:02.000\n<v Jane Doe>one</v>",
        "00:00:03.000 --> 00:00:04.000\n<v Ali Hassan>two</v>")
    first, second = parse_combined_webvtt(source), parse_combined_webvtt(source)
    assert [(c.cue_index, c.start_ms, c.end_ms, c.speaker_label_raw, c.cue_text_sha256)
            for c in first.cues] == \
           [(c.cue_index, c.start_ms, c.end_ms, c.speaker_label_raw, c.cue_text_sha256)
            for c in second.cues]
    assert first.metrics == second.metrics


def test_parser_version_is_stable():
    assert PARSER_VERSION == "webvtt_canonical_v1"


def test_parsing_never_logs_cue_text_or_speaker_names(caplog):
    secret = "Extremely confidential lecture content"
    with caplog.at_level(logging.DEBUG):
        document = parse_combined_webvtt(vtt(
            f"00:00:01.000 --> 00:00:02.000\n<v Jane Doe>{secret}</v>"))
    emitted = "\n".join(
        record.getMessage() + str(getattr(record, "fields", "")) for record in caplog.records)
    assert secret not in emitted
    assert "Jane Doe" not in emitted
    # Warnings carry codes and counts, never text.
    assert all("text" not in item for item in document.warnings)
