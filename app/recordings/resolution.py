"""
Full-recording resolution: several candidates that ALL pass the identity rule.

matching.py decides which files are this lecture's recording by identity
(exact call id, subject, business date, file-name lead). Sometimes more than
one file passes. That happens for four different reasons, and each has its own
answer:

  1. the SAME physical file seen by two discovery sources
       -> already one candidate (candidates.deduplicate, by drive + item);
     or a byte-identical COPY under another item id (same name, same size)
       -> one logical file, the copy in the canonical Teams location is kept;
  2. a false start / setup fragment next to the real delivery
       -> the full recording is selected;
  3. an interrupted delivery: two or more SUBSTANTIAL parts
       -> MULTIPART_RECORDING, review (recording_url holds one URL);
  4. two plausible full recordings
       -> ambiguous, review. Never "the longer one".

"Full" is judged against the lecture's OWN schedule, never a fixed length:

    expected = scheduled_end - scheduled_start
    FULL      duration >= full_min_expected_ratio      x expected
    FRAGMENT  duration <= fragment_max_expected_ratio  x expected
    PARTIAL   anything between: substantial, but not a whole lecture

Duration comes from the driveItem's video facet (measured media length), or,
when a Graph callRecording pairs with exactly one file, from Graph's
createdDateTime -> endDateTime. File size is a fallback only, and only when one
file outweighs every other by `size_dominance_ratio` - never max(size).

Pure functions. Every threshold is a ResolutionPolicy field with a safe default
(app/config/settings.py can override them), and every outcome carries a
`resolution` record that explains itself without any URL.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.recordings.models import (
    AMBIGUOUS_RECORDING_FILES,
    EXACT_RECORDING_FILE_MATCHED,
    GRAPH_RECORDING_AMBIGUOUS,
    MULTIPART_RECORDING,
    RECORDING_FILE_NOT_FOUND,
    RESOLUTION_POLICY_VERSION,
)


FULL = "FULL"
PARTIAL = "PARTIAL"
FRAGMENT = "FRAGMENT"
UNKNOWN = "UNKNOWN"

# Resolution rules, as persisted in `resolution.rule`.
RULE_SINGLE = "single_exact_candidate"
RULE_FULL_OVER_FRAGMENT = "full_recording_selected_over_short_fragment"
RULE_SIZE_OVER_FRAGMENT = "size_dominant_recording_selected_over_short_fragment"
RULE_FULL_GRAPH_OVER_FRAGMENT = "full_graph_recording_selected_over_short_fragment"
RULE_SEVERAL_FULL = "several_full_length_recordings"
RULE_SEVERAL_FULL_GRAPH = "several_full_length_graph_recordings"
RULE_MULTIPART = "several_substantial_recording_parts"
RULE_NO_FULL = "no_full_length_recording"
RULE_NO_EVIDENCE = "insufficient_media_evidence"
RULE_GRAPH_WITHOUT_FILE = "graph_recording_without_matching_file"
RULE_FULL_GRAPH_FILE_MISSING = "full_graph_recording_file_not_found"
RULE_CONFLICT = "file_and_graph_durations_disagree"
RULE_TOO_MANY = "too_many_candidates_for_media_evidence"

# Where the canonical copy of a Teams recording lives, most canonical first.
# Used ONLY to choose between byte-identical copies of one file.
SOURCE_PRIORITY = ("channel_recordings", "onedrive_recordings", "tenant_search")

# Media evidence is fetched for at most this many exact candidates; more than
# that is not a false-start pattern and stays in review.
MAX_EVIDENCE_CANDIDATES = 6


@dataclass(frozen=True)
class ResolutionPolicy:
    # For a 2 h slot: >= 60 min can be the delivered lecture ...
    full_min_expected_ratio: float = 0.50
    # ... <= 30 min next to it is a false start / setup fragment ...
    fragment_max_expected_ratio: float = 0.25
    # ... and without durations, the larger file must be >= 10x every other.
    # A 3-minute and a 2-hour Teams MP4 differ ~40x; two halves of a lecture
    # differ ~1-2x; 10x leaves a wide margin on both sides.
    size_dominance_ratio: float = 10.0
    version: str = RESOLUTION_POLICY_VERSION

    def __post_init__(self):
        if not 0 < self.fragment_max_expected_ratio < self.full_min_expected_ratio <= 1:
            raise ValueError("recording resolution needs 0 < fragment ratio < full ratio <= 1")
        if not self.size_dominance_ratio > 1:
            raise ValueError("recording size dominance ratio must be greater than 1")

    def thresholds(self) -> dict:
        return {"full_min_expected_ratio": self.full_min_expected_ratio,
                "fragment_max_expected_ratio": self.fragment_max_expected_ratio,
                "size_dominance_ratio": self.size_dominance_ratio}

    def classify(self, duration: float | None, expected: float | None) -> str:
        if duration is None or not expected:
            return UNKNOWN
        if duration >= self.full_min_expected_ratio * expected:
            return FULL
        if duration <= self.fragment_max_expected_ratio * expected:
            return FRAGMENT
        return PARTIAL


DEFAULT_POLICY = ResolutionPolicy()


@dataclass(frozen=True)
class Exact:
    """One exact candidate: the file, its lead, and the Graph recording it pairs with."""
    candidate: object
    lead: float
    recording: object          # GraphRecording


@dataclass(frozen=True)
class Resolution:
    status: str
    chosen: Exact | None
    detail: dict


def _source_rank(candidate) -> tuple:
    sources = candidate.sources or (candidate.source,)
    ranks = [SOURCE_PRIORITY.index(s) if s in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
             for s in sources]
    return (min(ranks), candidate.drive_id, candidate.item_id)


def collapse_copies(exact: list[Exact]) -> tuple[list[Exact], int]:
    """
    Byte-identical copies of ONE recording under different item ids.

    Merged only on strong evidence: the same file name (hence the same
    recording-start second) AND the same, known, non-zero byte size, and - if
    both are known - the same media duration. Equal names alone never merge:
    two different files can share a name. The kept copy is the one in the most
    canonical Teams location, then the lowest (drive, item) - deterministic.
    """
    groups: dict[tuple, list[Exact]] = {}
    for entry in exact:
        c = entry.candidate
        key = (("copy", c.name.casefold(), c.size_bytes) if c.size_bytes
               else ("file",) + c.physical_id)
        groups.setdefault(key, []).append(entry)
    kept, merged = [], 0
    for members in groups.values():
        durations = {round(m.candidate.duration_seconds) for m in members
                     if m.candidate.duration_seconds is not None}
        if len(members) > 1 and len(durations) > 1:
            kept.extend(members)                 # same size, different media: not a copy
            continue
        kept.append(min(members, key=lambda m: _source_rank(m.candidate)))
        merged += len(members) - 1
    kept.sort(key=lambda m: (m.lead, m.candidate.drive_id, m.candidate.item_id))
    return kept, merged


def _evidence(entry: Exact, duration, source, classification) -> dict:
    c = entry.candidate
    return {"file_name": c.name, "duration_seconds": _round(duration),
            "duration_source": source, "size_bytes": c.size_bytes,
            "classification": classification, "lead_seconds": entry.lead,
            "sources": list(c.sources or (c.source,)),
            "graph_recording_id": getattr(entry.recording, "recording_id", None)}


def _round(value):
    return round(value, 3) if value is not None else None


def resolve(exact: list[Exact], *, recordings: tuple, expected: float | None,
            policy: ResolutionPolicy = DEFAULT_POLICY) -> Resolution:
    """
    Resolve one lecture's exact candidates. `recordings` are the Graph
    callRecordings for the lecture's call id (one or several).
    """
    base = {"policy": policy.version, "thresholds": policy.thresholds(),
            "expected_duration_seconds": _round(expected),
            "graph_recording_count": len(recordings)}

    def outcome(status, rule, chosen=None, rejected=(), selected=None, **extra):
        detail = {**base, "rule": rule, **extra}
        if selected is not None:
            detail["selected"] = selected
        if rejected:
            detail["rejected"] = list(rejected)
        return Resolution(status, chosen, detail)

    exact, merged = collapse_copies(exact)
    base["exact_candidate_count"] = len(exact)
    base["logical_copies_merged"] = merged
    if not exact:
        return outcome(RECORDING_FILE_NOT_FOUND, RULE_NO_EVIDENCE)
    if len(recordings) <= 1 and len(exact) == 1:
        return outcome(EXACT_RECORDING_FILE_MATCHED, RULE_SINGLE, chosen=exact[0])

    # -- several Graph recordings for one call: which logical recording? ------
    if len(recordings) > 1:
        durations = [r.duration_seconds for r in recordings]
        base["graph_recordings"] = [
            {"recording_id": r.recording_id, "created_at": r.created_at,
             "end_at": r.end_at, "duration_seconds": _round(r.duration_seconds),
             "classification": policy.classify(r.duration_seconds, expected),
             "files_matched": sum(1 for e in exact if e.recording == r)}
            for r in recordings]
        if expected and all(d is not None for d in durations):
            classes = [policy.classify(d, expected) for d in durations]
            full = [r for r, k in zip(recordings, classes) if k == FULL]
            substantial = [r for r, k in zip(recordings, classes) if k in (FULL, PARTIAL)]
            if len(full) > 1:
                return outcome(GRAPH_RECORDING_AMBIGUOUS, RULE_SEVERAL_FULL_GRAPH)
            if len(substantial) > 1:
                return outcome(MULTIPART_RECORDING, RULE_MULTIPART)
            if not full:
                return outcome(GRAPH_RECORDING_AMBIGUOUS, RULE_NO_FULL)
            [real] = full
            ours = [e for e in exact if e.recording == real]
            others = [e for e in exact if e.recording != real]
            if not ours:
                # The fragment's file is visible but the delivery's is not
                # (still uploading?). Never link the fragment: retry.
                return outcome(RECORDING_FILE_NOT_FOUND, RULE_FULL_GRAPH_FILE_MISSING)
            if len(ours) == 1:
                chosen = ours[0]
                file_class = policy.classify(chosen.candidate.duration_seconds, expected)
                if file_class in (FRAGMENT, PARTIAL):
                    return outcome(AMBIGUOUS_RECORDING_FILES, RULE_CONFLICT)
                return outcome(
                    EXACT_RECORDING_FILE_MATCHED, RULE_FULL_GRAPH_OVER_FRAGMENT,
                    chosen=chosen,
                    selected=_evidence(chosen, real.duration_seconds, "graph_recording", FULL),
                    rejected=[_evidence(e, e.recording.duration_seconds, "graph_recording",
                                        FRAGMENT) for e in others])
            exact = ours                         # several files for the real one: by file
        elif any(not any(e.recording == r for e in exact) for r in recordings):
            # A Graph recording of unknown length has no file: it may be the
            # delivery. Nothing proves any visible file is the lecture.
            return outcome(GRAPH_RECORDING_AMBIGUOUS, RULE_GRAPH_WITHOUT_FILE)

    # -- several files: which one is the whole delivery? ----------------------
    return _resolve_files(exact, recordings=recordings, expected=expected,
                          policy=policy, outcome=outcome)


def _file_duration(entry: Exact, exact: list[Exact], recordings) -> tuple:
    if entry.candidate.duration_seconds is not None:
        return entry.candidate.duration_seconds, "video_facet"
    paired = [e for e in exact if e.recording == entry.recording]
    if len(recordings) > 1 and len(paired) == 1 and entry.recording.duration_seconds:
        # One file for one Graph recording: Graph's own start/end is its length.
        return entry.recording.duration_seconds, "graph_recording"
    return None, None


def _resolve_files(exact, *, recordings, expected, policy, outcome) -> Resolution:
    if len(exact) > MAX_EVIDENCE_CANDIDATES:
        return outcome(AMBIGUOUS_RECORDING_FILES, RULE_TOO_MANY)
    measured = []
    for entry in exact:
        duration, source = _file_duration(entry, exact, recordings)
        measured.append((entry, duration, source, policy.classify(duration, expected)))

    if expected and all(kind != UNKNOWN for *_, kind in measured):
        full = [m for m in measured if m[3] == FULL]
        substantial = [m for m in measured if m[3] in (FULL, PARTIAL)]
        evidence = [_evidence(*m) for m in measured]
        if len(full) > 1:
            return outcome(AMBIGUOUS_RECORDING_FILES, RULE_SEVERAL_FULL, candidates=evidence)
        if len(substantial) > 1:
            return outcome(MULTIPART_RECORDING, RULE_MULTIPART, candidates=evidence)
        if not full:
            return outcome(AMBIGUOUS_RECORDING_FILES, RULE_NO_FULL, candidates=evidence)
        [winner] = full
        return outcome(EXACT_RECORDING_FILE_MATCHED, RULE_FULL_OVER_FRAGMENT,
                       chosen=winner[0], selected=_evidence(*winner),
                       rejected=[_evidence(*m) for m in measured if m is not winner])

    # -- size fallback: durations unavailable ---------------------------------
    sizes = [m[0].candidate.size_bytes for m in measured]
    if not expected or any(not size for size in sizes):
        return outcome(AMBIGUOUS_RECORDING_FILES, RULE_NO_EVIDENCE,
                       candidates=[_evidence(*m) for m in measured])
    ranked = sorted(measured, key=lambda m: m[0].candidate.size_bytes, reverse=True)
    largest, rest = ranked[0], ranked[1:]
    dominant = all(largest[0].candidate.size_bytes
                   >= policy.size_dominance_ratio * m[0].candidate.size_bytes for m in rest)
    # Any duration that IS known must agree: the large one is not short, and
    # every small one is a fragment. A single Graph recording of known length
    # must itself be a full delivery.
    consistent = (largest[3] in (FULL, UNKNOWN)
                  and all(m[3] in (FRAGMENT, UNKNOWN) for m in rest))
    if len(recordings) == 1:
        consistent = consistent and policy.classify(
            recordings[0].duration_seconds, expected) in (FULL, UNKNOWN)
    if dominant and consistent:
        return outcome(EXACT_RECORDING_FILE_MATCHED, RULE_SIZE_OVER_FRAGMENT,
                       chosen=largest[0], selected=_evidence(*largest),
                       rejected=[_evidence(*m) for m in rest])
    return outcome(AMBIGUOUS_RECORDING_FILES, RULE_NO_EVIDENCE,
                   candidates=[_evidence(*m) for m in measured])
