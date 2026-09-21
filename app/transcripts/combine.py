"""
Phase 2B: multi-part WebVTT combination.

A direct port of the authoritative legacy n8n node "Combine Transcripts" from
automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json.

The output is DERIVED data. It is never written back over Phase 2A raw
evidence, which stays immutable and hash-verifiable.

Transcript wording is never touched: only cue TIMESTAMPS are rewritten, by a
whole-part offset, so that parts recorded as separate Teams calls land on one
continuous session timeline instead of all restarting near 00:00.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime


# Matches a cue timing line. Hours may exceed 23 and either '.' or ',' is
# accepted as the millisecond separator, exactly as the legacy regex allows.
TIMING = re.compile(
    r"(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})(\s*-->\s*)(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})"
)
HEADER = re.compile(r"^﻿?\s*WEBVTT[^\r\n]*(?:\r?\n)+", re.IGNORECASE)


@dataclass(frozen=True)
class CombinedTranscript:
    text: str
    duration_minutes: int
    duration_seconds: float
    parts_combined: int
    parts_failed: int
    combined: bool
    part_offsets_ms: list[int]
    range_start_ms: int | None
    range_end_ms: int | None


def strip_webvtt_header(value: str) -> str:
    return HEADER.sub("", value or "").strip()


def timestamp_to_ms(hours, minutes, seconds, milliseconds) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(milliseconds)


def format_timestamp(total_ms: float) -> str:
    """HH:MM:SS.mmm, with hours allowed past 23 and never negative."""
    safe = max(0, int(math.floor(total_ms + 0.5)))
    hours, remainder = divmod(safe, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def shift_vtt_timestamps(vtt: str, offset_ms: int) -> str:
    body = strip_webvtt_header(vtt)

    def replace(match: re.Match) -> str:
        start = timestamp_to_ms(*match.group(1, 2, 3, 4))
        end = timestamp_to_ms(*match.group(6, 7, 8, 9))
        return format_timestamp(start + offset_ms) + match.group(5) + format_timestamp(end + offset_ms)

    return TIMING.sub(replace, body)


def scan_vtt_range_ms(vtt: str) -> tuple[int | None, int | None]:
    lowest: int | None = None
    highest = 0
    for match in TIMING.finditer(vtt):
        start = timestamp_to_ms(*match.group(1, 2, 3, 4))
        end = timestamp_to_ms(*match.group(6, 7, 8, 9))
        lowest = start if lowest is None else min(lowest, start)
        highest = max(highest, end)
    return (None, None) if lowest is None else (lowest, highest)


def combine_transcript_parts(parts: list[tuple[datetime, str]]) -> CombinedTranscript:
    """
    Combine `(provider_created_at, raw_vtt)` parts onto one session timeline.

    Steps, in the legacy order: drop empty/untimed parts, sort by the real
    Microsoft created time, take the earliest as the session origin, strip each
    part's own WEBVTT header, shift its cues by `created - origin`, concatenate
    chronologically under exactly one header, then measure the combined range.
    """
    valid = [
        (created, text) for created, text in parts
        if created is not None and isinstance(text, str) and text.strip()
    ]
    failed = len(parts) - len(valid)
    if not valid:
        raise ValueError("no valid transcript parts to combine")

    valid.sort(key=lambda item: item[0])
    session_start = valid[0][0]

    offsets: list[int] = []
    sections: list[str] = []
    for created, text in valid:
        offset_ms = int(round((created - session_start).total_seconds() * 1000))
        offsets.append(offset_ms)
        shifted = shift_vtt_timestamps(text, offset_ms)
        if shifted:
            sections.append(shifted)

    combined_body = "\n\n".join(sections)
    combined_text = f"WEBVTT\n\n{combined_body}".strip()

    lowest, highest = scan_vtt_range_ms(combined_text)
    if lowest is None or highest is None or highest < lowest:
        raise ValueError("combined transcript contains no readable cue timings")
    duration_ms = highest - lowest
    duration_minutes = int(math.floor(duration_ms / 60000 + 0.5))
    if duration_minutes <= 0:
        raise ValueError(f"invalid combined transcript duration: {duration_minutes}")

    return CombinedTranscript(
        text=combined_text,
        duration_minutes=duration_minutes,
        duration_seconds=duration_ms / 1000,
        parts_combined=len(valid),
        parts_failed=failed,
        combined=len(valid) > 1,
        part_offsets_ms=offsets,
        range_start_ms=lowest,
        range_end_ms=highest,
    )
