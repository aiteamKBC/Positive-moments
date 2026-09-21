"""
Application-side validation of the model's structured output.

The legacy workflow relied on n8n's structured output parser plus an
auto-fixing model. A parser that merely produces JSON is not enough here: a
response can parse cleanly and still be unusable (ten checklist rows, a
renamed item, an invented top-level key). So the schema is enforced again in
code, and the result is a list of explicit error codes rather than a boolean.

Evidence clips are checked against the canonical Phase 2C1 cue timeline: a
model that invents a timestamp outside the transcript must be caught, not
quietly repaired.
"""
import re

from app.qa.checklist import (
    CHECKLIST_ITEMS,
    CHECKLIST_ITEM_COUNT,
    CHECKLIST_STATUSES,
)
from app.qa.prompt import STRUCTURED_OUTPUT_SCHEMA


TOP_LEVEL_KEYS = tuple(STRUCTURED_OUTPUT_SCHEMA["required"])

EXACT = "EXACT"
WHITESPACE_ONLY = "WHITESPACE_ONLY"
MISMATCH = "MISMATCH"
MAX_CLIPS = 3
MAX_SUMMARY_ITEMS = 6
MAX_KSBS = 10
# System message: "Each evidence clip MUST have a duration of at least 2 seconds."
MIN_CLIP_MS = 2000
KSB_TYPES = ("Knowledge", "Skill", "Behaviour")

# HH:MM:SS.mmm, the only format the system message allows.
TIMESTAMP = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$")

VALID = "VALID"
OUT_OF_RANGE = "OUT_OF_TRANSCRIPT_RANGE"
NO_CUE_OVERLAP = "NO_CUE_OVERLAP"
TOO_SHORT = "SHORTER_THAN_MINIMUM"
NOT_POSITIVE = "END_NOT_AFTER_START"
UNPARSEABLE = "UNPARSEABLE_TIMESTAMP"


def checklist_item_match(returned, expected: str) -> str:
    """
    Compare a returned checklist item with the canonical string.

    The legacy system message states the eleven items TWICE - once under
    "CHECKLIST ITEMS (MUST MATCH EXACTLY)", where items 1, 2 and 10 carry
    trailing spaces, and once in the JSON template, where they do not. The
    model legitimately echoes either list, and the legacy production table
    holds both spellings for the same item on the same day. A difference of
    surrounding whitespace is therefore the prompt's ambiguity, not a
    malformed response: it is reported, not rejected. Any other difference
    still breaks output identity and is an error.
    """
    if not isinstance(returned, str):
        return MISMATCH
    if returned == expected:
        return EXACT
    if returned.strip() == expected.strip():
        return WHITESPACE_ONLY
    return MISMATCH


def checklist_item_whitespace_variants(payload) -> list[int]:
    """1-based positions whose item text differed only by surrounding whitespace."""
    rows = (payload or {}).get("checklist_evaluation")
    if not isinstance(rows, list):
        return []
    return [
        index + 1
        for index, row in enumerate(rows[:CHECKLIST_ITEM_COUNT])
        if isinstance(row, dict)
        and checklist_item_match(row.get("item"), CHECKLIST_ITEMS[index]) == WHITESPACE_ONLY
    ]


def parse_timestamp_ms(value) -> int | None:
    """HH:MM:SS(.mmm) -> milliseconds. Hours may exceed 24, as in WebVTT."""
    match = TIMESTAMP.match(str(value or "").strip())
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    total = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
    return total + int((millis or "0").ljust(3, "0"))


def validate_structured_output(payload) -> list[str]:
    """
    Return error codes for one model response; an empty list means valid.

    Checks the whole contract: top-level keys exactly, 11 checklist rows in
    the exact order with the exact strings, allowed statuses, clip limits,
    rating range and KSB types.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["OUTPUT_NOT_AN_OBJECT"]

    missing = [key for key in TOP_LEVEL_KEYS if key not in payload]
    extra = [key for key in payload if key not in TOP_LEVEL_KEYS]
    errors += [f"MISSING_TOP_LEVEL_KEY:{key}" for key in missing]
    errors += [f"UNEXPECTED_TOP_LEVEL_KEY:{key}" for key in extra]

    info = payload.get("session_info")
    if not isinstance(info, dict) or not all(key in info for key in ("trainer", "date")):
        errors.append("INVALID_SESSION_INFO")

    checklist = payload.get("checklist_evaluation")
    if not isinstance(checklist, list):
        errors.append("CHECKLIST_NOT_A_LIST")
    else:
        if len(checklist) != CHECKLIST_ITEM_COUNT:
            errors.append(f"CHECKLIST_COUNT:{len(checklist)}")
        for index, row in enumerate(checklist):
            position = index + 1
            if not isinstance(row, dict):
                errors.append(f"CHECKLIST_ROW_NOT_AN_OBJECT:{position}")
                continue
            if (index < CHECKLIST_ITEM_COUNT
                    and checklist_item_match(row.get("item"),
                                             CHECKLIST_ITEMS[index]) == MISMATCH):
                # Wrong string or wrong position: both break output identity.
                # Surrounding whitespace alone does not - see checklist_item_match.
                errors.append(f"CHECKLIST_ITEM_MISMATCH:{position}")
            if row.get("status") not in CHECKLIST_STATUSES:
                errors.append(f"CHECKLIST_STATUS_INVALID:{position}")
            errors += _clip_errors(row.get("evidence_clips"), f"checklist[{position}]")
            unexpected = set(row) - {"item", "status", "evidence_clips", "reasoning"}
            errors += [f"CHECKLIST_UNEXPECTED_KEY:{position}:{key}" for key in sorted(unexpected)]

    summary = payload.get("overall_summary")
    if not isinstance(summary, dict):
        errors.append("INVALID_OVERALL_SUMMARY")
    else:
        for field in ("strengths", "areas_for_improvement"):
            entries = summary.get(field)
            if not isinstance(entries, list):
                errors.append(f"INVALID_{field.upper()}")
                continue
            if len(entries) > MAX_SUMMARY_ITEMS:
                errors.append(f"TOO_MANY_{field.upper()}:{len(entries)}")
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict) or "title" not in entry:
                    errors.append(f"INVALID_{field.upper()}_ENTRY:{index}")
                    continue
                errors += _clip_errors(entry.get("evidence_clips"), f"{field}[{index}]")
        if not isinstance(summary.get("overall_judgement"), str):
            errors.append("INVALID_OVERALL_JUDGEMENT")

    ksbs = payload.get("ksbs_covered")
    if not isinstance(ksbs, list):
        errors.append("INVALID_KSBS")
    else:
        if len(ksbs) > MAX_KSBS:
            errors.append(f"TOO_MANY_KSBS:{len(ksbs)}")
        for index, entry in enumerate(ksbs, start=1):
            if not isinstance(entry, dict):
                errors.append(f"INVALID_KSB_ENTRY:{index}")
                continue
            if entry.get("type") not in KSB_TYPES:
                errors.append(f"INVALID_KSB_TYPE:{index}")
            errors += _clip_errors(entry.get("evidence_clips"), f"ksb[{index}]")

    quality = payload.get("teaching_quality")
    if not isinstance(quality, dict):
        errors.append("INVALID_TEACHING_QUALITY")
    else:
        rating = quality.get("rating_1_5")
        if not isinstance(rating, (int, float)) or isinstance(rating, bool) or not 1 <= rating <= 5:
            errors.append("INVALID_TEACHING_QUALITY_RATING")
        errors += _clip_errors(quality.get("evidence_clips"), "teaching_quality")
    return errors


def _clip_errors(clips, where: str) -> list[str]:
    if clips is None:
        return [f"MISSING_EVIDENCE_CLIPS:{where}"]
    if not isinstance(clips, list):
        return [f"EVIDENCE_CLIPS_NOT_A_LIST:{where}"]
    errors = []
    if len(clips) > MAX_CLIPS:
        errors.append(f"TOO_MANY_EVIDENCE_CLIPS:{where}:{len(clips)}")
    for index, clip in enumerate(clips, start=1):
        if not isinstance(clip, dict) or "start" not in clip or "end" not in clip:
            errors.append(f"INVALID_EVIDENCE_CLIP:{where}:{index}")
            continue
        unexpected = set(clip) - {"start", "end", "speaker"}
        errors += [f"EVIDENCE_CLIP_UNEXPECTED_KEY:{where}:{index}:{key}"
                   for key in sorted(unexpected)]
    return errors


def validate_clip(clip, *, document_start_ms: int, document_end_ms: int, cues) -> dict:
    """
    Check one evidence clip against the canonical transcript.

    `cues` is a sorted sequence of (start_ms, end_ms). A clip is valid when it
    parses, ends after it starts, lasts at least two seconds, sits inside the
    document range and overlaps at least one real cue. Invalid clips are
    reported with a status and never rewritten.
    """
    start_ms = parse_timestamp_ms((clip or {}).get("start"))
    end_ms = parse_timestamp_ms((clip or {}).get("end"))
    result = {"start_ms": start_ms, "end_ms": end_ms, "status": VALID,
              "overlapping_cue_count": 0}
    if start_ms is None or end_ms is None:
        result["status"] = UNPARSEABLE
        return result
    if end_ms <= start_ms:
        result["status"] = NOT_POSITIVE
        return result
    if start_ms < document_start_ms or end_ms > document_end_ms:
        # A timestamp outside the stored transcript cannot be evidence.
        result["status"] = OUT_OF_RANGE
        return result
    overlaps = sum(1 for cue_start, cue_end in cues
                   if cue_start < end_ms and cue_end > start_ms)
    result["overlapping_cue_count"] = overlaps
    if overlaps == 0:
        result["status"] = NO_CUE_OVERLAP
    elif end_ms - start_ms < MIN_CLIP_MS:
        result["status"] = TOO_SHORT
    return result


def collect_clips(payload) -> list[dict]:
    """Every evidence clip in a response, tagged with where it came from."""
    found: list[dict] = []

    def take(clips, source, position):
        for index, clip in enumerate(clips or [], start=1):
            if isinstance(clip, dict):
                found.append({"source": source, "source_position": position,
                              "clip_index": index, "clip": clip})

    for index, row in enumerate(payload.get("checklist_evaluation") or [], start=1):
        if isinstance(row, dict):
            take(row.get("evidence_clips"), "checklist", index)
    summary = payload.get("overall_summary") or {}
    for field in ("strengths", "areas_for_improvement"):
        for index, entry in enumerate(summary.get(field) or [], start=1):
            if isinstance(entry, dict):
                take(entry.get("evidence_clips"), field, index)
    for index, entry in enumerate(payload.get("ksbs_covered") or [], start=1):
        if isinstance(entry, dict):
            take(entry.get("evidence_clips"), "ksb", index)
    quality = payload.get("teaching_quality") or {}
    if isinstance(quality, dict):
        take(quality.get("evidence_clips"), "teaching_quality", 1)
    return found
