"""
Rule meeting_key_start_v2: lecture -> LMS occurrence(s). Pure and deterministic.

    1. An LI-suppressed duplicate lecture is LI_DUPLICATE (never a gap).
    2. No parsable Teams meeting on the lecture -> NO_MEETING_KEY.
    3. Candidates = LMS occurrences sharing a meeting key with the lecture.
       ACTIVE candidates: occurrence not cancelled, series 'active' or
       'week-meeting' (so superseded / cancelled / feedback-test series are
       out), module not deleted when the series has one.
    4. Active candidates at the EXACT UTC start:
         - a contradicting online-meeting id                  -> CONFLICT
         - none: same key within +-7 days                     -> RESCHEDULED_CANDIDATE
                 only inactive rows at that start, all tests  -> TEST_EXCLUDED
                 only inactive rows at that start             -> LMS_RECORD_INACTIVE
                 otherwise                                    -> NO_LMS_SESSION
         - all in configured test programmes                  -> TEST_EXCLUDED
         - exactly one (test ones dropped)                    -> VERIFIED
         - several, and module_teams_shares links every other
           candidate's module to exactly one candidate's
           (unshared) module                                  -> VERIFIED, PRIMARY +
                                                                 SHARED_MODULE links
         - several otherwise                                  -> AMBIGUOUS
Titles, modules, trainers and same-time occurrences in other meetings are
never evidence of identity. The first candidate is never taken.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta

RULE_VERSION = "meeting_key_start_v2"

VERIFIED = "VERIFIED"
AMBIGUOUS = "AMBIGUOUS"
RESCHEDULED_CANDIDATE = "RESCHEDULED_CANDIDATE"
NO_LMS_SESSION = "NO_LMS_SESSION"
LMS_RECORD_INACTIVE = "LMS_RECORD_INACTIVE"
CONFLICT = "CONFLICT"
STALE = "STALE"
NO_MEETING_KEY = "NO_MEETING_KEY"
TEST_EXCLUDED = "TEST_EXCLUDED"
LI_DUPLICATE = "LI_DUPLICATE"
OUT_OF_LMS_SCOPE = "OUT_OF_LMS_SCOPE"

REVIEW_STATUSES = frozenset({AMBIGUOUS, RESCHEDULED_CANDIDATE, NO_LMS_SESSION, LMS_RECORD_INACTIVE,
                             CONFLICT, STALE, NO_MEETING_KEY})
ACTIVE_SERIES_STATUSES = frozenset({"active", "week-meeting"})

PRIMARY = "PRIMARY"
SHARED_MODULE = "SHARED_MODULE"


@dataclass(frozen=True)
class MatchConfig:
    # Exact programme names (curriculum.modules.programme_name). Explicit,
    # configured, reviewed with the LMS team - never a "contains 'test'" guess.
    test_programmes: frozenset = frozenset()
    reschedule_window: timedelta = timedelta(days=7)
    active_series_statuses: frozenset = ACTIVE_SERIES_STATUSES

    @classmethod
    def from_text(cls, value: str | None, **kwargs) -> "MatchConfig":
        """'Final Test;Test Lms;Test' (';' or newline separated; ',' is allowed in names)."""
        names = [part.strip() for part in str(value or "").replace("\n", ";").split(";")]
        return cls(test_programmes=frozenset(n for n in names if n), **kwargs)


@dataclass(frozen=True)
class OccurrenceLink:
    occurrence_id: str
    live_session_id: str
    module_catalogue_id: str | None
    session_number: int | None
    scheduled_start: object
    role: str
    shared_source_module_id: str | None = None


@dataclass(frozen=True)
class MatchResult:
    lecture_id: str
    status: str
    links: tuple = ()
    candidate_ids: tuple = ()
    meeting_key_hash: str | None = None
    evidence: dict = field(default_factory=dict)
    rule_version: str = RULE_VERSION

    @property
    def requires_review(self) -> bool:
        return self.status in REVIEW_STATUSES

    @property
    def primary(self) -> OccurrenceLink | None:
        return next((link for link in self.links if link.role == PRIMARY), None)

    @property
    def decision(self) -> dict:
        """What was decided - not when, not how often. Same decision, same sha."""
        decision = {"rule": self.rule_version, "status": self.status,
                    "links": sorted([l.occurrence_id, l.role, l.shared_source_module_id] for l in self.links),
                    "candidates": sorted(self.candidate_ids)}
        if self.status == STALE:
            decision["stale_from"] = self.evidence.get("stale_from_evaluation_id")
            decision["observed"] = self.evidence.get("observed_decision_sha256")
        return decision

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.decision, sort_keys=True, default=str).encode()).hexdigest()


def is_active(occurrence, config: MatchConfig) -> bool:
    return (occurrence.status != "cancelled"
            and occurrence.series_status in config.active_series_statuses
            and not occurrence.module_deleted)


def is_test(occurrence, config: MatchConfig) -> bool:
    return (occurrence.programme_name or "") in config.test_programmes


def _link(occurrence, role, source=None) -> OccurrenceLink:
    return OccurrenceLink(occurrence.occurrence_id, occurrence.live_session_id, occurrence.module_catalogue_id,
                          occurrence.session_number, occurrence.scheduled_start, role, source)


def _resolve_shared(candidates, shares: dict):
    """
    Several active occurrences of one meeting at one start are ONE lecture only
    when the LMS says so: every candidate but one sits in a module whose
    module_teams_shares source is the remaining candidate's module, and that
    root module is not itself shared from elsewhere. Anything else is ambiguous.
    """
    modules = [c.module_catalogue_id for c in candidates]
    if any(m is None for m in modules) or len(set(modules)) != len(modules):
        return None                      # moduleless, or two occurrences in one module
    roots = [c for c in candidates if c.module_catalogue_id not in shares]
    if len(roots) != 1:
        return None
    root = roots[0]
    others = [c for c in candidates if c is not root]
    if not all(shares.get(c.module_catalogue_id) == root.module_catalogue_id for c in others):
        return None
    return root, others


def match_lecture(lecture, snapshot, config: MatchConfig) -> MatchResult:
    base = {"lecture_id": lecture.lecture_id, "meeting_key_hash": lecture.meeting_key_hash}
    if lecture.duplicate_suppressed:
        return MatchResult(status=LI_DUPLICATE, evidence={"reason": "LI duplicate_suppression"}, **base)
    if not lecture.meeting_keys:
        return MatchResult(status=NO_MEETING_KEY, evidence={"reason": "no Teams meeting key"}, **base)

    keyed = [o for o in snapshot.occurrences if o.meeting_keys & lecture.meeting_keys]
    active = [o for o in keyed if is_active(o, config)]
    at_start = [o for o in active if o.scheduled_start == lecture.scheduled_start]
    evidence = {
        "keyed": len(keyed),
        "excluded_cancelled": sum(1 for o in keyed if o.status == "cancelled"),
        "excluded_inactive_series": sum(1 for o in keyed if o.series_status not in config.active_series_statuses),
        "excluded_deleted_module": sum(1 for o in keyed if o.module_deleted),
    }

    if lecture.online_meeting_key:
        contradicting = [o.occurrence_id for o in at_start
                         if o.online_meeting_keys and lecture.online_meeting_key not in o.online_meeting_keys]
        if contradicting:
            return MatchResult(status=CONFLICT, candidate_ids=tuple(sorted(contradicting)),
                               evidence={**evidence, "reason": "online meeting id contradicts the join URL key"},
                               **base)

    if not at_start:
        window = config.reschedule_window.total_seconds()
        moved = sorted(o.occurrence_id for o in active
                       if 0 < abs((o.scheduled_start - lecture.scheduled_start).total_seconds()) <= window)
        if moved:
            return MatchResult(status=RESCHEDULED_CANDIDATE, candidate_ids=tuple(moved),
                               evidence={**evidence, "reason": "same meeting, different start"}, **base)
        inactive_rows = [o for o in keyed
                         if not is_active(o, config) and o.scheduled_start == lecture.scheduled_start]
        inactive = sorted(o.occurrence_id for o in inactive_rows)
        if inactive and all(is_test(o, config) for o in inactive_rows):
            return MatchResult(status=TEST_EXCLUDED, candidate_ids=tuple(inactive),
                               evidence={**evidence, "reason": "configured test programme (inactive LMS rows)"},
                               **base)
        if inactive:
            return MatchResult(status=LMS_RECORD_INACTIVE, candidate_ids=tuple(inactive),
                               evidence={**evidence, "reason": "only cancelled/inactive LMS rows at this start"},
                               **base)
        return MatchResult(status=NO_LMS_SESSION, evidence={**evidence, "reason": "meeting not in the LMS"}, **base)

    tests = sorted(o.occurrence_id for o in at_start if is_test(o, config))
    production = [o for o in at_start if not is_test(o, config)]
    evidence["test_candidates"] = tests
    if not production:
        return MatchResult(status=TEST_EXCLUDED, candidate_ids=tuple(tests),
                           evidence={**evidence, "reason": "configured test programme"}, **base)
    if len(production) == 1:
        return MatchResult(status=VERIFIED, links=(_link(production[0], PRIMARY),),
                           candidate_ids=(production[0].occurrence_id,), evidence=evidence, **base)

    ids = tuple(sorted(o.occurrence_id for o in production))
    shared = _resolve_shared(production, snapshot.shares)
    if shared is None:
        return MatchResult(status=AMBIGUOUS, candidate_ids=ids,
                           evidence={**evidence, "reason": "several active occurrences, no explicit module share"},
                           **base)
    root, others = shared
    links = (_link(root, PRIMARY),) + tuple(
        _link(o, SHARED_MODULE, root.module_catalogue_id) for o in sorted(others, key=lambda o: o.occurrence_id))
    return MatchResult(status=VERIFIED, links=links, candidate_ids=ids,
                       evidence={**evidence, "shared_from_module": root.module_catalogue_id}, **base)
