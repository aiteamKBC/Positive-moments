"""
The LMS side of the two-sided reconciliation (investigation section 9).

Every LMS occurrence in the window gets exactly one coverage state, so a
session the LMS ran but LI never discovered (the 2 Oct week-additional events)
surfaces instead of disappearing. Pure; persistence is in
app/db/repositories/lms_matches.py. No daemon calls this yet (P0-A).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.lms.matching import RULE_VERSION, MatchConfig, is_active, is_test

IN_SCOPE = "IN_SCOPE"
TEST = "TEST_EXCLUDED"
INACTIVE = "INACTIVE"

NOT_YET_DUE = "NOT_YET_DUE"
MATCHED = "MATCHED"
LMS_ONLY_PENDING = "LMS_ONLY_PENDING"
LMS_SESSION_NOT_DISCOVERED = "LMS_SESSION_NOT_DISCOVERED"

# LI discovery today runs twice nightly (median 8 h after a lecture), so an
# occurrence is "pending" for a day before it is called a discovery gap.
DEFAULT_PENDING_AFTER = timedelta(hours=2)
DEFAULT_GAP_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class CoverageRow:
    occurrence: object
    scope_status: str
    coverage_status: str
    matched_lecture_id: str | None
    evidence: dict = field(default_factory=dict)
    rule_version: str = RULE_VERSION


def classify_occurrence(occurrence, matched_lecture_id, config: MatchConfig, now: datetime, *,
                        pending_after: timedelta = DEFAULT_PENDING_AFTER,
                        gap_after: timedelta = DEFAULT_GAP_AFTER) -> CoverageRow:
    if not is_active(occurrence, config):
        return CoverageRow(occurrence, INACTIVE, INACTIVE, None,
                           {"occurrence_status": occurrence.status, "series_status": occurrence.series_status})
    if is_test(occurrence, config):
        return CoverageRow(occurrence, TEST, TEST, None, {"programme": occurrence.programme_name})
    if matched_lecture_id:
        return CoverageRow(occurrence, IN_SCOPE, MATCHED, str(matched_lecture_id))
    ended = occurrence.scheduled_end
    if now < ended + pending_after:
        return CoverageRow(occurrence, IN_SCOPE, NOT_YET_DUE, None)
    status = LMS_ONLY_PENDING if now < ended + gap_after else LMS_SESSION_NOT_DISCOVERED
    return CoverageRow(occurrence, IN_SCOPE, status, None,
                       {"meeting_type": occurrence.meeting_type, "hours_since_end":
                        round((now - ended).total_seconds() / 3600, 1)})


def classify_snapshot(snapshot, matched: dict, config: MatchConfig, now: datetime, **kwargs) -> list[CoverageRow]:
    """`matched`: occurrence_id -> lecture_id from current VERIFIED links."""
    return [classify_occurrence(o, matched.get(o.occurrence_id), config, now, **kwargs)
            for o in snapshot.occurrences]
