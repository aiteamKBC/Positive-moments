"""
The LMS live-session register as the attendance source for lectures the legacy
source no longer answers.

WHY
---
`public.kbc_attendance` stopped being filled for most modules from the week of
2026-10-19. The LMS keeps the register in
`curriculum.live_session_learner_attendance`: one row per (occurrence, learner
profile, module) with a FINAL `attendance_status` in ('present', 'absent').

WHERE IT IS READ
----------------
Through the pipeline's own database connection (DATABASE_URL): that database already
exposes the LMS register as postgres_fdw foreign tables in schema `lms_sync`
(`live_session_learner_attendance`, `live_session_occurrences`, `live_sessions`,
server `lms_attendance_source`). So every process that runs attendance - scheduler,
backfill, Operations, CLI - reaches it with no new setting and without
LMS_DATABASE_URL. Plain SELECTs only, inside a savepoint: if the foreign tables are
missing or fail, the savepoint is rolled back, the decision is LMS_UNAVAILABLE, and
the lecture keeps its kbc_attendance answer (i.e. keeps waiting).

THE RULE (lms_final_status_v1) - business decision of the LMS attendance owner
-------------------------------------------------------------------------------
The LMS consolidates Teams reports, QR scans, staff corrections and parallel-group
credit itself, and this table is its final, authoritative answer:

  attendance_status = 'present'   -> attended (every basis, including parallel)
  attendance_status = 'absent'    -> not attended
  anything else                   -> the lecture is held (a value nobody agreed on)

Known implication, deliberately not compensated for: an officially present learner
may not have been in the analysed Teams meeting (e.g. parallel credit). Such a learner
joins the roster, cannot be heard in the transcript, and so counts as
attended-but-silent in the unchanged engagement formula.

ONE LEARNER, ONE ROW PER LECTURE
--------------------------------
A learner can have several rows for one lecture (one per module, and across the
occurrences of a shared-module lecture). Identity is `learner_profile_id`:

  * all rows agree      -> one roster row (duplicates collapsed, counted);
  * statuses disagree   -> the lecture is held (LMS_CONFLICTING_LEARNER_STATUS).

The existing roster rule then applies unchanged (blank names, bots, name|email
deduplication); no LMS row is marked makeup.

MATCHING
--------
ONLY the lecture's saved, CURRENT, VERIFIED decision of the existing
`meeting_key_start_v2` matcher (public.bunny_lms_match_evaluations /
bunny_lms_occurrence_links, written by the existing LMS matching). No matching is done
here: no saved decision, or a saved decision other than VERIFIED, means no LMS
attendance is read and the lecture keeps its existing state. Rows are used only once
every linked occurrence is `completed` (the LMS's own "report ready" state). Learner names and emails are read into memory for the roster
and never logged or put into any summary.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field

from app.lms import matching


LMS_ATTENDANCE_SOURCE = "curriculum.live_session_learner_attendance"
LMS_ATTENDANCE_RULE_VERSION = "lms_final_status_v1"

# Decisions.
USED = "LMS_ATTENDANCE_USED"
NOT_VERIFIED = "LMS_MATCH_NOT_VERIFIED"
NO_SAVED_MATCH = "LMS_NO_SAVED_MATCH"
OCCURRENCE_NOT_COMPLETED = "LMS_OCCURRENCE_NOT_COMPLETED"
NO_ROWS = "LMS_NO_ATTENDANCE_ROWS"
UNRECOGNISED_ROW = "LMS_UNRECOGNISED_ATTENDANCE_STATUS"
CONFLICTING_LEARNER_STATUS = "LMS_CONFLICTING_LEARNER_STATUS"
UNAVAILABLE = "LMS_UNAVAILABLE"

PRESENT, ABSENT = "present", "absent"
COMPLETED_OCCURRENCE = "completed"

# Row classes.
ATTENDED = "ATTENDED"
NOT_PRESENT = "ABSENT"
UNRECOGNISED = "UNRECOGNISED"

LMS_SYNC_PRESENT_SQL = "SELECT to_regclass('lms_sync.live_session_learner_attendance') IS NOT NULL"

# The matcher's current persisted decision for one lecture (links exist only when VERIFIED).
PERSISTED_MATCH_SQL = """
SELECT e.match_status, k.lms_occurrence_id, k.lms_live_session_id, k.lms_module_catalogue_id,
       k.lms_session_number, k.lms_scheduled_start, k.link_role, k.shared_source_module_id
  FROM public.bunny_lms_match_evaluations e
  LEFT JOIN public.bunny_lms_occurrence_links k
         ON k.evaluation_id = e.evaluation_id AND k.is_current
 WHERE e.lecture_id = %s AND e.is_current AND e.rule_version = %s
 ORDER BY k.link_role DESC NULLS LAST, k.lms_occurrence_id
"""

LMS_SYNC_OCCURRENCE_STATUS_SQL = """
SELECT o.id, o.status FROM lms_sync.live_session_occurrences o WHERE o.id = ANY(%s)
"""

# Every row of the linked occurrences, any module.
LMS_ATTENDANCE_SQL = """
SELECT a.occurrence_id, a.learner_profile_id, a.learner_name, a.learner_email,
       a.module_catalogue_id, a.attendance_status
  FROM lms_sync.live_session_learner_attendance a
 WHERE a.occurrence_id = ANY(%s)
 ORDER BY a.occurrence_id, a.learner_profile_id, a.module_catalogue_id
"""

log = logging.getLogger(__name__)


# -- pure rule ---------------------------------------------------------------------------

def _norm(value) -> str:
    return str(value or "").strip().lower()


def classify_row(row: dict) -> str:
    """The FINAL attendance_status, and nothing else, decides."""
    status = _norm(row.get("attendance_status"))
    if status == PRESENT:
        return ATTENDED
    if status == ABSENT:
        return NOT_PRESENT
    return UNRECOGNISED


def roster_input(row: dict) -> dict:
    """The row shape app.attendance.roster.build_roster reads (same as kbc_attendance)."""
    return {
        "learner_id": row.get("learner_profile_id"),
        "full_name": row.get("learner_name"),
        "email": row.get("learner_email"),
        "attendance_flag": 1,
        "module": row.get("module_catalogue_id"),
        # Never 'makeup': the LMS already decided this learner attended THIS occurrence.
        "attendance_status": "original",
    }


@dataclass(frozen=True)
class LmsAttendanceDecision:
    status: str
    match_status: str | None = None
    occurrence_ids: tuple = ()
    link_roles: tuple = ()
    # Roster input: one row per learner whose final status is present.
    rows: tuple = ()
    source_rows_any_status: int = 0
    source_rows_present: int = 0
    counts: dict = field(default_factory=dict)
    reason: str | None = None

    @property
    def used(self) -> bool:
        return self.status == USED

    def provenance(self) -> dict:
        """Ids, statuses and counts. Never a name, an email or a profile id."""
        return {"source": LMS_ATTENDANCE_SOURCE, "rule_version": LMS_ATTENDANCE_RULE_VERSION,
                "match_rule": matching.RULE_VERSION, "decision": self.status,
                "match_status": self.match_status, "occurrence_ids": list(self.occurrence_ids),
                "link_roles": list(self.link_roles), "reason": self.reason,
                "source_rows_any_status": self.source_rows_any_status,
                "source_rows_present": self.source_rows_present, "counts": dict(self.counts)}


def decide(match: matching.MatchResult, occurrences: dict, rows) -> LmsAttendanceDecision:
    """The whole decision for one lecture, from its match, occurrences and register rows."""
    base = {"match_status": match.status,
            "occurrence_ids": tuple(link.occurrence_id for link in match.links),
            "link_roles": tuple(link.role for link in match.links)}
    if match.status != matching.VERIFIED or not match.links:
        return LmsAttendanceDecision(NOT_VERIFIED, reason=match.evidence.get("reason"), **base)
    states = sorted({str(getattr(occurrences.get(oid), "status", None) or "missing")
                     for oid in base["occurrence_ids"]})
    if states != [COMPLETED_OCCURRENCE]:
        return LmsAttendanceDecision(OCCURRENCE_NOT_COMPLETED, reason=",".join(states), **base)
    rows = list(rows)
    if not rows:
        return LmsAttendanceDecision(NO_ROWS, **base)

    by_learner = collections.defaultdict(list)
    for row in rows:
        row_class = classify_row(row)
        if row_class == UNRECOGNISED:
            return LmsAttendanceDecision(
                UNRECOGNISED_ROW, reason=f"status:{_norm(row.get('attendance_status')) or 'empty'}",
                source_rows_any_status=len(rows), **base)
        by_learner[int(row["learner_profile_id"])].append((row, row_class))

    conflicts = sum(1 for items in by_learner.values() if len({cls for _, cls in items}) > 1)
    if conflicts:
        return LmsAttendanceDecision(
            CONFLICTING_LEARNER_STATUS, reason=f"learners_with_conflicting_status:{conflicts}",
            source_rows_any_status=len(rows),
            counts={"conflicting_learners": conflicts}, **base)

    present = []
    for profile_id in sorted(by_learner):
        items = by_learner[profile_id]
        if items[0][1] != ATTENDED:
            continue
        # The statuses agree, so which row carries the name is immaterial to the
        # decision; pick deterministically, never by query order.
        items.sort(key=lambda item: (str(item[0]["module_catalogue_id"]),
                                     str(item[0]["occurrence_id"])))
        present.append(items[0][0])
    counts = {"learners_attended": len(present),
              "learners_absent": len(by_learner) - len(present),
              "duplicate_rows_collapsed": len(rows) - len(by_learner)}
    return LmsAttendanceDecision(
        USED, rows=tuple(roster_input(row) for row in present),
        source_rows_any_status=len(rows), source_rows_present=len(present),
        counts=counts, **base)


# -- the gateway (the pipeline's own connection) -----------------------------------------

@dataclass(frozen=True)
class _Occurrence:
    status: str


class LmsAttendanceSource:
    """Read-only, on the caller's connection, inside a savepoint."""

    def decide(self, connection, lecture_id) -> LmsAttendanceDecision:
        try:
            with connection.transaction():
                return self._decide(connection, str(lecture_id))
        except Exception as exc:
            # The savepoint is rolled back, so the caller's transaction is intact.
            log.warning("lms attendance read failed", extra={"fields": {
                "service": "lms_attendance", "lecture_id": str(lecture_id),
                "error_type": type(exc).__name__}})
            return LmsAttendanceDecision(UNAVAILABLE, reason=type(exc).__name__)

    def _decide(self, connection, lecture_id) -> LmsAttendanceDecision:
        match = self._persisted_match(connection, lecture_id)
        if match is None:
            # No saved decision: nothing is matched here and nothing is fetched.
            return LmsAttendanceDecision(NO_SAVED_MATCH)
        if match.status != matching.VERIFIED or not match.links:
            return decide(match, {}, ())
        if not connection.execute(LMS_SYNC_PRESENT_SQL).fetchone()[0]:
            return LmsAttendanceDecision(UNAVAILABLE, reason="lms_sync foreign tables absent")
        ids = [link.occurrence_id for link in match.links]
        occurrences = {oid: _Occurrence(status) for oid, status in
                       connection.execute(LMS_SYNC_OCCURRENCE_STATUS_SQL, (ids,)).fetchall()}
        rows = [_register_row(r) for r in connection.execute(LMS_ATTENDANCE_SQL, (ids,)).fetchall()]
        return decide(match, occurrences, rows)

    @staticmethod
    def _persisted_match(connection, lecture_id) -> matching.MatchResult | None:
        found = connection.execute(PERSISTED_MATCH_SQL, (lecture_id, matching.RULE_VERSION)).fetchall()
        if not found:
            return None
        links = tuple(matching.OccurrenceLink(oid, sid, module, number, start, role, shared)
                      for _, oid, sid, module, number, start, role, shared in found if oid)
        return matching.MatchResult(lecture_id=lecture_id, status=found[0][0], links=links,
                                    evidence={"source": "persisted_evaluation"})


def _register_row(row) -> dict:
    occurrence_id, profile_id, name, email, module_id, status = row
    return {"occurrence_id": occurrence_id, "learner_profile_id": profile_id,
            "learner_name": name, "learner_email": email, "module_catalogue_id": module_id,
            "attendance_status": status}
