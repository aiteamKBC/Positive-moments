"""
P0-C: the multipart recording mapping, as pure functions.

    Graph callRecordings of the meeting ─▶ the parts of THIS occurrence
                                         ─▶ one SharePoint/OneDrive MP4 per part
                                         ─▶ the transcript part of the same stretch (when stored)

No database, no Graph, no clock. Every rule reuses an existing validated one:

* OCCURRENCE (which recordings are this lecture's): the transcript selection
  rule QA uses for transcripts (app/transcripts/selection.py) - Cairo business
  date, the window scheduled_start - 2 h .. scheduled_end + 3 h, a primary by
  overlap / duration / start distance, then every recording with the same call
  id or within 20 min of the cluster. A recording in the window that is NOT
  attached but overlaps the scheduled slot is ambiguity, never ignored.
* FILE (which MP4 is a part's recording): the Recording Link identity rule
  (app/recordings/matching.py) - the lecture's exact normalized subject, its
  business date, and a file-name time 0..120 s BEFORE the part's Graph
  createdDateTime - applied PER PART. A file that fits two parts, or a part
  that two different files fit, is ambiguous; byte-identical copies of one file
  collapse first (app/recordings/resolution.collapse_copies).
* DURATION (an independent check): the file's measured video duration must
  equal the part's Graph window, with zero at createdDateTime or at the file
  name's time, within the alignment tolerance (app/media/alignment.py).
* TRANSCRIPT (corroboration, optional): contentCorrelationId first, then
  createdDateTime within 2 s, exactly as alignment pairs them. A transcript part
  that belongs to a recording OUTSIDE this occurrence, or call ids that do not
  intersect, is a conflict. No transcript at all is fine.

Nothing is guessed: a part that cannot be proven keeps an explicit status, and
a lecture is LOCATED only when every one of its parts is.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.common.time import parse_graph_datetime
from app.media.alignment import (
    DURATION_TOLERANCE_RATIO, METHOD_FILE_NAME, METHOD_GRAPH_CREATED, MIN_DURATION_TOLERANCE_SECONDS,
    PAIR_TOLERANCE_SECONDS, SAME_ORIGIN_SECONDS,
)
from app.recordings.candidates import normalize_subject
from app.recordings.models import MAX_FILE_LEAD_SECONDS
from app.recordings.resolution import DEFAULT_POLICY, Exact, ResolutionPolicy, collapse_copies
from app.transcripts.selection import MAX_PART_GAP, WINDOW_AFTER, WINDOW_BEFORE, cairo_date_of

MAPPING_VERSION = "recording_parts_v1"
VERIFICATION_METHOD = "graph_recording_file_lead_duration_v1"

# Per-part statuses (bunny_recording_sources.locate_status).
LOCATED = "LOCATED"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
DURATION_PENDING = "DURATION_PENDING"
DURATION_MISMATCH = "DURATION_MISMATCH"
SOURCE_MISSING = "SOURCE_MISSING"
SOURCE_CHANGED = "SOURCE_CHANGED"

# Occurrence selection statuses.
SELECTED = "SELECTED"
NO_RECORDING_FOR_OCCURRENCE = "NO_RECORDING_FOR_OCCURRENCE"
AMBIGUOUS_OCCURRENCE_RECORDINGS = "AMBIGUOUS_OCCURRENCE_RECORDINGS"
GRAPH_RESPONSE_INVALID = "GRAPH_RESPONSE_INVALID"

# Transcript pairing (bunny_recording_discoveries.transcript_pairing).
NO_TRANSCRIPT = "NO_TRANSCRIPT"
PAIRED = "PAIRED"
PARTIAL = "PARTIAL"
CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class RecordingPart:
    """One Graph callRecording. Metadata only - recordingContentUrl is never read."""
    recording_id: str
    call_id: str
    created_at: datetime
    end_at: datetime | None
    content_correlation_id: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.end_at is None:
            return None
        seconds = (self.end_at - self.created_at).total_seconds()
        return seconds if seconds > 0 else None

    @property
    def effective_end(self) -> datetime:
        return self.end_at if self.end_at is not None and self.end_at > self.created_at else self.created_at


@dataclass(frozen=True)
class OccurrenceRecordings:
    status: str
    parts: tuple = ()                 # this occurrence's recordings, in recording order
    all_recordings: tuple = ()        # every recording Graph listed for the meeting
    detail: dict = field(default_factory=dict)

    @property
    def in_progress(self) -> bool:
        return any(part.end_at is None for part in self.parts)


@dataclass(frozen=True)
class TranscriptPartRef:
    """A SELECTED transcript part (lecture_transcript_selection_parts + artifact)."""
    artifact_id: str
    part_index: int
    call_id: str | None
    content_correlation_id: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class PartMapping:
    """One recording part's answer: its file, how it was proven, or why not."""
    part_index: int
    recording: RecordingPart
    locate_status: str
    reason: str
    candidate: object | None = None            # DriveItemCandidate
    lead_seconds: float | None = None
    duration_difference_seconds: float | None = None
    media_origin: str | None = None
    classification: str | None = None
    transcript_artifact_id: str | None = None
    transcript_part_index: int | None = None
    transcript_pairing: str | None = None
    candidate_count: int = 0

    @property
    def located(self) -> bool:
        return self.locate_status == LOCATED


# -- occurrence ----------------------------------------------------------------------------------

def parse_recordings(rows) -> tuple[tuple, int]:
    """(distinct RecordingParts, invalid row count). Same id, or same created+end, is one recording."""
    found, invalid, seen = [], 0, set()
    for row in rows or ():
        if not isinstance(row, dict):
            invalid += 1
            continue
        recording_id, created = row.get("id"), _time(row.get("createdDateTime"))
        if not recording_id or created is None:
            invalid += 1
            continue
        end = _time(row.get("endDateTime"))
        keys = {("id", recording_id), ("moment", created, end)}
        if keys & seen:
            continue
        seen |= keys
        found.append(RecordingPart(recording_id=str(recording_id), call_id=str(row.get("callId") or "").lower(),
                                   created_at=created, end_at=end,
                                   content_correlation_id=row.get("contentCorrelationId") or None))
    return tuple(sorted(found, key=lambda r: (r.created_at, r.recording_id))), invalid


def select_occurrence(rows, *, scheduled_start: datetime, scheduled_end: datetime, session_date) -> OccurrenceRecordings:
    recordings, invalid = parse_recordings(rows)
    detail = {"recordings_listed": len(recordings), "invalid_rows": invalid,
              "window_before_minutes": WINDOW_BEFORE.total_seconds() / 60,
              "window_after_minutes": WINDOW_AFTER.total_seconds() / 60}
    if invalid:
        # One unreadable row could be this lecture's part: never decide around it.
        return OccurrenceRecordings(GRAPH_RESPONSE_INVALID, all_recordings=recordings, detail=detail)
    window_start, window_end = scheduled_start - WINDOW_BEFORE, scheduled_end + WINDOW_AFTER
    relevant = [r for r in recordings if cairo_date_of(r.created_at) == session_date
                and r.effective_end >= window_start and r.created_at <= window_end]
    detail["window_recordings"] = len(relevant)
    if not relevant:
        return OccurrenceRecordings(NO_RECORDING_FOR_OCCURRENCE, all_recordings=recordings, detail=detail)

    def overlap(r):
        return max(0.0, (min(r.effective_end, scheduled_end) - max(r.created_at, scheduled_start)).total_seconds())

    primary = min(relevant, key=lambda r: (-overlap(r), -(r.duration_seconds or 0.0),
                                           abs((r.created_at - scheduled_start).total_seconds()), r.recording_id))
    selected = [primary]
    start, end = primary.created_at, primary.effective_end
    added = True
    while added:
        added = False
        for r in relevant:
            if r in selected:
                continue
            gap = (start - r.effective_end if r.effective_end < start
                   else r.created_at - end if r.created_at > end else timedelta(0))
            if (r.call_id and any(p.call_id == r.call_id for p in selected)) or gap <= MAX_PART_GAP:
                selected.append(r)
                start, end = min(start, r.created_at), max(end, r.effective_end)
                added = True
    selected.sort(key=lambda r: (r.created_at, r.recording_id))
    leftovers = [r for r in relevant if r not in selected and overlap(r) > 0]
    detail.update(selected_recordings=len(selected), unattached_window_recordings=len(relevant) - len(selected))
    if leftovers:
        detail["unattached_overlapping"] = len(leftovers)
        return OccurrenceRecordings(AMBIGUOUS_OCCURRENCE_RECORDINGS, parts=tuple(selected),
                                    all_recordings=recordings, detail=detail)
    return OccurrenceRecordings(SELECTED, parts=tuple(selected), all_recordings=recordings, detail=detail)


# -- files -------------------------------------------------------------------------------------------

def assign_files(parts, candidates, *, subject, session_date: str) -> tuple[list, dict]:
    """
    Per part, the candidate files that pass the identity rule for THAT part
    (copies collapsed), plus diagnostics. A file fitting several parts goes to
    none of them: it is reported, and those parts are ambiguous.
    """
    expected = normalize_subject(subject)
    per_part = {p.recording_id: [] for p in parts}
    shared: dict[str, int] = {}
    diagnostics = {"candidate_files": 0, "same_subject_date": 0, "subject_mismatch_in_window": 0,
                   "files_fitting_several_parts": 0}
    for candidate in candidates:
        if candidate.file_timestamp is None:
            continue
        diagnostics["candidate_files"] += 1
        fits = [(p, round(p.created_at.timestamp() - candidate.file_timestamp, 3)) for p in parts]
        fits = [(p, lead) for p, lead in fits if 0 <= lead <= MAX_FILE_LEAD_SECONDS]
        same_date = candidate.file_date == session_date
        if not expected or candidate.subject_key != expected or not same_date:
            if fits and same_date:
                diagnostics["subject_mismatch_in_window"] += 1
            continue
        diagnostics["same_subject_date"] += 1
        if len(fits) > 1:
            diagnostics["files_fitting_several_parts"] += 1
            for p, _ in fits:
                shared[p.recording_id] = shared.get(p.recording_id, 0) + 1
            continue
        for p, lead in fits:
            per_part[p.recording_id].append(Exact(candidate, lead, p))
    assigned = []
    for p in parts:
        kept, _ = collapse_copies(per_part[p.recording_id]) if per_part[p.recording_id] else ([], 0)
        assigned.append((p, kept, shared.get(p.recording_id, 0)))
    return assigned, diagnostics


def check_duration(part: RecordingPart, file_duration: float | None, lead: float) -> tuple[str, str, str | None, float | None]:
    """(locate_status, reason, media_origin, difference). The alignment rule, without a transcript."""
    reference = part.duration_seconds
    if reference is None:
        return DURATION_PENDING, "RECORDING_END_UNKNOWN", None, None
    if file_duration is None or file_duration <= 0:
        return DURATION_PENDING, "FILE_DURATION_UNAVAILABLE", None, None
    tolerance = max(MIN_DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_RATIO * reference)
    graph_gap, name_gap = abs(file_duration - reference), abs(file_duration - (reference + lead))
    graph_fits, name_fits = graph_gap <= tolerance, name_gap <= tolerance
    if graph_fits and name_fits and lead > SAME_ORIGIN_SECONDS:
        return AMBIGUOUS, "MEDIA_ORIGIN_AMBIGUOUS", None, round(graph_gap, 3)
    if graph_fits:
        return LOCATED, "EXACT_RECORDING_FILE_MATCHED", METHOD_GRAPH_CREATED, round(file_duration - reference, 3)
    if name_fits:
        return LOCATED, "EXACT_RECORDING_FILE_MATCHED", METHOD_FILE_NAME, round(file_duration - reference - lead, 3)
    return DURATION_MISMATCH, "FILE_DURATION_CONTRADICTS_RECORDING", None, round(file_duration - reference, 3)


def only_duration_match(part: RecordingPart, kept: list):
    """
    Of several distinct files that fit one part by subject, date and start window, the ONE whose
    duration proves it is this recording - or None. Decides only when the recording's own duration
    is known, EVERY file has a known duration, exactly one file passes check_duration (LOCATED)
    and every other file contradicts it (DURATION_MISMATCH). A missing duration, a second match
    or an origin ambiguity decides nothing: the part stays SEVERAL_FILES_FOR_PART. The files
    passed over are not touched - a short stray recording stays where it is, unimported.
    """
    if part.duration_seconds is None or len(kept) < 2:
        return None
    verdicts = []
    for exact in kept:
        duration = exact.candidate.duration_seconds
        if duration is None or duration <= 0:
            return None
        verdicts.append((exact, check_duration(part, duration, exact.lead)[0]))
    matches = [exact for exact, status in verdicts if status == LOCATED]
    if len(matches) != 1 or any(status not in (LOCATED, DURATION_MISMATCH) for _, status in verdicts):
        return None
    return matches[0]


def map_part(index: int, part: RecordingPart, kept: list, shared: int, *, expected_duration,
             policy: ResolutionPolicy = DEFAULT_POLICY) -> PartMapping:
    """A part's mapping from its assigned (and, where needed, enriched) candidates."""
    classification = policy.classify(part.duration_seconds, expected_duration)
    base = {"part_index": index, "recording": part, "classification": classification}
    considered = len(kept)
    if not shared and len(kept) > 1:
        chosen = only_duration_match(part, kept)
        kept = [chosen] if chosen is not None else kept
    if shared or len(kept) > 1:
        return PartMapping(locate_status=AMBIGUOUS, candidate_count=len(kept) + shared,
                           reason="FILE_FITS_SEVERAL_PARTS" if shared else "SEVERAL_FILES_FOR_PART", **base)
    if not kept:
        return PartMapping(locate_status=NOT_FOUND, reason="RECORDING_FILE_NOT_FOUND", **base)
    [exact] = kept
    status, reason, origin, difference = check_duration(part, exact.candidate.duration_seconds, exact.lead)
    return PartMapping(locate_status=status, reason=reason, candidate=exact.candidate, lead_seconds=exact.lead,
                       duration_difference_seconds=difference, media_origin=origin, candidate_count=considered, **base)


def claimed_twice(mappings) -> list:
    """The same physical file resolved for two parts: both become ambiguous."""
    owners: dict = {}
    for m in mappings:
        if m.candidate is not None:
            owners.setdefault(m.candidate.physical_id, []).append(m.part_index)
    clash = {i for indexes in owners.values() if len(indexes) > 1 for i in indexes}
    return [replace(m, locate_status=AMBIGUOUS, reason="FILE_RESOLVED_FOR_SEVERAL_PARTS", media_origin=None)
            if m.part_index in clash else m for m in mappings]


# -- transcript corroboration ---------------------------------------------------------------------

def pair_transcripts(mappings, all_recordings, transcript_parts) -> tuple[list, str, str | None]:
    """(mappings with their transcript part, pairing status, conflict reason or None)."""
    if not transcript_parts:
        return list(mappings), NO_TRANSCRIPT, None
    ours = {m.recording.recording_id for m in mappings}
    elsewhere = {r.content_correlation_id for r in all_recordings
                 if r.recording_id not in ours and r.content_correlation_id}
    paired: dict[str, tuple] = {}
    unpaired = 0
    for tp in transcript_parts:
        by_corr = [m for m in mappings if tp.content_correlation_id
                   and m.recording.content_correlation_id == tp.content_correlation_id]
        if not by_corr and tp.content_correlation_id and tp.content_correlation_id in elsewhere:
            return list(mappings), CONFLICT, "TRANSCRIPT_PART_BELONGS_TO_ANOTHER_RECORDING"
        by_time = [m for m in mappings if tp.created_at is not None
                   and abs((m.recording.created_at - tp.created_at).total_seconds()) <= PAIR_TOLERANCE_SECONDS]
        matches, method = (by_corr, "content_correlation_id") if by_corr else (by_time, "created_at")
        if len(matches) > 1:
            return list(mappings), CONFLICT, "TRANSCRIPT_PAIRING_AMBIGUOUS"
        if not matches:
            unpaired += 1
            continue
        recording_id = matches[0].recording.recording_id
        if recording_id in paired:
            return list(mappings), CONFLICT, "SEVERAL_TRANSCRIPT_PARTS_FOR_ONE_RECORDING"
        paired[recording_id] = (tp.artifact_id, tp.part_index, method)
    transcript_calls = {tp.call_id for tp in transcript_parts if tp.call_id}
    recording_calls = {m.recording.call_id for m in mappings if m.recording.call_id}
    if transcript_calls and recording_calls and not transcript_calls & recording_calls:
        return list(mappings), CONFLICT, "CALL_ID_CONFLICT"
    result = [replace(m, transcript_artifact_id=paired[m.recording.recording_id][0],
                      transcript_part_index=paired[m.recording.recording_id][1],
                      transcript_pairing=paired[m.recording.recording_id][2])
              if m.recording.recording_id in paired else m for m in mappings]
    status = PAIRED if not unpaired and len(paired) == len(mappings) else PARTIAL
    return result, status, None


# -- previously stored parts --------------------------------------------------------------------------

def merge_with_stored(mappings, stored: dict) -> tuple[list, str | None]:
    """
    Keep what was verified before. `stored` maps graph_recording_id to the
    stored row ({part_index, locate_status, source_drive_id, source_item_id}).

    * a stored LOCATED part is never downgraded by a later read: a part that
      is now not found / pending keeps its verified file;
    * a stored LOCATED part that now resolves to a DIFFERENT file, or now has
      another place in the recording order, or is no longer part of this
      occurrence, is a conflict for review - never re-pointed automatically.
    """
    current = {m.recording.recording_id: m for m in mappings}
    for recording_id, row in stored.items():
        if row["locate_status"] != LOCATED:
            continue
        mapping = current.get(recording_id)
        if mapping is None or mapping.part_index != row["part_index"]:
            return list(mappings), "RECORDING_SET_CHANGED"
        if mapping.candidate is not None and mapping.candidate.physical_id != (
                row["source_drive_id"], row["source_item_id"]) and mapping.locate_status in (LOCATED, DURATION_PENDING):
            return list(mappings), "SOURCE_CONFLICT_WITH_VERIFIED_FILE"
    merged = [replace(m, locate_status=LOCATED, reason="PREVIOUSLY_VERIFIED_SOURCE_KEPT", candidate=None)
              if not m.located and stored.get(m.recording.recording_id, {}).get("locate_status") == LOCATED
              else m for m in mappings]
    return merged, None


def _time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_graph_datetime(value)
    except (TypeError, ValueError):
        return None
