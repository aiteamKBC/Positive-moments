"""
Phase 2B: deterministic transcript occurrence selection.

A direct port of the authoritative legacy n8n node "Select Transcript Parts V3"
from automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json. Where
this file and the Phase 2A documentation ever disagreed, the export won.

One deliberate deviation, as instructed: the legacy node re-parsed
`scheduledStart` / `scheduledEnd` from n8n strings that were UTC without a
trailing `Z`, and compensated for that. The new platform reads canonical
`timestamptz` values from `lecture_sessions`, so that compatibility parsing is
NOT recreated. Every comparison below is on timezone-aware instants; only the
business-date filter uses Africa/Cairo.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.common.time import CAIRO


SELECTION_VERSION = "legacy_qa_v8_overlap_cluster_v1"

# Occurrence window, exactly as the legacy export defines it.
WINDOW_BEFORE = timedelta(hours=2)
WINDOW_AFTER = timedelta(hours=3)
# Maximum temporal gap for attaching a nearby part to the cluster.
MAX_PART_GAP = timedelta(minutes=20)

NO_CANDIDATES = "NO_CANDIDATES"
NO_SAME_DAY_CANDIDATES = "NO_SAME_DAY_CANDIDATES"
NO_WINDOW_CANDIDATES = "NO_WINDOW_CANDIDATES"
SELECTED = "SELECTED"


@dataclass(frozen=True)
class CandidateArtifact:
    """One Phase 2A artifact visible to a lecture, as stored in PostgreSQL."""

    artifact_id: Any
    provider_transcript_id: str
    provider_created_at: datetime | None
    provider_end_at: datetime | None
    provider_call_id: str | None
    meeting_id: str | None
    content_sha256: str | None = None
    content_bytes: int | None = None


@dataclass(frozen=True)
class RankedCandidate:
    candidate: CandidateArtifact
    start: datetime
    end: datetime
    duration_seconds: float
    overlap_seconds: float
    start_distance_seconds: float


@dataclass
class SelectionResult:
    status: str
    primary: RankedCandidate | None = None
    parts: list[RankedCandidate] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _js_round(value: float) -> int:
    """
    JavaScript Math.round: half rounds toward +Infinity.

    Python's round() is banker's rounding, which would disagree with the legacy
    node on exact .5 minute boundaries.
    """
    import math

    return int(math.floor(value + 0.5))


def cairo_date_of(value: datetime) -> date:
    return value.astimezone(CAIRO).date()


def _prepare(candidate: CandidateArtifact) -> RankedCandidate | None:
    """
    Legacy `preparedCandidates` mapping.

    The end falls back to the start when it is missing or earlier than the
    start, matching the export exactly; duration is then zero rather than
    negative.
    """
    start = candidate.provider_created_at
    if start is None:
        return None
    end = candidate.provider_end_at
    if end is None or end < start:
        end = start
    return RankedCandidate(
        candidate=candidate, start=start, end=end,
        duration_seconds=(end - start).total_seconds(),
        overlap_seconds=0.0, start_distance_seconds=0.0,
    )


def select_transcript_parts(
    candidates: list[CandidateArtifact],
    *,
    scheduled_start: datetime,
    scheduled_end: datetime,
    target_date: date,
    meeting_id: str | None = None,
) -> SelectionResult:
    """Port of Select Transcript Parts V3. Deterministic, offline, no Graph."""
    if scheduled_end <= scheduled_start:
        raise ValueError("scheduled_end must be after scheduled_start")

    diagnostics: dict[str, Any] = {
        "selection_method": SELECTION_VERSION,
        "scheduled_start": scheduled_start.isoformat(),
        "scheduled_end": scheduled_end.isoformat(),
        "target_date": target_date.isoformat(),
        "occurrence_window_before_minutes": WINDOW_BEFORE.total_seconds() / 60,
        "occurrence_window_after_minutes": WINDOW_AFTER.total_seconds() / 60,
        "maximum_part_gap_minutes": MAX_PART_GAP.total_seconds() / 60,
        "candidate_count_before_date_filter": len(candidates),
        "same_day_candidate_count": 0,
        "occurrence_window_candidate_count": 0,
    }
    if not candidates:
        return SelectionResult(NO_CANDIDATES, diagnostics=diagnostics)

    # --- Step 1: same meeting (when both sides carry it) + same Cairo date ---
    same_day: list[CandidateArtifact] = []
    for candidate in candidates:
        if candidate.provider_created_at is None:
            continue
        if meeting_id and candidate.meeting_id and candidate.meeting_id != meeting_id:
            continue
        if cairo_date_of(candidate.provider_created_at) != target_date:
            continue
        same_day.append(candidate)
    diagnostics["same_day_candidate_count"] = len(same_day)
    if not same_day:
        return SelectionResult(NO_SAME_DAY_CANDIDATES, diagnostics=diagnostics)

    # --- Step 2: occurrence window, 2h before start .. 3h after end ---------
    window_start = scheduled_start - WINDOW_BEFORE
    window_end = scheduled_end + WINDOW_AFTER
    prepared: list[RankedCandidate] = []
    for candidate in same_day:
        item = _prepare(candidate)
        if item is None:
            continue
        if item.end >= window_start and item.start <= window_end:
            prepared.append(item)
    diagnostics["occurrence_window_candidate_count"] = len(prepared)
    if not prepared:
        return SelectionResult(NO_WINDOW_CANDIDATES, diagnostics=diagnostics)

    # Chronological order first, exactly as the legacy node does before ranking.
    prepared.sort(key=lambda item: item.start)
    prepared = [
        RankedCandidate(
            candidate=item.candidate, start=item.start, end=item.end,
            duration_seconds=item.duration_seconds,
            overlap_seconds=max(
                0.0,
                (min(item.end, scheduled_end) - max(item.start, scheduled_start)).total_seconds(),
            ),
            start_distance_seconds=abs((item.start - scheduled_start).total_seconds()),
        )
        for item in prepared
    ]

    # --- Step 3: primary = overlap desc, duration desc, start distance asc --
    ranked = sorted(
        prepared,
        key=lambda item: (-item.overlap_seconds, -item.duration_seconds,
                          item.start_distance_seconds),
    )
    primary = ranked[0]

    # --- Step 4: iteratively attach nearby parts ----------------------------
    # Same non-empty callId as ANY already-selected part, OR within 20 minutes
    # of the current cluster. Repeats until nothing more attaches, so a second
    # part can bring a third into range.
    selected = [primary]
    selected_ids = {primary.candidate.provider_transcript_id}
    cluster_start, cluster_end = primary.start, primary.end

    def gap_from_cluster(item: RankedCandidate) -> timedelta:
        if item.end < cluster_start:
            return cluster_start - item.end
        if item.start > cluster_end:
            return item.start - cluster_end
        return timedelta(0)

    added = True
    while added:
        added = False
        for item in prepared:
            if item.candidate.provider_transcript_id in selected_ids:
                continue
            call_id = item.candidate.provider_call_id
            same_call = bool(call_id) and any(
                part.candidate.provider_call_id == call_id for part in selected
            )
            if same_call or gap_from_cluster(item) <= MAX_PART_GAP:
                selected.append(item)
                selected_ids.add(item.candidate.provider_transcript_id)
                cluster_start = min(cluster_start, item.start)
                cluster_end = max(cluster_end, item.end)
                added = True

    selected.sort(key=lambda item: item.start)

    actual_start = min(item.start for item in selected)
    actual_end = max(item.end for item in selected)
    diagnostics.update({
        "primary_transcript_id": primary.candidate.provider_transcript_id,
        "primary_overlap_seconds": primary.overlap_seconds,
        "primary_duration_seconds": primary.duration_seconds,
        "primary_start_distance_seconds": primary.start_distance_seconds,
        "selected_parts_count": len(selected),
        "selected_part_ids": [item.candidate.provider_transcript_id for item in selected],
        "selected_call_ids": [item.candidate.provider_call_id for item in selected],
        "actual_start": actual_start.isoformat(),
        "actual_end": actual_end.isoformat(),
        "start_difference_minutes": _js_round(
            (actual_start - scheduled_start).total_seconds() / 60),
        "end_difference_minutes": _js_round(
            (actual_end - scheduled_end).total_seconds() / 60),
    })
    diagnostics["start_status"] = (
        "OnTime" if diagnostics["start_difference_minutes"] == 0
        else "Early" if diagnostics["start_difference_minutes"] < 0 else "Late"
    )
    diagnostics["end_status"] = (
        "OnTime" if diagnostics["end_difference_minutes"] == 0
        else "EarlyFinish" if diagnostics["end_difference_minutes"] < 0 else "Overrun"
    )
    return SelectionResult(SELECTED, primary=primary, parts=selected, diagnostics=diagnostics)
