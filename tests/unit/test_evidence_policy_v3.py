"""
Evidence policy v3: MM:SS.mmm is read as hour zero, and a timestamp nothing
can read is excluded as empty evidence instead of failing the evaluation.

The fixtures are the REAL 2026-10-16 Ray MSP shapes: gpt-5.2 wrote every
sub-hour clip as `49:05.214`, one clip as `04:18:480`, and every clip past the
first hour correctly as `01:35:29.694`. 29 of 37 clips were rejected as
UNPARSEABLE_TIMESTAMP, twice, and the lecture went to review with a verdict
that was otherwise complete.
"""
import pytest

from app.qa.evidence_policy import (
    EVIDENCE_POLICY_V1,
    EVIDENCE_POLICY_V2,
    EVIDENCE_POLICY_V3,
    assess,
    is_fatal,
    is_renderable,
    reads_minutes_seconds,
)
from app.qa.validation import (
    NO_CUE_OVERLAP,
    OUT_OF_RANGE,
    UNPARSEABLE,
    VALID,
    parse_timestamp_ms,
    validate_clip,
)


# A two-hour lecture with speech throughout, bar one silent gap.
CUES = [(0, 3_000_000), (3_010_000, 7_600_000)]
DOCUMENT = {"document_start_ms": 0, "document_end_ms": 7_600_000, "cues": CUES}


def _verdict(start, end, *, short):
    return validate_clip({"start": start, "end": end}, **DOCUMENT,
                         allow_minutes_seconds=short)


# --- reading ---------------------------------------------------------------

def test_minutes_seconds_is_read_as_hour_zero_only_when_asked():
    assert parse_timestamp_ms("49:05.214") is None
    assert parse_timestamp_ms("49:05.214", allow_minutes_seconds=True) == 2_945_214
    assert (parse_timestamp_ms("49:05.214", allow_minutes_seconds=True)
            == parse_timestamp_ms("00:49:05.214"))


def test_the_full_form_reads_identically_either_way():
    for stamp in ("01:35:29.694", "00:00:05.250", "01:02:03"):
        assert (parse_timestamp_ms(stamp)
                == parse_timestamp_ms(stamp, allow_minutes_seconds=True))


@pytest.mark.parametrize("stamp", [
    "04:18:480",     # the real one: milliseconds after a colon
    "49:05",         # no decimal point: hours and minutes, or minutes and seconds?
    "75:12.300",     # minutes past 59 belong in the hour
    "49:61.000",     # no such second
    "9:05.214",      # one-digit minutes: not a shape the model was ever shown
    "five past two",
])
def test_only_the_unambiguous_short_form_is_read(stamp):
    assert parse_timestamp_ms(stamp, allow_minutes_seconds=True) is None


# --- a short-form clip faces every check a full one does ----------------------

def test_a_short_form_clip_inside_the_transcript_is_valid():
    verdict = _verdict("49:05.214", "49:23.214", short=True)
    assert verdict["status"] == VALID
    assert (verdict["start_ms"], verdict["end_ms"]) == (2_945_214, 2_963_214)


def test_a_short_form_clip_in_a_silent_gap_is_still_fabricated():
    verdict = _verdict("50:00.500", "50:09.500", short=True)
    assert verdict["status"] == NO_CUE_OVERLAP


def test_a_short_form_clip_past_the_transcript_is_still_fabricated():
    short_document = {**DOCUMENT, "document_end_ms": 600_000}
    verdict = validate_clip({"start": "49:05.214", "end": "49:23.214"},
                            **short_document, allow_minutes_seconds=True)
    assert verdict["status"] == OUT_OF_RANGE


def test_without_the_flag_the_validator_is_exactly_as_it_was():
    assert _verdict("49:05.214", "49:23.214", short=False)["status"] == UNPARSEABLE


# --- what each policy makes of it -------------------------------------------

def test_only_v3_reads_the_short_form():
    assert reads_minutes_seconds(EVIDENCE_POLICY_V3) is True
    assert reads_minutes_seconds(EVIDENCE_POLICY_V2) is False
    assert reads_minutes_seconds(EVIDENCE_POLICY_V1) is False


def test_the_ray_answer_stands_under_v3_and_fails_under_v1_and_v2():
    """
    The real outcome counts. Under v2: 8 valid, 29 unparseable, rejected.
    Under v3 the 28 short-form clips are read and the one `04:18:480` is
    excluded.
    """
    v2 = [VALID] * 8 + [UNPARSEABLE] * 29
    v3 = [VALID] * 36 + [UNPARSEABLE]
    for policy in (EVIDENCE_POLICY_V1, EVIDENCE_POLICY_V2):
        assert assess(v2, policy=policy)["evaluation_usable"] is False
    result = assess(v3, policy=EVIDENCE_POLICY_V3)
    assert result["evaluation_usable"] is True
    assert result["excluded_reasons"] == {UNPARSEABLE: 1}
    assert result["fatal_clip_count"] == 0


def test_an_unreadable_clip_is_excluded_under_v3_and_never_renders():
    assert is_fatal(UNPARSEABLE, policy=EVIDENCE_POLICY_V3) is False
    assert is_fatal(UNPARSEABLE, policy=EVIDENCE_POLICY_V2) is True
    assert is_fatal(UNPARSEABLE, policy=EVIDENCE_POLICY_V1) is True
    assert is_renderable(UNPARSEABLE, policy=EVIDENCE_POLICY_V3) is False


def test_a_fabricated_coordinate_still_fails_under_v3():
    for status in (OUT_OF_RANGE, NO_CUE_OVERLAP):
        result = assess([VALID] * 36 + [UNPARSEABLE, status],
                        policy=EVIDENCE_POLICY_V3)
        assert result["evaluation_usable"] is False, status
        assert result["fatal_reasons"] == {status: 1}
