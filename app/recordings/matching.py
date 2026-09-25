"""
Candidate matching: the safe matching contract, as a pure function.

A driveItem is THE lecture's recording only if all of these hold:
  * the Graph recording was resolved exactly (organizer route, exact call id);
  * its file name carries the lecture's exact normalized subject;
  * its file name is dated the lecture's own business date;
  * its file-name timestamp is at or before Graph's createdDateTime, by at
    most MAX_FILE_LEAD_SECONDS (a file named after Graph's timestamp never
    matches - no evidence supports that case);
  * it is the ONLY candidate for which all of the above hold.

Anything else is a reason, never a guess.
"""
from __future__ import annotations

from app.common.time import parse_graph_datetime
from app.recordings.candidates import normalize_subject
from app.recordings.models import (
    AMBIGUOUS_RECORDING_FILES,
    EXACT_RECORDING_FILE_MATCHED,
    GRAPH_RECORDING_FOUND,
    MAX_FILE_LEAD_SECONDS,
    RECORDING_FILE_NOT_FOUND,
    SUBJECT_MISMATCH,
    TIMESTAMP_MISMATCH,
    DriveItemCandidate,
    GraphRecordingLookup,
    MatchResult,
)


def lead_seconds(graph_created_at: str, candidate: DriveItemCandidate) -> float | None:
    """How far the file's name precedes Graph's createdDateTime (negative = after)."""
    if candidate.file_timestamp is None:
        return None
    graph_ts = parse_graph_datetime(graph_created_at).timestamp()
    return round(graph_ts - candidate.file_timestamp, 3)


def timestamp_ok(lead: float | None) -> bool:
    return lead is not None and 0 <= lead <= MAX_FILE_LEAD_SECONDS


def match_candidates(*, subject: str | None, session_date: str,
                     graph: GraphRecordingLookup, candidates) -> MatchResult:
    if graph is None or graph.status != GRAPH_RECORDING_FOUND or not graph.created_at:
        raise ValueError("matching requires an exactly resolved Graph recording")
    expected = normalize_subject(subject)
    parsed = [c for c in candidates if c.file_timestamp is not None]
    total = len(parsed)
    if not expected:
        return MatchResult(status=SUBJECT_MISMATCH, candidate_file_count=total)

    assessed = []
    for candidate in parsed:
        lead = lead_seconds(graph.created_at, candidate)
        assessed.append((candidate, lead,
                         candidate.subject_key == expected,
                         candidate.file_date == session_date,
                         timestamp_ok(lead)))

    exact = sorted(((c, lead) for c, lead, subject_ok, date_ok, time_ok in assessed
                    if subject_ok and date_ok and time_ok),
                   key=lambda pair: pair[1])
    same_subject_leads = [lead for _, lead, subject_ok, date_ok, _ in assessed
                          if subject_ok and date_ok]
    nearest = min(same_subject_leads, key=abs) if same_subject_leads else None
    common = {"candidate_file_count": total, "exact_candidate_count": len(exact),
              "nearest_same_subject_lead_seconds": nearest}

    if len(exact) == 1:
        candidate, lead = exact[0]
        return MatchResult(status=EXACT_RECORDING_FILE_MATCHED, candidate=candidate,
                           timestamp_difference_seconds=lead, **common)
    if len(exact) > 1:
        return MatchResult(status=AMBIGUOUS_RECORDING_FILES,
                           ambiguous_filenames=tuple(c.name for c, _ in exact),
                           **common)
    if same_subject_leads:
        return MatchResult(status=TIMESTAMP_MISMATCH, **common)
    if any(date_ok and time_ok for _, _, _, date_ok, time_ok in assessed):
        return MatchResult(status=SUBJECT_MISMATCH, **common)
    return MatchResult(status=RECORDING_FILE_NOT_FOUND, **common)
