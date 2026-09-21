#!/usr/bin/env python
"""
Lecture three-part split planner (semantic_balanced_v1).

Division of responsibility, enforced by this module:

    AI   chooses TWO cue IDs from a supplied candidate list, and writes titles.
    CODE parses the transcript, builds the candidate windows, resolves cue IDs
         to real timestamps, validates every boundary, and computes all ranges.

The AI never sees or invents a timestamp. If its output fails any deterministic
check the plan is stored with planner_status='rejected' and ZERO media jobs are
produced.

This module is standalone: it does not import, modify or share code with the
positive-clip pipeline.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from typing import Iterable

SPLIT_VERSION = "semantic_balanced_v1"

# Target boundaries and the zones the AI is allowed to choose within.
CUT1_TARGET, CUT1_MIN, CUT1_MAX = 0.33, 0.25, 0.42
CUT2_TARGET, CUT2_MIN, CUT2_MAX = 0.66, 0.58, 0.75

# Balance backstop. The zones above already imply part1/part3 in [25%,42%] and
# part2 in [16%,50%]; these bounds catch anything that slips through.
MIN_PART_SHARE, MAX_PART_SHARE = 0.15, 0.50

# How many cues of context to hand the AI on each side of a target boundary.
WINDOW_CUES_EACH_SIDE = 40

MEDIA_ORIGIN_PRIORITY = {"live": 100, "history": 10}


# --------------------------------------------------------------------------
# Transcript parsing (deterministic)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Cue:
    cue_id: int
    start_seconds: float
    end_seconds: float
    speaker: str
    text: str


_TS = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>[0-5]\d):(?P<s>[0-5]\d)[.,](?P<ms>\d{1,3})"
)
_ARROW = re.compile(r"-->")
_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>)?\s*$", re.S)


def parse_timestamp(value: str) -> float:
    """HH:MM:SS.mmm -> seconds. Raises on anything else; never guesses."""
    m = _TS.fullmatch(value.strip())
    if not m:
        raise ValueError(f"malformed timestamp: {value!r}")
    return (int(m["h"]) * 3600 + int(m["m"]) * 60 + int(m["s"])
            + int(m["ms"].ljust(3, "0")) / 1000.0)


def parse_webvtt(content: str) -> list[Cue]:
    """
    Parse a Microsoft Graph WEBVTT transcript into sequentially numbered cues.

    cue_id is the 1-based ordinal of the cue in the file. This matches the cue
    numbering the existing V5 positive-clip analysis already uses, so cue IDs
    mean the same thing in both subsystems.
    """
    cues: list[Cue] = []
    block: list[str] = []

    def flush(block: list[str]) -> None:
        timing = next((l for l in block if _ARROW.search(l)), None)
        if timing is None:
            return
        left, right = _ARROW.split(timing, 1)
        start = parse_timestamp(left.strip().split()[-1])
        end = parse_timestamp(right.strip().split()[0])
        body = "\n".join(block[block.index(timing) + 1:]).strip()
        speaker = ""
        vm = _VOICE.match(body)
        if vm:
            speaker, body = vm.group(1).strip(), vm.group(2).strip()
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            cues.append(Cue(len(cues) + 1, start, end, speaker, body))

    for raw in content.splitlines():
        line = raw.rstrip()
        if line.strip().upper().startswith("WEBVTT"):
            continue
        if not line.strip():
            if block:
                flush(block)
                block = []
            continue
        block.append(line)
    if block:
        flush(block)

    if not cues:
        raise ValueError("transcript contained no usable cues")
    return cues


def parse_transcript(content: str, content_type: str | None = None) -> list[Cue]:
    """Entry point. WEBVTT today; other formats must be added explicitly."""
    ct = (content_type or "").lower()
    if "json" in ct:
        raise NotImplementedError("JSON transcript parsing is not implemented yet")
    return parse_webvtt(content)


# --------------------------------------------------------------------------
# The lecture timeline
#
# THE BUG THIS REPLACES
# ---------------------
# Every boundary used to be a fraction of `duration_seconds`, the recording
# length, on the assumption that the lecture runs 0 -> duration. Phase 6A
# measured that this is false in two independent ways:
#
#   * Cue times are CALL-relative. One measured lecture's first cue is at
#     10026.8s because the call sat open for nearly three hours before the
#     teaching began. Its 33% zone was computed as 3600-6048s, a stretch of
#     time containing no cues at all, and the planner raised "no transcript
#     cues fall inside the cut_1 zone".
#
#   * A long call is recorded in SEVERAL files, so the last cue can sit past
#     the end of the first recording. `duration_seconds` was never the length
#     of the lecture; for a multipart lecture it is not even the length of the
#     media.
#
# The lecture timeline is therefore the span the transcript actually covers.
# Media coordinates are a separate question, answered once by
# app.media.coordinates and never re-derived here.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LectureTimeline:
    """The canonical span the transcript covers. Not the recording length."""
    start_seconds: float
    end_seconds: float

    @property
    def span_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def at(self, fraction: float) -> float:
        """The canonical time a share of the way through the lecture."""
        return self.start_seconds + fraction * self.span_seconds

    def as_dict(self) -> dict:
        return {"start_seconds": round(self.start_seconds, 3),
                "end_seconds": round(self.end_seconds, 3),
                "span_seconds": round(self.span_seconds, 3)}


def lecture_timeline(cues: list[Cue]) -> LectureTimeline:
    """
    Derive the timeline from the transcript itself.

    The first cue's start and the last cue's end, which is the only pair of
    numbers that describes the taught lecture rather than the call around it.
    """
    if not cues:
        raise ValueError("cannot derive a timeline from an empty transcript")
    start = min(c.start_seconds for c in cues)
    end = max(c.end_seconds for c in cues)
    if end <= start:
        raise ValueError("the transcript covers no time")
    return LectureTimeline(start_seconds=start, end_seconds=end)


# --------------------------------------------------------------------------
# Candidate windows
# --------------------------------------------------------------------------

def _zone(cues: list[Cue], timeline: LectureTimeline,
          lo: float, hi: float) -> list[Cue]:
    """Cues between two shares of the LECTURE, wherever the lecture starts."""
    lower, upper = timeline.at(lo), timeline.at(hi)
    return [c for c in cues if lower <= c.start_seconds <= upper]


def build_candidate_windows(cues: list[Cue], timeline: LectureTimeline) -> dict:
    """
    Build the two candidate windows the AI is allowed to choose from.

    Only these cues are sent - never the whole multi-hour transcript. Each
    window is trimmed to a bounded number of cues centred on the target
    boundary, so the request size stays predictable regardless of lecture
    length, while still giving enough surrounding dialogue to recognise a
    genuine transition.
    """
    if timeline.span_seconds <= 0:
        raise ValueError("the lecture timeline must cover a positive span")

    windows = {}
    for name, target, lo, hi in (("cut_1", CUT1_TARGET, CUT1_MIN, CUT1_MAX),
                                 ("cut_2", CUT2_TARGET, CUT2_MIN, CUT2_MAX)):
        zone = _zone(cues, timeline, lo, hi)
        if not zone:
            raise ValueError(
                f"no transcript cues fall inside the {name} zone "
                f"({lo:.0%}-{hi:.0%} of the lecture, "
                f"{timeline.at(lo):.1f}s-{timeline.at(hi):.1f}s)")
        target_s = timeline.at(target)
        centre = min(range(len(zone)), key=lambda i: abs(zone[i].start_seconds - target_s))
        start = max(0, centre - WINDOW_CUES_EACH_SIDE)
        end = min(len(zone), centre + WINDOW_CUES_EACH_SIDE + 1)
        windows[name] = {
            "zone_start_seconds": round(timeline.at(lo), 3),
            "zone_end_seconds": round(timeline.at(hi), 3),
            "target_seconds": round(target_s, 3),
            "cues": [
                {"cue_id": c.cue_id,
                 "start": format_timestamp(c.start_seconds),
                 "speaker": c.speaker,
                 "text": c.text}
                for c in zone[start:end]
            ],
        }
    return windows


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# --------------------------------------------------------------------------
# AI contract
# --------------------------------------------------------------------------

AI_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cut_1_cue", "cut_1_reason", "cut_2_cue", "cut_2_reason", "parts", "confidence"],
    "properties": {
        "cut_1_cue": {"type": "integer",
                      "description": "cue_id from the cut_1 candidate list ONLY"},
        "cut_1_reason": {"type": "string", "maxLength": 500},
        "cut_2_cue": {"type": "integer",
                      "description": "cue_id from the cut_2 candidate list ONLY"},
        "cut_2_reason": {"type": "string", "maxLength": 500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "parts": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["part_number", "title", "summary"],
                "properties": {
                    "part_number": {"type": "integer", "enum": [1, 2, 3]},
                    "title": {"type": "string", "maxLength": 120},
                    "summary": {"type": "string", "maxLength": 600},
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You choose where to split a recorded training lecture into three parts.

You will be given two candidate windows of transcript cues. Choose exactly one
cue_id from each window to act as a boundary.

HARD RULES
- Return ONLY cue_id values that appear in the candidate list you were given.
- Never invent, calculate or return a timestamp. Timestamps are resolved from
  the cue IDs by code.
- cut_1_cue must be smaller than cut_2_cue.

CHOOSING WELL
Prefer a cue that begins a genuine change in teaching activity:
- a topic transition or the start of a new subject
- the end or start of an exercise or activity
- a break boundary ("let's take five", returning from a break)
- the end of a Q&A segment
- a recap or summary boundary
- an explicit trainer transition phrase ("right, moving on", "so that covers")

Prefer the cue where the NEW segment begins, not the last cue of the old one.
If several cues are equally good, choose the one closest to the middle of the
window, which keeps the three parts balanced.

Write a short, factual title and summary for each of the three parts, based on
what the transcript actually shows. Do not speculate about content you cannot
see."""


def build_ai_request(session: dict, windows: dict,
                     timeline: LectureTimeline) -> dict:
    """One structured request carrying both candidate windows."""
    return {
        "system": SYSTEM_PROMPT,
        "schema": AI_OUTPUT_SCHEMA,
        "input": {
            "lecture": {
                "subject": session.get("subject"),
                "trainer": session.get("trainer"),
                "date": str(session.get("date")),
                "duration_seconds": round(timeline.span_seconds, 3),
            },
            "cut_1_window": windows["cut_1"],
            "cut_2_window": windows["cut_2"],
            "instructions": (
                "Choose cut_1_cue from cut_1_window.cues and cut_2_cue from "
                "cut_2_window.cues. Return only cue IDs present in those lists."
            ),
        },
    }


# --------------------------------------------------------------------------
# Deterministic validator
# --------------------------------------------------------------------------

@dataclass
class SplitPlan:
    session_id: str
    split_version: str
    media_origin: str | None
    # Provenance only. The boundaries below are canonical transcript times and
    # are NOT fractions of this number - see LectureTimeline for why.
    recording_duration_seconds: float
    timeline_start_seconds: float = 0.0
    timeline_end_seconds: float = 0.0
    cut_1_cue: int | None = None
    cut_1_seconds: float | None = None
    cut_1_reason: str | None = None
    cut_2_cue: int | None = None
    cut_2_seconds: float | None = None
    cut_2_reason: str | None = None
    parts: list[dict] = field(default_factory=list)
    planner_status: str = "rejected"
    planner_model: str | None = None
    planner_confidence: float | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def validate_ai_output(ai: dict,
                       cues: list[Cue],
                       windows: dict,
                       timeline: LectureTimeline,
                       session_id: str,
                       media_origin: str | None,
                       model: str | None = None,
                       recording_duration_seconds: float | None = None) -> SplitPlan:
    """
    Every boundary claim is re-derived from the transcript here. Nothing the AI
    returned is trusted except the two cue IDs and the free text.

    BOUNDARY CONVENTION: boundary_seconds = the START timestamp of the selected
    cue, on the CANONICAL transcript timeline. Part 1 runs from the lecture's
    first cue to cut_1, part 2 from cut_1 to cut_2, part 3 from cut_2 to the
    lecture's last cue. Boundaries are shared, so the parts are contiguous by
    construction: no gaps and no overlaps are possible.

    Part 1 no longer starts at 0 and part 3 no longer ends at the recording
    length. Both were wrong for any lecture whose call began before the
    teaching did, and the second was wrong for every multipart lecture.
    """
    plan = SplitPlan(session_id=session_id, split_version=SPLIT_VERSION,
                     media_origin=media_origin,
                     recording_duration_seconds=round(
                         recording_duration_seconds
                         if recording_duration_seconds is not None
                         else timeline.span_seconds, 3),
                     timeline_start_seconds=round(timeline.start_seconds, 3),
                     timeline_end_seconds=round(timeline.end_seconds, 3),
                     planner_model=model)
    errors = plan.errors
    by_id = {c.cue_id: c for c in cues}
    allowed = {name: {c["cue_id"] for c in w["cues"]} for name, w in windows.items()}

    c1 = ai.get("cut_1_cue")
    c2 = ai.get("cut_2_cue")

    for label, value, key in (("cut_1_cue", c1, "cut_1"), ("cut_2_cue", c2, "cut_2")):
        if not isinstance(value, int):
            errors.append(f"{label} is not an integer: {value!r}")
        elif value not in by_id:
            errors.append(f"{label}={value} does not exist in the transcript")
        elif value not in allowed[key]:
            errors.append(f"{label}={value} was not among the candidate cues offered")

    if errors:
        return plan

    if not c1 < c2:
        errors.append(f"cut_1_cue ({c1}) must be less than cut_2_cue ({c2})")
        return plan

    s1 = by_id[c1].start_seconds
    s2 = by_id[c2].start_seconds

    if not (timeline.at(CUT1_MIN) <= s1 <= timeline.at(CUT1_MAX)):
        errors.append(f"cut_1 at {s1:.3f}s is outside the allowed "
                      f"{CUT1_MIN:.0%}-{CUT1_MAX:.0%} region of the lecture "
                      f"({timeline.at(CUT1_MIN):.3f}s-{timeline.at(CUT1_MAX):.3f}s)")
    if not (timeline.at(CUT2_MIN) <= s2 <= timeline.at(CUT2_MAX)):
        errors.append(f"cut_2 at {s2:.3f}s is outside the allowed "
                      f"{CUT2_MIN:.0%}-{CUT2_MAX:.0%} region of the lecture "
                      f"({timeline.at(CUT2_MIN):.3f}s-{timeline.at(CUT2_MAX):.3f}s)")
    if s2 <= s1:
        errors.append(f"cut_2 time ({s2:.3f}s) must be after cut_1 time ({s1:.3f}s)")
    if timeline.end_seconds <= s2:
        errors.append(f"the lecture ends at {timeline.end_seconds:.3f}s, which must "
                      f"be after cut_2 ({s2:.3f}s)")
    if s1 <= timeline.start_seconds:
        errors.append(f"the lecture starts at {timeline.start_seconds:.3f}s, which "
                      f"must be before cut_1 ({s1:.3f}s)")

    if errors:
        return plan

    ranges = [(1, timeline.start_seconds, s1), (2, s1, s2),
              (3, s2, timeline.end_seconds)]
    for n, a, b in ranges:
        if b - a <= 0:
            errors.append(f"part {n} has non-positive duration ({b - a:.3f}s)")

    # Contiguity: each part must start exactly where the previous one ended.
    for (n, a, _), (_, prev_b) in zip(ranges[1:], [(r[0], r[2]) for r in ranges[:-1]]):
        if a != prev_b:
            errors.append(f"part {n} starts at {a:.3f}s but the previous part "
                          f"ended at {prev_b:.3f}s (gap or overlap)")

    for n, a, b in ranges:
        share = (b - a) / timeline.span_seconds
        if not (MIN_PART_SHARE <= share <= MAX_PART_SHARE):
            errors.append(f"part {n} is {share:.1%} of the lecture, outside the "
                          f"balanced range {MIN_PART_SHARE:.0%}-{MAX_PART_SHARE:.0%}")

    parts_in = ai.get("parts") or []
    if len(parts_in) != 3:
        errors.append(f"expected exactly 3 parts, got {len(parts_in)}")
    elif sorted(p.get("part_number") for p in parts_in) != [1, 2, 3]:
        errors.append("parts must be numbered 1, 2 and 3 exactly once each")

    conf = ai.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
        errors.append(f"confidence must be between 0 and 1, got {conf!r}")

    if errors:
        return plan

    titled = {p["part_number"]: p for p in parts_in}
    plan.cut_1_cue, plan.cut_1_seconds = c1, round(s1, 3)
    plan.cut_2_cue, plan.cut_2_seconds = c2, round(s2, 3)
    plan.cut_1_reason = (ai.get("cut_1_reason") or "").strip() or None
    plan.cut_2_reason = (ai.get("cut_2_reason") or "").strip() or None
    plan.planner_confidence = conf
    plan.parts = [
        {"part_number": n,
         "title": titled[n].get("title"),
         "summary": titled[n].get("summary"),
         "start_seconds": round(a, 3),
         "end_seconds": round(b, 3),
         "duration_seconds": round(b - a, 3),
         "share": round((b - a) / timeline.span_seconds, 4)}
        for n, a, b in ranges
    ]
    plan.planner_status = "planned"
    return plan


# --------------------------------------------------------------------------
# Deterministic identities and job preview
# --------------------------------------------------------------------------

def short_session_hash(session_id: str) -> str:
    """Same md5[:8] short id the positive-clip filenames already use."""
    return hashlib.md5(session_id.encode()).hexdigest()[:8]


def split_plan_key(session_id: str, split_version: str = SPLIT_VERSION) -> str:
    return f"split:{short_session_hash(session_id)}:{split_version}"


def part_job_key(session_id: str, part_number: int,
                 split_version: str = SPLIT_VERSION) -> str:
    return f"lecturepart:{short_session_hash(session_id)}:{split_version}:{part_number}"


def safe_subject(subject: str | None, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", subject or "lecture").strip("-")
    return slug[:limit].strip("-") or "lecture"


def part_output_filename(session: dict, part_number: int) -> str:
    """YYYYMMDD_<safe-subject>_<short-id>_part-0N.mp4 - Windows/SharePoint safe."""
    date = str(session.get("date") or "")[:10].replace("-", "") or "00000000"
    return (f"{date}_{safe_subject(session.get('subject'))}_"
            f"{short_session_hash(session['session_id'])}_part-{part_number:02d}.mp4")


def preview_media_jobs(plan: SplitPlan, session: dict,
                       destination: dict | None = None) -> list[dict]:
    """
    The exact three rows that would later be inserted into qa_media_jobs.
    Returns preview dicts only - this function never touches the database.
    """
    if plan.planner_status != "planned":
        return []
    dest = destination or {}
    priority = MEDIA_ORIGIN_PRIORITY.get(plan.media_origin or "", None)
    jobs = []
    for part in plan.parts:
        n = part["part_number"]
        jobs.append({
            "job_key": part_job_key(plan.session_id, n, plan.split_version),
            "session_id": plan.session_id,
            "job_type": "lecture_part",
            "part_number": n,
            "clip_key": None,
            "cut_mode": "fast_copy",
            "start_seconds": part["start_seconds"],
            "end_seconds": part["end_seconds"],
            "duration_seconds": part["duration_seconds"],
            "output_filename": part_output_filename(session, n),
            "source_drive_id": session.get("recording_drive_id"),
            "source_item_id": session.get("recording_item_id"),
            "destination_drive_id": dest.get("destination_drive_id"),
            "destination_folder_item_id": dest.get("destination_folder_item_id"),
            "status": "pending",
            "priority": priority,
            "metadata": {
                "split_version": plan.split_version,
                "split_plan_key": split_plan_key(plan.session_id, plan.split_version),
                "part_number": n,
                "part_title": part["title"],
                "part_summary": part["summary"],
                "lecture_date": str(session.get("date") or "")[:10],
                "subject": session.get("subject"),
                "trainer": session.get("trainer"),
                "queue_origin": plan.media_origin,
                "produced_by": "lecture-split-planner-v1",
            },
        })
    return jobs
