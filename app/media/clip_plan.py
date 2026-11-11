"""
The Positive Moment clip plan: evidence range -> padded, clamped media range.

    evidence (canonical ms)          01:20:15 -> 01:20:37
    media evidence (aligned)         canonical - media_offset_seconds
    requested (+/- padding)          01:19:15 -> 01:21:37
    actual (clamped to 0..duration)  what is really cut

Padding is context, never evidence: it is reduced at the file's edges, but the
evidence itself is never shortened to fit. Evidence outside the linked file,
or outside the transcript part that file carries, is refused - a clip of
another part's media would be a confident clip of the wrong moment.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.media.alignment import Alignment

CLIP_PLAN_VERSION = "positive_moment_clip_plan_v1"
DEFAULT_PADDING_BEFORE_SECONDS = 60.0
DEFAULT_PADDING_AFTER_SECONDS = 60.0
EDGE_TOLERANCE_SECONDS = 1.0

EVIDENCE_OUTSIDE_LINKED_RECORDING = "EVIDENCE_OUTSIDE_LINKED_RECORDING"
EVIDENCE_OUTSIDE_MEDIA = "EVIDENCE_OUTSIDE_MEDIA"


class PlanRefused(Exception):
    def __init__(self, code: str, detail: dict | None = None):
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


@dataclass(frozen=True)
class ClipPlan:
    evidence_start_ms: int
    evidence_end_ms: int
    media_evidence_start_seconds: float
    media_evidence_end_seconds: float
    requested_start_seconds: float
    requested_end_seconds: float
    actual_start_seconds: float
    actual_end_seconds: float
    padding_before_requested: float
    padding_after_requested: float
    padding_before_applied: float
    padding_after_applied: float

    @property
    def duration_seconds(self) -> float:
        return round(self.actual_end_seconds - self.actual_start_seconds, 3)


def plan_clip(*, evidence_start_ms: int, evidence_end_ms: int, alignment: Alignment,
              padding_before: float = DEFAULT_PADDING_BEFORE_SECONDS,
              padding_after: float = DEFAULT_PADDING_AFTER_SECONDS) -> ClipPlan:
    if not alignment.aligned:
        raise PlanRefused(alignment.code)
    start_c, end_c = evidence_start_ms / 1000.0, evidence_end_ms / 1000.0
    if (start_c < alignment.canonical_start_seconds - EDGE_TOLERANCE_SECONDS
            or end_c > alignment.canonical_end_seconds + EDGE_TOLERANCE_SECONDS):
        raise PlanRefused(EVIDENCE_OUTSIDE_LINKED_RECORDING, {
            "evidence_canonical_seconds": [round(start_c, 3), round(end_c, 3)],
            "linked_canonical_seconds": [alignment.canonical_start_seconds,
                                         alignment.canonical_end_seconds]})
    duration = alignment.file_duration_seconds
    media_start = start_c - alignment.media_offset_seconds
    media_end = end_c - alignment.media_offset_seconds
    if media_start < -EDGE_TOLERANCE_SECONDS or media_end > duration + EDGE_TOLERANCE_SECONDS:
        raise PlanRefused(EVIDENCE_OUTSIDE_MEDIA, {
            "media_evidence_seconds": [round(media_start, 3), round(media_end, 3)],
            "file_duration_seconds": duration})
    media_start = max(media_start, 0.0)
    media_end = min(media_end, duration)
    requested_start = media_start - padding_before
    requested_end = media_end + padding_after
    actual_start = max(0.0, requested_start)
    actual_end = min(duration, requested_end)
    return ClipPlan(
        evidence_start_ms=evidence_start_ms, evidence_end_ms=evidence_end_ms,
        media_evidence_start_seconds=round(media_start, 3),
        media_evidence_end_seconds=round(media_end, 3),
        requested_start_seconds=round(requested_start, 3),
        requested_end_seconds=round(requested_end, 3),
        actual_start_seconds=round(actual_start, 3),
        actual_end_seconds=round(actual_end, 3),
        padding_before_requested=padding_before, padding_after_requested=padding_after,
        padding_before_applied=round(media_start - actual_start, 3),
        padding_after_applied=round(actual_end - media_end, 3))


def plan_fingerprint(*, moment_fingerprint: str, source_drive_id: str, source_item_id: str,
                     alignment: Alignment, plan: ClipPlan, render_policy: dict,
                     provider: str) -> str:
    """Everything that decides the output bytes. Same inputs -> same job/asset."""
    raw = json.dumps({
        "version": CLIP_PLAN_VERSION, "moment": moment_fingerprint,
        "source": [source_drive_id, source_item_id],
        "offset_ms": round((alignment.media_offset_seconds or 0) * 1000),
        "range_ms": [round(plan.actual_start_seconds * 1000),
                     round(plan.actual_end_seconds * 1000)],
        "render": render_policy, "provider": provider}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
