"""
Teams WebVTT -> Bunny caption publication copy.

The ONLY changes made to the stored Microsoft transcript:
  * `<v Speaker Name>` / `</v>` voice spans are removed (speaker identity);
  * cue identifiers and NOTE/STYLE/REGION blocks are dropped (metadata);
  * line endings become LF, one `WEBVTT` header, UTF-8 without BOM;
  * cue times are shifted by a PROVEN offset, which is 0 when media time zero
    is the transcript's own start (the Phase 6A / alignment.py result).
Spoken text, cue order, overlaps and millisecond timings are preserved. The
result is re-parsed by the canonical parser and compared cue-by-cue against the
source before it may be published.

This is NOT a redaction step: every spoken word stays, including any name a
trainer or learner says aloud. Only speaker-attribution METADATA (the voice
tags) is removed, and validation proves exactly that - it never scans the
spoken text for participant names.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.transcripts.combine import TIMING, timestamp_to_ms
from app.transcripts.webvtt import PARSED, PARSER_VERSION, parse_combined_webvtt

TRANSFORM_VERSION = "teams_vtt_strip_voice_v1"
# Teams may stamp the final cue a fraction of a second past the recording's end; never more than this.
CAPTION_TAIL_TOLERANCE_SECONDS = 1.0

_VOICE_TAG = re.compile(r"</?v(?:\.[^\s>]*)?(?:\s+[^>]*)?>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)(?:[\s.][^>]*)?/?>")
_HEADER = re.compile(r"^﻿?\s*WEBVTT[^\n]*(?:\n)+")
_BLOCKS = re.compile(r"\n[ \t]*\n")
_STRICT_TIMING = re.compile(r"\d{2,}:\d{2}:\d{2}\.\d{3} --> \d{2,}:\d{2}:\d{2}\.\d{3}")


_SERVED_TIMING = re.compile(r"(\d{2,}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2,}):(\d{2}):(\d{2})\.(\d{3})(?: (\S.*))?")

# Read-back verdicts (compare_published_caption).
IDENTICAL, EQUIVALENT, MISMATCH = "IDENTICAL", "EQUIVALENT", "MISMATCH"


class CaptionBuildError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class _Unparseable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CaptionBuild:
    text: str
    sha256: str
    size_bytes: int
    source_sha256: str
    source_cue_count: int
    cue_count: int
    removed_voice_tags: int
    applied_offset_ms: int
    transform_version: str = TRANSFORM_VERSION

    def __repr__(self) -> str:                      # never print caption text
        return (f"CaptionBuild(sha256={self.sha256[:12]}, cues={self.cue_count}, "
                f"offset_ms={self.applied_offset_ms})")


@dataclass
class CaptionValidation:
    ok: bool
    checks: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def build_caption(raw_vtt: str, *, applied_offset_ms: int = 0) -> CaptionBuild:
    """Sanitized publication copy of one stored Teams WebVTT part."""
    if not isinstance(raw_vtt, str) or not raw_vtt.lstrip("﻿").lstrip().startswith("WEBVTT"):
        raise CaptionBuildError("SOURCE_NOT_WEBVTT", "source has no WEBVTT header")
    source = parse_combined_webvtt(raw_vtt)
    if source.status != PARSED:
        raise CaptionBuildError("SOURCE_PARSE_" + source.status,
                                f"source transcript did not parse cleanly ({len(source.warnings)} warnings)")
    body = _HEADER.sub("", raw_vtt.replace("\r\n", "\n").replace("\r", "\n"), count=1)
    blocks, removed = [], 0
    for block in _BLOCKS.split(body):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].lstrip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if TIMING.search(line)), None)
        if timing_index is None:
            raise CaptionBuildError("SOURCE_BLOCK_WITHOUT_TIMING", "a non-metadata block has no cue timing")
        match = TIMING.search(lines[timing_index])
        start = timestamp_to_ms(*match.group(1, 2, 3, 4)) + applied_offset_ms
        end = timestamp_to_ms(*match.group(6, 7, 8, 9)) + applied_offset_ms
        if start < 0:
            raise CaptionBuildError("NEGATIVE_CUE_AFTER_OFFSET",
                                    "the offset would move a cue before media time zero")
        settings = lines[timing_index][match.end():].strip()
        timing = f"{_format(start)} --> {_format(end)}" + (f" {settings}" if settings else "")
        text_lines = []
        for line in lines[timing_index + 1:]:
            removed += len(_VOICE_TAG.findall(line))
            stripped = _VOICE_TAG.sub("", line).strip()
            if stripped:
                text_lines.append(stripped)
        blocks.append("\n".join([timing] + text_lines))
    text = "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
    return CaptionBuild(text=text, sha256=sha256_text(text), size_bytes=len(text.encode("utf-8")),
                        source_sha256=sha256_text(raw_vtt), source_cue_count=len(source.cues),
                        cue_count=len(blocks), removed_voice_tags=removed,
                        applied_offset_ms=applied_offset_ms)


def _published_cues(text: str) -> tuple[list[tuple], dict]:
    """Strictly parse a publication-format WebVTT (what build_caption writes, as Bunny may serve it).

    Tolerated, because Bunny was observed to add them: ONE leading UTF-8 BOM, a cue identifier
    that is exactly the cue's 1-based position, and any number of trailing newlines. Anything
    else - CR line endings, a header other than `WEBVTT`, extra blank lines, NOTE/STYLE blocks,
    non-sequential identifiers, a loose timing format, blank or `-->` payload lines - raises.
    """
    facts = {"bom": text.startswith("﻿"), "cue_identifiers": 0}
    body = text[1:] if facts["bom"] else text
    if "\r" in body:
        raise _Unparseable("CARRIAGE_RETURN")
    stripped = body.rstrip("\n")
    facts["trailing_newlines"] = len(body) - len(stripped)
    if stripped == "WEBVTT":
        blocks = []
    elif stripped.startswith("WEBVTT\n\n"):
        blocks = stripped[len("WEBVTT\n\n"):].split("\n\n")
    else:
        raise _Unparseable("HEADER")
    cues = []
    for position, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if lines[0] == str(position) and len(lines) > 1:
            facts["cue_identifiers"] += 1
            lines = lines[1:]
        match = _SERVED_TIMING.fullmatch(lines[0])
        if match is None:
            raise _Unparseable("CUE_IDENTIFIER_OR_TIMING")
        payload = tuple(lines[1:])
        if any(not line.strip() or "-->" in line for line in payload):
            raise _Unparseable("CUE_PAYLOAD")
        cues.append((timestamp_to_ms(*match.group(1, 2, 3, 4)), timestamp_to_ms(*match.group(5, 6, 7, 8)),
                     match.group(9) or "", payload))
    return cues, facts


def compare_published_caption(expected: str, served: str) -> dict:
    """Cue-by-cue verdict on the caption Bunny serves versus the one we uploaded.

    IDENTICAL: byte-for-byte. EQUIVALENT: only the tolerated formatting differs and every cue's
    position, start, end, settings and every payload line are exactly equal. Otherwise MISMATCH.
    The result holds counts, positions and codes only - never caption text.
    """
    if served == expected:
        return {"result": IDENTICAL}
    try:
        ours, _ = _published_cues(expected)
    except _Unparseable as error:
        return {"result": MISMATCH, "reason": f"UPLOADED_UNPARSEABLE:{error.code}"}
    try:
        theirs, facts = _published_cues(served)
    except _Unparseable as error:
        return {"result": MISMATCH, "reason": f"SERVED_UNPARSEABLE:{error.code}"}
    found = {"uploaded_cues": len(ours), "served_cues": len(theirs), "served_bom": facts["bom"],
             "served_cue_identifiers": facts["cue_identifiers"],
             "served_trailing_newlines": facts["trailing_newlines"]}
    for position, (a, b) in enumerate(zip(ours, theirs), 1):
        difference = ("TIMING" if a[:2] != b[:2] else "SETTINGS" if a[2] != b[2]
                      else "TEXT" if a[3] != b[3] else None)
        if difference:
            return {"result": MISMATCH, "reason": f"CUE_{difference}", "first_difference_cue": position, **found}
    if len(ours) != len(theirs):
        return {"result": MISMATCH, "reason": "CUE_COUNT", "first_difference_cue": min(len(ours), len(theirs)) + 1,
                **found}
    return {"result": EQUIVALENT, **found}


def _spoken_lines(vtt: str) -> list[tuple]:
    """Per cue, its payload lines exactly as written, minus voice tags - no unescaping, no rewording."""
    body = _HEADER.sub("", vtt.replace("\r\n", "\n").replace("\r", "\n"), count=1)
    cues = []
    for block in _BLOCKS.split(body):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].lstrip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if TIMING.search(line)), None)
        if timing_index is None:
            continue
        cues.append(tuple(text for text in (_VOICE_TAG.sub("", line).strip() for line in lines[timing_index + 1:])
                          if text))
    return cues


def validate_caption(build: CaptionBuild, raw_vtt: str, *,
                     media_duration_seconds: float | None,
                     tail_tolerance_seconds: float = CAPTION_TAIL_TOLERANCE_SECONDS) -> CaptionValidation:
    """
    Prove the publication copy is the source minus speaker METADATA, synchronised and in range.

    Spoken words are never inspected for names: a name said aloud is content and stays. What is
    proven instead is structural - no voice tag or other markup survives, the parser finds no
    speaker label in the output, and every cue's timing and every spoken line are exactly the
    source's (so nothing was injected, removed or reworded). The last cue may end at most
    `tail_tolerance_seconds` (capped at CAPTION_TAIL_TOLERANCE_SECONDS) after the MEASURED media
    duration; nothing is trimmed, shifted or clamped to get there.
    """
    source = parse_combined_webvtt(raw_vtt).cues
    parsed = parse_combined_webvtt(build.text)
    cues = parsed.cues
    offset = build.applied_offset_ms
    tail_tolerance_seconds = min(max(float(tail_tolerance_seconds), 0.0), CAPTION_TAIL_TOLERANCE_SECONDS)
    timing_lines = [line for line in build.text.split("\n") if "-->" in line]
    checks = {
        "parser": PARSER_VERSION,
        "parse_status": parsed.status,
        "cue_count_source": len(source),
        "cue_count_published": len(cues),
        "timings_and_text_identical_to_source": len(cues) == len(source) and all(
            out.start_ms == src.start_ms + offset and out.end_ms == src.end_ms + offset
            and out.text == src.text for out, src in zip(cues, source)),
        "voice_tags_remaining": len(re.findall(r"</?v[\s.>]", build.text)),
        "markup_tags_remaining": len(_ANY_TAG.findall(build.text)),
        "speaker_labels_remaining": parsed.metrics["unique_raw_speaker_label_count"],
        "spoken_lines_identical_to_source": _spoken_lines(build.text) == _spoken_lines(raw_vtt),
        "overlapping_cues_preserved": parsed.metrics["overlapping_cue_count"],
        "starts_non_decreasing": all(b.start_ms >= a.start_ms for a, b in zip(cues, cues[1:])),
        "every_end_gte_start": all(cue.end_ms >= cue.start_ms for cue in cues),
        "timing_format_strict": bool(timing_lines) and all(_STRICT_TIMING.fullmatch(line)
                                                           for line in timing_lines),
        "first_cue_start_ms": min((c.start_ms for c in cues), default=None),
        "last_cue_end_ms": max((c.end_ms for c in cues), default=None),
        "sha256_matches": sha256_text(build.text) == build.sha256,
        "source_sha256_matches": sha256_text(raw_vtt) == build.source_sha256,
    }
    if media_duration_seconds is not None and checks["last_cue_end_ms"] is not None:
        checks["media_duration_seconds"] = media_duration_seconds
        checks["last_cue_within_media"] = (checks["last_cue_end_ms"] / 1000.0
                                           <= media_duration_seconds + tail_tolerance_seconds)
        checks["last_cue_margin_seconds"] = round(media_duration_seconds - checks["last_cue_end_ms"] / 1000.0, 3)
        checks["tail_overrun_seconds"] = max(0.0, -checks["last_cue_margin_seconds"])
        checks["tail_tolerance_seconds"] = tail_tolerance_seconds
    required = {
        "parse_status": checks["parse_status"] == PARSED,
        "timings_and_text_identical_to_source": checks["timings_and_text_identical_to_source"],
        "spoken_lines_identical_to_source": checks["spoken_lines_identical_to_source"],
        "voice_tags_remaining": checks["voice_tags_remaining"] == 0,
        "markup_tags_remaining": checks["markup_tags_remaining"] == 0,
        "speaker_labels_remaining": checks["speaker_labels_remaining"] == 0,
        "starts_non_decreasing": checks["starts_non_decreasing"],
        "every_end_gte_start": checks["every_end_gte_start"],
        "timing_format_strict": checks["timing_format_strict"],
        "first_cue_not_negative": (checks["first_cue_start_ms"] or 0) >= 0 and bool(cues),
        "sha256_matches": checks["sha256_matches"],
        "source_sha256_matches": checks["source_sha256_matches"],
        "last_cue_within_media": checks.get("last_cue_within_media", False),
    }
    failures = [name for name, passed in required.items() if not passed]
    return CaptionValidation(ok=not failures, checks=checks, failures=failures)
