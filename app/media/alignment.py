"""
Transcript <-> media alignment for ONE linked recording file.

WHY THIS IS NOT `media = transcript`
------------------------------------
Canonical cue times are call-relative: the first selected transcript part
starts the canonical timeline at zero and later parts are shifted by their
start offset (app/transcripts/combine.py). The file Recording Link resolved is
a SharePoint/OneDrive MP4 whose NAME carries the moment the client started
recording - measured 17-73 s BEFORE Graph's callRecording createdDateTime
(app/recordings/models.py). Phase 6A proved that the callRecording CONTENT
starts at createdDateTime and equals the transcript part's window; it did not
prove where the SharePoint file's time zero is. So neither origin is assumed.

HOW THE OFFSET IS PROVEN
------------------------
1. Pair the file with the Graph callRecording whose createdDateTime its name
   precedes by 0..MAX_FILE_LEAD_SECONDS (the Recording Link identity rule).
2. Pair that recording with ONE selected transcript part: by
   contentCorrelationId when both carry it, otherwise by createdDateTime
   equality within PAIR_TOLERANCE_SECONDS (6A measured +0.000 s).
3. Take the reference window W = recording endDateTime - createdDateTime (or
   the part's own transcript window). Test the two possible origins against
   the file's MEASURED duration D (driveItem video.duration):
      A  zero at createdDateTime   ->  D ~ W
      B  zero at the file-name time ->  D ~ W + lead
   Exactly one must hold within DURATION_TOLERANCE. Both holding is only
   accepted when they are the same origin within a second, or when exactly
   one of them also holds within ORIGIN_PRECISION_SECONDS (media_origin);
   neither holding, or no measurement, is refused.
4. media_seconds = canonical_seconds - media_offset_seconds, where
      media_offset_seconds = part_offset + (origin - part_created_at)

Anything unproven is REVIEW_REQUIRED_ALIGNMENT (or WAITING_FOR_ALIGNMENT when
the missing fact may still arrive, e.g. the duration is not yet indexed). A
clip is never submitted on an unproven offset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.common.time import parse_graph_datetime
from app.recordings.candidates import parse_recording_name
from app.recordings.models import MAX_FILE_LEAD_SECONDS

ALIGNMENT_VERSION = "positive_moment_media_alignment_v1"

ALIGNED = "ALIGNED"
WAITING_FOR_ALIGNMENT = "WAITING_FOR_ALIGNMENT"
REVIEW_REQUIRED_ALIGNMENT = "REVIEW_REQUIRED_ALIGNMENT"

PAIR_TOLERANCE_SECONDS = 2.0
MIN_DURATION_TOLERANCE_SECONDS = 2.0
DURATION_TOLERANCE_RATIO = 0.001        # 7 s on a 2 h recording
SAME_ORIGIN_SECONDS = 1.0
# When BOTH origins fit DURATION_TOLERANCE (a lead shorter than the tolerance: 0.1 % is 8-10 s on a 2-3 h
# recording), the one origin whose residual is also within this bound is the file's. Production 2026-11-02:
# every one of 42 measured Teams MP4s was 0.04-0.13 s longer than its Graph window, so the true origin's
# residual sits far inside 1 s while the other's is about the lead (5.3 s on the 2026-09-02 lectures).
ORIGIN_PRECISION_SECONDS = 1.0

METHOD_GRAPH_CREATED = "graph_created_origin"
METHOD_FILE_NAME = "file_name_origin"
ORIGIN_AMBIGUOUS = "ORIGIN_AMBIGUOUS"


@dataclass(frozen=True)
class TranscriptPart:
    part_index: int
    part_offset_ms: int
    provider_created_at: datetime | None
    provider_end_at: datetime | None = None
    content_correlation_id: str | None = None


@dataclass(frozen=True)
class RecordingRef:
    recording_id: str | None
    created_at: str
    end_at: str | None = None
    content_correlation_id: str | None = None


@dataclass(frozen=True)
class Alignment:
    status: str
    code: str
    media_offset_seconds: float | None = None
    method: str | None = None
    confidence: float | None = None
    file_duration_seconds: float | None = None
    canonical_start_seconds: float | None = None   # the part this file carries
    canonical_end_seconds: float | None = None
    detail: dict = field(default_factory=dict)

    @property
    def aligned(self) -> bool:
        return self.status == ALIGNED

    def to_canonical(self, media_seconds: float) -> float:
        return media_seconds + self.media_offset_seconds

    def as_dict(self) -> dict:
        return {"alignment_version": ALIGNMENT_VERSION, "status": self.status,
                "code": self.code, "media_offset_seconds": self.media_offset_seconds,
                "method": self.method, "confidence": self.confidence,
                "file_duration_seconds": self.file_duration_seconds,
                "canonical_start_seconds": self.canonical_start_seconds,
                "canonical_end_seconds": self.canonical_end_seconds, **self.detail}


def _ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return parse_graph_datetime(value).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


def media_origin(graph_gap: float, name_gap: float, tolerance: float, lead: float) -> str | None:
    """
    Where the file's time zero is, from the residuals of its measured duration against the two origins:
    METHOD_GRAPH_CREATED, METHOD_FILE_NAME, ORIGIN_AMBIGUOUS, or None (neither fits: the duration
    contradicts the recording). `tolerance` is the identity gate, unchanged. Only when BOTH origins pass it
    and they are more than SAME_ORIGIN_SECONDS apart is the strict bound consulted: exactly one residual
    within ORIGIN_PRECISION_SECONDS decides; both or neither within it stays ambiguous. A merely smaller
    residual never decides.
    """
    graph_fits, name_fits = graph_gap <= tolerance, name_gap <= tolerance
    if graph_fits and name_fits and lead > SAME_ORIGIN_SECONDS:
        graph_exact, name_exact = graph_gap <= ORIGIN_PRECISION_SECONDS, name_gap <= ORIGIN_PRECISION_SECONDS
        if graph_exact == name_exact:
            return ORIGIN_AMBIGUOUS
        return METHOD_GRAPH_CREATED if graph_exact else METHOD_FILE_NAME
    if graph_fits:
        return METHOD_GRAPH_CREATED
    if name_fits:
        return METHOD_FILE_NAME
    return None


def _review(code, **detail):
    return Alignment(REVIEW_REQUIRED_ALIGNMENT, code, detail=detail)


def align(*, file_name: str | None, file_duration_seconds: float | None,
          recordings: list, parts: list) -> Alignment:
    """Prove the offset between the canonical timeline and one recording file."""
    parsed = parse_recording_name(file_name)
    if parsed is None:
        return _review("FILE_NAME_HAS_NO_RECORDING_TIMESTAMP")
    file_ts = parsed[2]
    if not recordings:
        return Alignment(WAITING_FOR_ALIGNMENT, "GRAPH_RECORDING_METADATA_UNAVAILABLE")

    paired = []
    for recording in recordings:
        created = _ts(recording.created_at)
        if created is None:
            continue
        lead = created - file_ts
        if 0 <= lead <= MAX_FILE_LEAD_SECONDS:
            paired.append((lead, recording, created))
    if not paired:
        return _review("NO_GRAPH_RECORDING_FOR_FILE")
    lead, recording, created = min(paired, key=lambda item: item[0])

    by_correlation = [part for part in parts if recording.content_correlation_id
                      and part.content_correlation_id == recording.content_correlation_id]
    by_time = [part for part in parts if _ts(part.provider_created_at) is not None
               and abs(_ts(part.provider_created_at) - created) <= PAIR_TOLERANCE_SECONDS]
    candidates, pairing = ((by_correlation, "content_correlation_id") if by_correlation
                           else (by_time, "created_at"))
    if len(candidates) != 1:
        return _review("NO_TRANSCRIPT_PART_FOR_RECORDING" if not candidates
                       else "SEVERAL_TRANSCRIPT_PARTS_FOR_RECORDING",
                       pairing_candidates=len(candidates))
    part = candidates[0]
    part_created = _ts(part.provider_created_at)
    if part_created is None:
        return _review("TRANSCRIPT_PART_HAS_NO_START")

    end = _ts(recording.end_at)
    reference = (end - created) if end and end > created else None
    reference_source = "graph_recording_window"
    if reference is None and part.provider_end_at is not None:
        part_end = _ts(part.provider_end_at)
        if part_end and part_end > part_created:
            reference, reference_source = part_end - part_created, "transcript_part_window"
    if reference is None:
        return _review("NO_REFERENCE_DURATION")
    if file_duration_seconds is None or file_duration_seconds <= 0:
        return Alignment(WAITING_FOR_ALIGNMENT, "FILE_DURATION_UNAVAILABLE")

    tolerance = max(MIN_DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_RATIO * reference)
    graph_gap = abs(file_duration_seconds - reference)
    name_gap = abs(file_duration_seconds - (reference + lead))
    graph_origin, name_origin = graph_gap <= tolerance, name_gap <= tolerance
    detail = {"pairing": pairing, "part_index": part.part_index,
              "graph_recording_id": recording.recording_id,
              "file_name_lead_seconds": round(lead, 3),
              "reference_duration_seconds": round(reference, 3),
              "reference_source": reference_source,
              "duration_tolerance_seconds": round(tolerance, 3),
              "graph_origin_fits": graph_origin, "file_name_origin_fits": name_origin}
    decided = media_origin(graph_gap, name_gap, tolerance, lead)
    if decided == ORIGIN_AMBIGUOUS:
        return _review("ALIGNMENT_ORIGIN_AMBIGUOUS", **detail)
    if graph_origin and name_origin and lead > SAME_ORIGIN_SECONDS:
        detail["origin_decided_by"] = "precision"
    if decided == METHOD_GRAPH_CREATED:
        origin, method = created, METHOD_GRAPH_CREATED
    elif decided == METHOD_FILE_NAME:
        origin, method = file_ts, METHOD_FILE_NAME
    else:
        return _review("FILE_DURATION_CONTRADICTS_RECORDING", **detail,
                       file_duration_seconds=round(file_duration_seconds, 3))

    part_offset = part.part_offset_ms / 1000.0
    offset = part_offset + (origin - part_created)
    ordered = sorted(parts, key=lambda item: item.part_offset_ms)
    later = [item.part_offset_ms / 1000.0 for item in ordered
             if item.part_offset_ms > part.part_offset_ms]
    upper = min([offset + file_duration_seconds] + later[:1])
    confidence = (0.99 if pairing == "content_correlation_id" else 0.95) - (
        0.05 if method == METHOD_FILE_NAME else 0.0)
    return Alignment(ALIGNED, "OFFSET_PROVEN_BY_MEASURED_DURATION",
                     media_offset_seconds=round(offset, 3), method=method,
                     confidence=round(confidence, 4),
                     file_duration_seconds=round(file_duration_seconds, 3),
                     canonical_start_seconds=round(max(part_offset, offset), 3),
                     canonical_end_seconds=round(upper, 3), detail=detail)
