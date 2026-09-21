"""
Phase 4A Part R: the day reconciliation report.

One question, answered completely: for a given date, where is every lecture?

It is READ-ONLY and it is DERIVED. Nothing here is stored, because a stored
summary is a second source of truth that starts drifting the moment the tables
it summarises change. Recomputing it is cheap and always right; caching it
would be cheaper and sometimes wrong.

The counts are a PARTITION over the day's lectures - every lecture lands in
exactly one bucket and the buckets sum to the canonical count. That is asserted
by a test rather than trusted, because an operations dashboard whose numbers do
not add up teaches people to ignore it.
"""
from datetime import date as _date

from app.orchestration.stages import (
    ATTENDANCE,
    COMPLETE,
    EXCEL_SYNC,
    FAILED,
    LEGACY_QA_SYNC,
    MISSING,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    ORCHESTRATION_VERSION,
    PERFECT_ELIGIBILITY,
    PERFECT_SYNC,
    RECORDING_LINK,
    REVIEW_REQUIRED,
    STAGE_ORDER,
    WAITING,
)
from app.qa.perfect import PENDING_ATTENDANCE_DATA


BUCKET_COMPLETE = "complete"
BUCKET_WAITING = "waiting"
BUCKET_REVIEW = "review"
BUCKET_FAILED = "failed"
BUCKET_IN_PROGRESS = "in_progress"
# Phase 4C1. A duplicate calendar event that the deterministic rule retired.
# It gets its own bucket rather than joining `complete`, because it is not a
# lecture that finished - it is an event that was never a lecture. Folding it
# into any existing bucket would overstate exactly one number, and a report
# that overstates its own throughput is worse than one that omits the row.
BUCKET_SUPPRESSED_DUPLICATE = "suppressed_duplicate"

BUCKETS = (BUCKET_COMPLETE, BUCKET_WAITING, BUCKET_REVIEW, BUCKET_FAILED,
           BUCKET_IN_PROGRESS, BUCKET_SUPPRESSED_DUPLICATE)


def bucket_for(state: dict) -> str:
    """
    Which single bucket a lecture belongs to.

    Order matters and is severity-first, unlike the resume rule, which is
    earliest-stage-first. They answer different questions: the resume rule asks
    "what do we do next?", this asks "how worried should somebody be?".
    """
    stages = state["stages"]
    if state.get("is_suppressed_duplicate"):
        # Checked first, and on the explicit flag rather than on stage states.
        # A suppressed duplicate is every-stage NOT_APPLICABLE and would
        # otherwise count as complete, which is the one reading of it that is
        # actively misleading.
        return BUCKET_SUPPRESSED_DUPLICATE
    if any(item["state"] == FAILED for item in stages.values()):
        return BUCKET_FAILED
    if any(item["state"] == REVIEW_REQUIRED for item in stages.values()):
        return BUCKET_REVIEW
    if state["next_executable_action"] == NOTHING_TO_DO:
        # Everything the coded platform owns is done. A missing recording link
        # or Excel stamp belongs to a live legacy workflow and is reported
        # separately rather than holding the lecture open.
        return BUCKET_COMPLETE
    if any(item["state"] == WAITING for item in stages.values()):
        return BUCKET_WAITING
    return BUCKET_IN_PROGRESS


class DayReconciliation:
    """Derive the day report from the canonical stage matrix. Read-only."""

    def __init__(self, *, resolver):
        self.resolver = resolver

    def for_day(self, connection, session_date: _date) -> dict:
        states = self.resolver.for_day(connection, session_date)
        return self.from_states(session_date, states)

    def from_states(self, session_date, states) -> dict:
        buckets = {name: 0 for name in BUCKETS}
        matrix = []
        for state in states:
            bucket = bucket_for(state)
            buckets[bucket] += 1
            matrix.append(_row(state, bucket))

        # Every counter below this line describes BUSINESS lectures - the
        # occurrences somebody still has to care about. A suppressed duplicate
        # would otherwise appear as an extra "nothing to do" lecture and an
        # extra not-coded-owned legacy row, neither of which is true of it.
        # `by_stage` deliberately keeps every row, because that view is the
        # audit trail and the extra calendar event belongs in it.
        business = [state for state in states
                    if not state.get("is_suppressed_duplicate")]
        stage_counts = {
            stage: _tally(state["stages"][stage]["state"] for state in states)
            for stage in STAGE_ORDER}

        return {
            "session_date": (session_date.isoformat()
                             if hasattr(session_date, "isoformat")
                             else str(session_date)),
            "orchestration_version": ORCHESTRATION_VERSION,
            "canonical_lecture_count": len(states),
            # What the calendar produced, and what it actually meant. The
            # extra event stays visible - hiding it would make the registry
            # and the report disagree about how many rows exist - but it is
            # not counted as a lecture anybody has to process.
            "suppressed_duplicate_count": buckets[BUCKET_SUPPRESSED_DUPLICATE],
            "business_lecture_count": (len(states)
                                       - buckets[BUCKET_SUPPRESSED_DUPLICATE]),
            "complete_count": buckets[BUCKET_COMPLETE],
            "waiting_count": buckets[BUCKET_WAITING],
            "review_count": buckets[BUCKET_REVIEW],
            "failed_count": buckets[BUCKET_FAILED],
            "in_progress_count": buckets[BUCKET_IN_PROGRESS],

            "legacy_synced_count": _stage_is(business, LEGACY_QA_SYNC, COMPLETE),
            "legacy_not_coded_owned_count": _stage_is(business, LEGACY_QA_SYNC,
                                                      NOT_APPLICABLE),
            "perfect_eligible_count": sum(
                1 for state in business
                if state["stages"][PERFECT_ELIGIBILITY].get("is_perfect")),
            "perfect_pending_attendance_count": sum(
                1 for state in business
                if state["stages"][PERFECT_ELIGIBILITY].get("reason")
                == PENDING_ATTENDANCE_DATA),
            "perfect_synced_count": _stage_is(business, PERFECT_SYNC, COMPLETE),
            "recording_missing_count": _stage_is(business, RECORDING_LINK, MISSING),
            "excel_pending_count": _stage_is(business, EXCEL_SYNC, MISSING),
            "attendance_waiting_count": _stage_is(business, ATTENDANCE, WAITING),

            "by_next_action": _tally(state["next_executable_action"]
                                     for state in business),
            "by_stage": stage_counts,
            "lectures": matrix,
        }

    def for_window(self, connection, days) -> dict:
        reports = [self.for_day(connection, day) for day in days]
        totals = {}
        for report in reports:
            for key, value in report.items():
                if key.endswith("_count") and isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return {"days": [report["session_date"] for report in reports],
                "totals": totals, "reports": reports}


def _row(state, bucket) -> dict:
    return {
        "lecture_id": state["lecture_id"], "subject": state["subject"],
        "session_date": state["session_date"], "bucket": bucket,
        # Descriptive, not derived: the console needs a human heading for the
        # row and should not have to fetch the registry again to get one.
        "module": state.get("module"),
        "scheduled_start": state.get("scheduled_start"),
        "next_action": state["next_action"],
        "is_suppressed_duplicate": bool(state.get("is_suppressed_duplicate")),
        "duplicate_winner_lecture_id": (state.get("duplicate_resolution") or {})
            .get("winner_lecture_id"),
        "next_executable_action": state["next_executable_action"],
        "blocking_stage": state["blocking_stage"],
        "attendance_coverage_status": state["attendance_coverage_status"],
        "attendance_source_authoritative":
            state["attendance_source_authoritative"],
        "perfect_reason": state["stages"][PERFECT_ELIGIBILITY].get("reason"),
        # Reason codes are preserved verbatim. An operator chasing a stuck
        # lecture needs the platform's own word for why, not a paraphrase.
        "reason_codes": {stage: item.get("reason")
                         for stage, item in state["stages"].items()
                         if item.get("reason")},
        "stages": {stage: item["state"] for stage, item in state["stages"].items()},
    }


def _stage_is(states, stage, value) -> int:
    return sum(1 for state in states if state["stages"][stage]["state"] == value)


def _tally(values) -> dict:
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))
