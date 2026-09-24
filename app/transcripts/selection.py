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
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.common.time import CAIRO
from app.transcripts.identity import canonical_key


SELECTION_VERSION = "legacy_qa_v8_overlap_cluster_v1"

# Versions the definition of "the evidence this selection was made from". It is
# NOT the selection algorithm: the parts chosen are exactly what they were. It
# is what lets freshness be judged on the inputs that can change the answer
# rather than on when Graph last happened to be asked for them.
SELECTION_INPUT_FINGERPRINT_VERSION = "selection_input_fingerprint_v1"

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


@dataclass(frozen=True)
class RelevantCandidates:
    """The part of the candidate pool that can influence ONE occurrence."""

    candidates: list[CandidateArtifact]
    same_day_count: int
    fingerprint: str


def relevant_candidates(
    candidates: list[CandidateArtifact],
    *,
    scheduled_start: datetime,
    scheduled_end: datetime,
    target_date: date,
    meeting_id: str | None = None,
) -> RelevantCandidates:
    """
    Steps 1 and 2 of Select Transcript Parts V3, and nothing else.

    This is the ONE definition of "transcript evidence relevant to this lecture
    occurrence". The selector draws its parts from it and the pipeline state
    resolver judges freshness by it, so the two cannot disagree about what a
    selection was made from.

    It exists because a Teams series meeting lists the transcripts of every
    occurrence: Phase 2A links all of them to each lecture of the series. The
    09-11 and 09-18 transcripts of a weekly class are candidates of the 09-04
    lecture, and they can never be selected for it - the Cairo date filter
    rejects them first. Letting them decide whether 09-04 is stale was the bug.
    """
    same_day: list[CandidateArtifact] = []
    for candidate in candidates:
        if candidate.provider_created_at is None:
            continue
        if meeting_id and candidate.meeting_id and candidate.meeting_id != meeting_id:
            continue
        if cairo_date_of(candidate.provider_created_at) != target_date:
            continue
        same_day.append(candidate)

    window_start = scheduled_start - WINDOW_BEFORE
    window_end = scheduled_end + WINDOW_AFTER
    relevant: list[CandidateArtifact] = []
    for candidate in same_day:
        item = _prepare(candidate)
        if item is None:
            continue
        if item.end >= window_start and item.start <= window_end:
            relevant.append(candidate)
    return RelevantCandidates(candidates=relevant, same_day_count=len(same_day),
                              fingerprint=selection_input_fingerprint(relevant))


def selection_input_fingerprint(candidates: list[CandidateArtifact]) -> str:
    """
    SHA-256 over exactly what can change a selection, for the relevant set.

    Per candidate: the CANONICAL transcript identity (so a Graph
    re-serialization of the same transcript is the same input), the provider
    timing the selector ranks and clusters on, the call id it attaches parts
    by, and the current content hash. Sorted, so candidate order is not an
    input. Deliberately absent: artifact ids, fetch and first/last-seen times.
    Re-fetching identical bytes changes none of these, so it cannot make a
    selection stale; a new relevant transcript or revised content always does.
    """
    lines = sorted(
        "|".join((
            canonical_key(candidate.provider_transcript_id),
            _instant(candidate.provider_created_at),
            _instant(candidate.provider_end_at),
            candidate.provider_call_id or "",
            candidate.content_sha256 or "",
        ))
        for candidate in candidates
    )
    digest = hashlib.sha256()
    digest.update(f"version:{SELECTION_INPUT_FINGERPRINT_VERSION}\n".encode())
    digest.update(f"selection:{SELECTION_VERSION}\n".encode())
    for line in lines:
        digest.update(f"candidate:{line}\n".encode())
    return digest.hexdigest()


def combined_source_fingerprint(selection_version: str, part_content_sha256s) -> str:
    """
    The Phase 2B combined transcript's `source_fingerprint`: the selection
    version plus each selected part's content hash, in part order. Defined once
    so the state resolver can ask "would recombining produce different bytes?"
    of a selection persisted before input fingerprints existed.
    """
    return hashlib.sha256(
        "\0".join([selection_version] + [sha or "" for sha in part_content_sha256s])
        .encode("utf-8")).hexdigest()


def _instant(value: datetime | None) -> str:
    # Normalised to UTC so the same instant always hashes the same, whatever
    # session time zone the driver happened to hand it back in.
    return "" if value is None else value.astimezone(timezone.utc).isoformat()


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
        diagnostics["selection_input_fingerprint"] = selection_input_fingerprint([])
        diagnostics["selection_input_fingerprint_version"] = SELECTION_INPUT_FINGERPRINT_VERSION
        return SelectionResult(NO_CANDIDATES, diagnostics=diagnostics)

    # --- Steps 1-2: the candidates relevant to THIS occurrence --------------
    relevance = relevant_candidates(
        candidates, scheduled_start=scheduled_start, scheduled_end=scheduled_end,
        target_date=target_date, meeting_id=meeting_id)
    diagnostics["same_day_candidate_count"] = relevance.same_day_count
    diagnostics["occurrence_window_candidate_count"] = len(relevance.candidates)
    diagnostics["selection_input_fingerprint"] = relevance.fingerprint
    diagnostics["selection_input_fingerprint_version"] = SELECTION_INPUT_FINGERPRINT_VERSION
    if not relevance.same_day_count:
        return SelectionResult(NO_SAME_DAY_CANDIDATES, diagnostics=diagnostics)
    if not relevance.candidates:
        return SelectionResult(NO_WINDOW_CANDIDATES, diagnostics=diagnostics)
    prepared = [_prepare(candidate) for candidate in relevance.candidates]

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
