"""
Phase 2B WebVTT combination parity.

Ported from the authoritative legacy export node "Combine Transcripts" in
automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.transcripts.combine import (
    combine_transcript_parts,
    format_timestamp,
    scan_vtt_range_ms,
    shift_vtt_timestamps,
    strip_webvtt_header,
)


T0 = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)

PART_ONE = (
    "WEBVTT\n"
    "\n"
    "00:00:10.000 --> 00:00:14.500\n"
    "<v Jane Doe>Welcome everyone.</v>\n"
    "\n"
    "00:01:00.000 --> 00:01:05.250\n"
    "<v Ali Hassan>Thanks, glad to be here.</v>\n"
)
PART_TWO = (
    "WEBVTT\n"
    "\n"
    "00:00:05.000 --> 00:00:09.750\n"
    "<v Jane Doe>Sorry, we dropped out.</v>\n"
)


# --- header and timestamp primitives -----------------------------------------


def test_webvtt_header_is_stripped():
    assert strip_webvtt_header(PART_ONE).startswith("00:00:10.000")
    assert "WEBVTT" not in strip_webvtt_header(PART_ONE)
    assert strip_webvtt_header("﻿WEBVTT - some title\r\n\r\n00:00:01.000 --> 00:00:02.000\nHi") \
        .startswith("00:00:01.000")


def test_timestamps_beyond_twenty_three_hours_are_supported():
    assert format_timestamp(25 * 3600 * 1000 + 61_500) == "25:01:01.500"
    shifted = shift_vtt_timestamps(
        "WEBVTT\n\n99:59:59.999 --> 100:00:01.000\nlate cue\n", 0)
    assert "99:59:59.999 --> 100:00:01.000" in shifted


def test_comma_millisecond_separator_is_accepted_and_normalized_to_a_dot():
    shifted = shift_vtt_timestamps("WEBVTT\n\n00:00:01,250 --> 00:00:02,500\ncue\n", 0)
    assert "00:00:01.250 --> 00:00:02.500" in shifted


def test_millisecond_accuracy_is_preserved():
    shifted = shift_vtt_timestamps("WEBVTT\n\n00:00:01.001 --> 00:00:02.999\ncue\n", 0)
    assert "00:00:01.001 --> 00:00:02.999" in shifted


def test_shifting_moves_every_cue_by_the_offset():
    shifted = shift_vtt_timestamps(PART_ONE, 90_000)
    assert "00:01:40.000 --> 00:01:44.500" in shifted
    assert "00:02:30.000 --> 00:02:35.250" in shifted
    # Wording is never touched.
    assert "<v Jane Doe>Welcome everyone.</v>" in shifted


def test_scan_range_reads_the_lowest_and_highest_cue():
    lowest, highest = scan_vtt_range_ms(PART_ONE)
    assert (lowest, highest) == (10_000, 65_250)


# --- combination ---------------------------------------------------------------


def test_single_part_keeps_its_own_timeline():
    result = combine_transcript_parts([(T0, PART_ONE)])
    assert result.parts_combined == 1
    assert result.combined is False
    assert result.part_offsets_ms == [0]
    assert result.text.startswith("WEBVTT\n\n00:00:10.000 --> 00:00:14.500")
    assert result.text.count("WEBVTT") == 1


def test_multi_part_timeline_is_shifted_onto_one_session_clock():
    """
    The second part restarts near 00:00 in provider time. Concatenating it
    unshifted would overlap part one; the offset is its real start delta.
    """
    second_start = T0 + timedelta(minutes=30)
    result = combine_transcript_parts([(T0, PART_ONE), (second_start, PART_TWO)])

    assert result.parts_combined == 2
    assert result.combined is True
    assert result.part_offsets_ms == [0, 1_800_000]
    # Part one unchanged, part two pushed to 30 minutes + its own 5s.
    assert "00:00:10.000 --> 00:00:14.500" in result.text
    assert "00:30:05.000 --> 00:30:09.750" in result.text
    # Exactly one header, and no part timeline restarts.
    assert result.text.count("WEBVTT") == 1
    assert "00:00:05.000 --> 00:00:09.750" not in result.text


def test_parts_are_ordered_by_real_provider_time_not_input_order():
    later = T0 + timedelta(minutes=30)
    out_of_order = combine_transcript_parts([(later, PART_TWO), (T0, PART_ONE)])
    in_order = combine_transcript_parts([(T0, PART_ONE), (later, PART_TWO)])
    assert out_of_order.text == in_order.text
    assert out_of_order.text.index("Welcome everyone") < out_of_order.text.index("we dropped out")


def test_combined_duration_spans_the_whole_session():
    second_start = T0 + timedelta(minutes=30)
    result = combine_transcript_parts([(T0, PART_ONE), (second_start, PART_TWO)])
    # 00:00:10.000 .. 00:30:09.750
    assert result.range_start_ms == 10_000
    assert result.range_end_ms == 1_809_750
    assert result.duration_seconds == pytest.approx(1799.75)
    assert result.duration_minutes == 30


def test_single_part_duration_matches_its_own_span():
    result = combine_transcript_parts([(T0, PART_ONE)])
    assert result.duration_seconds == pytest.approx(55.25)
    assert result.duration_minutes == 1


def test_empty_and_untimed_parts_are_counted_as_failed_not_combined():
    result = combine_transcript_parts([(T0, PART_ONE), (T0 + timedelta(minutes=5), "   ")])
    assert result.parts_combined == 1
    assert result.parts_failed == 1


def test_no_usable_parts_raises_rather_than_inventing_a_transcript():
    with pytest.raises(ValueError, match="no valid transcript parts"):
        combine_transcript_parts([(T0, "")])


def test_content_without_readable_cue_timings_raises():
    with pytest.raises(ValueError, match="no readable cue timings"):
        combine_transcript_parts([(T0, "WEBVTT\n\nNOTE just a note, no cues\n")])


def test_combination_is_deterministic_for_identical_inputs():
    later = T0 + timedelta(minutes=30)
    first = combine_transcript_parts([(T0, PART_ONE), (later, PART_TWO)])
    second = combine_transcript_parts([(T0, PART_ONE), (later, PART_TWO)])
    assert first.text == second.text
    assert first.duration_seconds == second.duration_seconds


def test_speaker_text_and_cue_count_are_never_altered():
    later = T0 + timedelta(minutes=30)
    result = combine_transcript_parts([(T0, PART_ONE), (later, PART_TWO)])
    assert result.text.count("-->") == 3          # every cue survives
    for line in ("<v Jane Doe>Welcome everyone.</v>",
                 "<v Ali Hassan>Thanks, glad to be here.</v>",
                 "<v Jane Doe>Sorry, we dropped out.</v>"):
        assert line in result.text
