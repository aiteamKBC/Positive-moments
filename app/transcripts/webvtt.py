"""
Phase 2C1: canonical WebVTT cue parser.

INPUT is the Phase 2B derived combined transcript, never a raw Phase 2A
artifact and never Microsoft Graph. OUTPUT is a deterministic cue list: same
bytes plus same parser version always produce the same cues, in the same order,
with the same hashes.

PARSER REUSE. The timing/header primitives are imported from
`app.transcripts.combine`, which is the Phase 2B VTT module, so the platform has
exactly ONE timestamp convention. `automation/lecture_parts/planner.parse_webvtt`
is deliberately NOT reused and NOT modified: it drops empty-text cues (which
renumbers cue IDs and silently loses evidence), raises instead of reporting a
status, and reports no malformed/overlap diagnostics. Those are reasonable
choices for the AI split planner it feeds, and wrong for a canonical evidence
layer. The Lecture Parts pipeline keeps using it, unchanged.

Cue timestamps are OFFSETS on the combined transcript timeline, in whole
milliseconds. They are not wall-clock instants and carry no timezone.
"""
import html
import re
from dataclasses import dataclass, field
from typing import Any

from app.common.hashing import content_sha256
from app.transcripts.combine import HEADER, TIMING, timestamp_to_ms


PARSER_VERSION = "webvtt_canonical_v1"

# Parse outcomes.
PARSED = "PARSED"
PARSED_WITH_WARNINGS = "PARSED_WITH_WARNINGS"
EMPTY_TRANSCRIPT = "EMPTY_TRANSCRIPT"
INVALID_WEBVTT = "INVALID_WEBVTT"
UNSUPPORTED_STRUCTURE = "UNSUPPORTED_STRUCTURE"
ERROR = "ERROR"

# <v Speaker Name> ... optionally closed by </v>. Microsoft emits one per cue.
VOICE_OPEN = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>", re.IGNORECASE)
VOICE_ANY = re.compile(r"</?v(?:\.[^\s>]*)?(?:\s+[^>]*)?>", re.IGNORECASE)
ANY_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?/?>")
BLANK_LINE = re.compile(r"\r?\n[ \t]*\r?\n")


@dataclass(frozen=True)
class ParsedCue:
    cue_index: int
    start_ms: int
    end_ms: int
    speaker_label_raw: str | None
    text: str
    cue_text_sha256: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    status: str
    cues: list[ParsedCue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _clean_text(body: str) -> tuple[str, list[str]]:
    """
    Reduce cue markup to canonical spoken text.

    Only WebVTT markup is removed. Wording is never summarized, rewritten,
    spell-corrected, translated, or trimmed of meaningful words. Any tag other
    than the voice tag is recorded as a diagnostic rather than silently dropped
    without trace.
    """
    other_tags = sorted({
        match.group(1).lower() for match in ANY_TAG.finditer(body)
        if match.group(1).lower() != "v"
    })
    without_voice = VOICE_ANY.sub("", body)
    without_tags = ANY_TAG.sub("", without_voice)
    return html.unescape(without_tags).strip(), other_tags


def _speaker(body: str) -> tuple[str | None, list[str]]:
    """
    Raw provider speaker label only. No identity inference of any kind.

    One distinct label wins. Several conflicting labels in one cue resolve to
    NULL plus a recorded anomaly, because picking one would invent a speaker.
    """
    labels = [match.group(1).strip() for match in VOICE_OPEN.finditer(body)]
    labels = [label for label in labels if label]
    distinct = list(dict.fromkeys(labels))
    if len(distinct) == 1:
        return distinct[0], distinct
    return None, distinct


def parse_combined_webvtt(content: str) -> ParsedDocument:
    """Parse one combined transcript into canonical cues. Deterministic."""
    metrics = {
        "cue_count": 0, "empty_text_cue_count": 0, "overlapping_cue_count": 0,
        "malformed_block_count": 0, "multi_speaker_cue_count": 0,
        "non_monotonic_cue_count": 0, "zero_length_cue_count": 0,
        "unique_raw_speaker_label_count": 0, "cue_identifier_count": 0,
        "other_markup_tag_count": 0,
    }
    warnings: list[dict[str, Any]] = []

    if content is None or not content.strip():
        return ParsedDocument(EMPTY_TRANSCRIPT, metrics=metrics)

    text = content.lstrip("﻿")
    if not re.match(r"^\s*WEBVTT", text, re.IGNORECASE):
        return ParsedDocument(
            INVALID_WEBVTT, metrics=metrics,
            warnings=[{"code": "MISSING_WEBVTT_HEADER"}])

    body = HEADER.sub("", text, count=1)
    cues: list[ParsedCue] = []
    labels_seen: set[str] = set()
    previous_end: int | None = None
    previous_start: int | None = None

    for block_number, block in enumerate(BLANK_LINE.split(body)):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_line = next((index for index, line in enumerate(lines)
                            if TIMING.search(line)), None)
        if timing_line is None:
            # A NOTE/STYLE/REGION block is structural, not a broken cue.
            if lines[0].strip().upper().startswith(("NOTE", "STYLE", "REGION")):
                continue
            metrics["malformed_block_count"] += 1
            warnings.append({"code": "BLOCK_WITHOUT_TIMING", "block": block_number})
            continue

        match = TIMING.search(lines[timing_line])
        start_ms = timestamp_to_ms(*match.group(1, 2, 3, 4))
        end_ms = timestamp_to_ms(*match.group(6, 7, 8, 9))
        cue_metadata: dict[str, Any] = {}

        if timing_line > 0:
            metrics["cue_identifier_count"] += 1
            cue_metadata["cue_identifier"] = lines[timing_line - 1].strip()
        settings = lines[timing_line][match.end():].strip()
        if settings:
            cue_metadata["cue_settings"] = settings

        if end_ms < start_ms:
            # Cannot be stored: the table requires end_ms >= start_ms. Reported
            # rather than silently "repaired" by swapping the timestamps.
            metrics["malformed_block_count"] += 1
            warnings.append({"code": "END_BEFORE_START", "block": block_number,
                             "start_ms": start_ms, "end_ms": end_ms})
            continue

        raw_body = "\n".join(lines[timing_line + 1:])
        speaker, distinct_labels = _speaker(raw_body)
        spoken, other_tags = _clean_text(raw_body)

        if len(distinct_labels) > 1:
            metrics["multi_speaker_cue_count"] += 1
            cue_metadata["multiple_speaker_labels"] = distinct_labels
            warnings.append({"code": "MULTIPLE_SPEAKER_LABELS",
                             "cue_index": len(cues) + 1,
                             "label_count": len(distinct_labels)})
        if other_tags:
            metrics["other_markup_tag_count"] += 1
            cue_metadata["other_markup_tags"] = other_tags
        if not spoken:
            metrics["empty_text_cue_count"] += 1
            cue_metadata["empty_text"] = True
        if end_ms == start_ms:
            metrics["zero_length_cue_count"] += 1
        if previous_end is not None and start_ms < previous_end:
            # Overlap is ordinary in speaker-attributed Teams transcripts (two
            # people talking over each other). It is counted and flagged on the
            # cue, but it is NOT a warning and never "fixed" by shifting times.
            metrics["overlapping_cue_count"] += 1
            cue_metadata["overlaps_previous_cue"] = True
        if previous_start is not None and start_ms < previous_start:
            metrics["non_monotonic_cue_count"] += 1
            cue_metadata["starts_before_previous_cue"] = True
            warnings.append({"code": "NON_MONOTONIC_CUE_START",
                             "cue_index": len(cues) + 1})

        if speaker:
            labels_seen.add(speaker)
        cues.append(ParsedCue(
            cue_index=len(cues) + 1, start_ms=start_ms, end_ms=end_ms,
            speaker_label_raw=speaker, text=spoken,
            cue_text_sha256=content_sha256(spoken.encode("utf-8")),
            metadata=cue_metadata,
        ))
        previous_end = max(previous_end or 0, end_ms)
        previous_start = start_ms

    metrics["cue_count"] = len(cues)
    metrics["unique_raw_speaker_label_count"] = len(labels_seen)
    if cues:
        metrics["first_cue_start_ms"] = min(cue.start_ms for cue in cues)
        metrics["last_cue_end_ms"] = max(cue.end_ms for cue in cues)
        metrics["duration_ms"] = metrics["last_cue_end_ms"] - metrics["first_cue_start_ms"]
    else:
        metrics["first_cue_start_ms"] = None
        metrics["last_cue_end_ms"] = None
        metrics["duration_ms"] = None

    if metrics["empty_text_cue_count"]:
        # One aggregate entry rather than one per cue, so the warning list stays
        # bounded on a long transcript.
        warnings.append({"code": "EMPTY_CUE_TEXT",
                         "cue_count": metrics["empty_text_cue_count"]})

    # Status is derived purely from the warning list, so warning_count can never
    # disagree with the status.
    if not cues:
        status = UNSUPPORTED_STRUCTURE if metrics["malformed_block_count"] else EMPTY_TRANSCRIPT
    elif warnings:
        status = PARSED_WITH_WARNINGS
    else:
        status = PARSED
    metrics["warning_count"] = len(warnings)
    return ParsedDocument(status, cues=cues, metrics=metrics, warnings=warnings)
