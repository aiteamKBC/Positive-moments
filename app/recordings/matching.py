"""
Candidate matching: the safe matching contract, as a pure function.

A driveItem is an EXACT candidate for the lecture's recording only if all of
these hold:
  * the Graph recording(s) for the lecture's exact call id were resolved
    through the organizer route;
  * its file name carries the lecture's exact normalized subject;
  * its file name is dated the lecture's own business date;
  * its file-name timestamp is at or before the createdDateTime of one of those
    Graph recordings, by at most MAX_FILE_LEAD_SECONDS (a file named after
    Graph's timestamp never matches - no evidence supports that case).

Exactly one exact candidate for exactly one Graph recording is the match, as
it always was. Anything more is handed to app/recordings/resolution.py, which
selects the full delivery over a short fragment and otherwise fails closed
(AMBIGUOUS_RECORDING_FILES, MULTIPART_RECORDING, GRAPH_RECORDING_AMBIGUOUS).
Anything else is a reason, never a guess.
"""
from __future__ import annotations

from app.common.time import parse_graph_datetime
from app.recordings.candidates import normalize_subject
from app.recordings.models import (
    EXACT_RECORDING_FILE_MATCHED,
    MAX_FILE_LEAD_SECONDS,
    RECORDING_FILE_NOT_FOUND,
    SUBJECT_MISMATCH,
    TIMESTAMP_MISMATCH,
    DriveItemCandidate,
    GraphRecordingLookup,
    MatchResult,
)
from app.recordings.resolution import DEFAULT_POLICY, Exact, ResolutionPolicy, resolve


def lead_seconds(graph_created_at: str, candidate: DriveItemCandidate) -> float | None:
    """How far the file's name precedes Graph's createdDateTime (negative = after)."""
    if candidate.file_timestamp is None:
        return None
    graph_ts = parse_graph_datetime(graph_created_at).timestamp()
    return round(graph_ts - candidate.file_timestamp, 3)


def timestamp_ok(lead: float | None) -> bool:
    return lead is not None and 0 <= lead <= MAX_FILE_LEAD_SECONDS


def pair(candidate: DriveItemCandidate, recordings) -> tuple:
    """
    (lead, recording) for the Graph recording this file belongs to: the one
    whose createdDateTime it precedes by the least within the allowed window;
    failing that, the nearest one (so a mismatch can still report its lead).
    """
    leads = [(lead_seconds(r.created_at, candidate), r) for r in recordings]
    inside = [pair_ for pair_ in leads if timestamp_ok(pair_[0])]
    if inside:
        return min(inside, key=lambda pair_: pair_[0])
    return min(leads, key=lambda pair_: abs(pair_[0]))


def match_candidates(*, subject: str | None, session_date: str,
                     graph: GraphRecordingLookup, candidates,
                     expected_duration_seconds: float | None = None,
                     policy: ResolutionPolicy = DEFAULT_POLICY) -> MatchResult:
    if graph is None or not graph.resolvable:
        raise ValueError("matching requires an exactly resolved Graph recording")
    recordings = graph.logical_recordings
    expected = normalize_subject(subject)
    parsed = [c for c in candidates if c.file_timestamp is not None]
    total = len(parsed)
    if not expected:
        return MatchResult(status=SUBJECT_MISMATCH, candidate_file_count=total)

    assessed = []
    for candidate in parsed:
        lead, recording = pair(candidate, recordings)
        assessed.append((candidate, lead, recording,
                         candidate.subject_key == expected,
                         candidate.file_date == session_date,
                         timestamp_ok(lead)))

    exact = sorted((Exact(c, lead, recording)
                    for c, lead, recording, subject_ok, date_ok, time_ok in assessed
                    if subject_ok and date_ok and time_ok),
                   key=lambda e: (e.lead, e.candidate.drive_id, e.candidate.item_id))
    same_subject_leads = [lead for _, lead, _, subject_ok, date_ok, _ in assessed
                          if subject_ok and date_ok]
    nearest = min(same_subject_leads, key=abs) if same_subject_leads else None
    common = {"candidate_file_count": total, "exact_candidate_count": len(exact),
              "nearest_same_subject_lead_seconds": nearest,
              "exact_candidates": tuple(e.candidate for e in exact)}

    if exact:
        resolved = resolve(exact, recordings=recordings,
                           expected=expected_duration_seconds, policy=policy)
        if resolved.status == EXACT_RECORDING_FILE_MATCHED:
            return MatchResult(status=EXACT_RECORDING_FILE_MATCHED,
                               candidate=resolved.chosen.candidate,
                               timestamp_difference_seconds=resolved.chosen.lead,
                               resolution=resolved.detail, **common)
        return MatchResult(status=resolved.status,
                           ambiguous_filenames=tuple(e.candidate.name for e in exact),
                           resolution=resolved.detail, **common)
    if same_subject_leads:
        return MatchResult(status=TIMESTAMP_MISMATCH, **common)
    if any(date_ok and time_ok for *_, date_ok, time_ok in assessed):
        return MatchResult(status=SUBJECT_MISMATCH, **common)
    return MatchResult(status=RECORDING_FILE_NOT_FOUND, **common)
