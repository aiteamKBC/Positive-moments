"""
Glue for P0-A: evaluate lectures against an LMS snapshot, persist decisions,
classify LMS-side coverage. No scheduling, no daemon, no side effects outside
the Lecture Intelligence connection the caller passes in.
"""
from __future__ import annotations

import collections

from app.db.repositories.lms_matches import LmsMatchRepository
from app.lms.coverage import classify_snapshot
from app.lms.matching import MatchConfig, MatchResult, match_lecture


def evaluate(lectures, snapshot, config: MatchConfig) -> list[MatchResult]:
    return [match_lecture(lecture, snapshot, config) for lecture in lectures]


def summarize(results) -> dict:
    by_status = collections.Counter(r.status for r in results)
    shared = sum(1 for r in results if len(r.links) > 1)
    return {"lectures": len(results), "by_status": dict(sorted(by_status.items())),
            "requires_review": sum(1 for r in results if r.requires_review),
            "verified_with_shared_modules": shared}


def persist(connection, lectures, results, repository: LmsMatchRepository | None = None) -> dict:
    repository = repository or LmsMatchRepository()
    starts = {l.lecture_id: l.scheduled_start for l in lectures}
    outcome = collections.Counter()
    for result in results:
        recorded = repository.record(connection, result, lecture_scheduled_start=starts[result.lecture_id])
        outcome[(recorded.status, "CHANGED" if recorded.changed else "UNCHANGED")] += 1
    return {f"{status}:{kind}": n for (status, kind), n in sorted(outcome.items())}


def reconcile_coverage(connection, snapshot, config: MatchConfig, now, *,
                       repository: LmsMatchRepository | None = None, owners: dict | None = None, **kwargs):
    """
    Classify every LMS occurrence in the snapshot. Owners (occurrence -> lecture)
    come from current VERIFIED links unless given (a dry run passes the owners
    it just computed). Persist the rows with LmsMatchRepository.upsert_coverage.
    """
    repository = repository or LmsMatchRepository()
    if owners is None:
        owners = repository.current_occurrence_owners(connection, [o.occurrence_id for o in snapshot.occurrences])
    return classify_snapshot(snapshot, owners, config, now, **kwargs)
