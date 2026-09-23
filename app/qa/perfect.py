"""
Phase 3C2.3D: Perfect Lecture eligibility.

The answer is derived from the FROZEN, persisted QA result - the Phase 3B
rendered session and its rendered checklist rows. Nothing here calls a model,
re-reads a transcript, recalculates a checklist status or consults
public.qa_perfect_lectures. Given the same rendered payload this function
returns the same answer forever, which is what makes a missing legacy row
repairable without spending anything.

The authority for the rule is the executable legacy branch
`Prepare Lecture Recording` in QA_One_Lecture_Safe_Exact_Recording_v8:

    const uniqueOrders = new Set(
      rows.map(row => Number(row.checklist_order)).filter(Number.isFinite));
    const allMet = rows.every(row => String(row.status || '').trim() === 'Met');
    const metCount = Number(first.met_count ?? 0);
    const isPerfect =
      metCount === 11 &&
      uniqueOrders.size === 11 &&
      allMet &&
      first.cancelled_session !== true;

and the key it writes:

    lecture_key: `${sessionDate}|${subject}`

where `sessionDate` is `String(first.date || lecture.targetDate).slice(0, 10)`.
"""
from app.attendance.coverage import is_authoritative
from app.qa.checklist import CHECKLIST_ITEM_COUNT, MET


# The rule identity. The legacy table still holds 79 rows written when the
# checklist had TWELVE items; those rows were correct under the rule of their
# day. Versioning the answer is how that stays true instead of being
# retroactively reinterpreted as a defect.
PERFECT_ELIGIBILITY_VERSION = "legacy_qa_v8_perfect_v1"

# Phase 3C3D. The coded platform's own policy for UNATTENDED operation. It is
# strictly narrower than v1 and it is a SEPARATE version, never a redefinition:
# every row already written under v1 stays reproducible under v1 forever.
#
# The added condition is one sentence: a lecture may not be published as a
# Perfect Lecture while the platform has no attendance evidence for it.
#
# Why that matters concretely. Item 7 has a deterministic override from
# attendance, but the override cannot apply when there is no attendance, so the
# status falls back to the MODEL's opinion - and legacy does exactly the same.
# On 2026-09-17 that produced an 11/11 Perfect Lecture for a lecture whose
# learners the platform has never heard of. QA completing on an AI fallback is
# acceptable and unchanged; EXTERNALLY PUBLISHING that as a Perfect Lecture,
# unattended, to a shared Excel workbook, is not.
#
# The blocked state is explicitly NON-FINAL. It is not "not perfect": it is
# "cannot be decided yet", and it clears by itself the moment attendance
# coverage arrives, with no model call.
PERFECT_ELIGIBILITY_VERSION_V2 = "kbc_perfect_v2_attendance_required"

PERFECT_ELIGIBILITY_VERSIONS = (PERFECT_ELIGIBILITY_VERSION,
                                PERFECT_ELIGIBILITY_VERSION_V2)

# Phase 3C3E. v2 is now the default for ALL NEW coded processing. v1 is not
# removed and not rewritten: it stays selectable by name so every row already
# written under it remains reproducible, which is the whole reason the policy
# was versioned rather than edited.
DEFAULT_PERFECT_ELIGIBILITY_VERSION = PERFECT_ELIGIBILITY_VERSION_V2

# F-01. The policies allowed to PUBLISH a Perfect Lecture row. An allowlist, not
# a blocklist: v1 has no attendance condition and published an 11/11 lecture
# with zero attendees on 2026-09-18, so a policy may reach a legacy table only
# once somebody has decided it should. v1 remains fully usable for dry-run
# historical reproduction - it is only barred from write-enabled modes.
PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS = frozenset({PERFECT_ELIGIBILITY_VERSION_V2})


def may_publish(eligibility_version) -> bool:
    """Whether a Perfect policy may be used in a write-enabled mode."""
    return eligibility_version in PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS

# Non-final. Never written to the legacy table, never an ownership record.
PENDING_ATTENDANCE_DATA = "PENDING_ATTENDANCE_DATA"

# Reasons. ELIGIBLE is the only one that yields is_perfect = True.
ELIGIBLE = "ELIGIBLE"
NOT_ELIGIBLE_CANCELLED = "NOT_ELIGIBLE_CANCELLED"
NOT_ELIGIBLE_CHECKLIST_ROW_COUNT = "NOT_ELIGIBLE_CHECKLIST_ROW_COUNT"
NOT_ELIGIBLE_DUPLICATE_ORDER = "NOT_ELIGIBLE_DUPLICATE_ORDER"
NOT_ELIGIBLE_ORDERS_NOT_1_TO_11 = "NOT_ELIGIBLE_ORDERS_NOT_1_TO_11"
NOT_ELIGIBLE_STATUS_NOT_ALL_MET = "NOT_ELIGIBLE_STATUS_NOT_ALL_MET"
NOT_ELIGIBLE_MET_COUNT = "NOT_ELIGIBLE_MET_COUNT"
NOT_ELIGIBLE_PARTIAL_COUNT = "NOT_ELIGIBLE_PARTIAL_COUNT"
NOT_ELIGIBLE_NOT_MET_COUNT = "NOT_ELIGIBLE_NOT_MET_COUNT"
NOT_ELIGIBLE_MISSING_IDENTIFIERS = "NOT_ELIGIBLE_MISSING_IDENTIFIERS"

EXPECTED_ORDERS = frozenset(range(1, CHECKLIST_ITEM_COUNT + 1))


class PerfectLectureInputError(RuntimeError):
    """The rendered payload cannot be judged at all."""


def is_cancelled(value) -> bool:
    """
    Read `cancelled_session` the way both sides store it.

    The coded rendered column is a real boolean; the legacy column is TEXT
    holding the JavaScript literals n8n wrote ('true' / 'false'). Legacy tests
    `!== true`, so anything that is not an affirmative cancellation counts as
    not cancelled - including NULL.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def legacy_lecture_key(session_date, subject) -> str:
    """
    Reproduce the legacy compatibility key exactly: `YYYY-MM-DD|subject`.

    This is NOT an identity and must never become one. The production table
    already contains one date+subject pair that maps to two distinct legacy
    sessions, so the key can collide by construction. lecture_id stays
    canonical; this string exists only to address the legacy row.
    """
    if session_date is None:
        raise PerfectLectureInputError("a legacy lecture_key needs a session date")
    # Legacy does String(first.date).slice(0, 10) on a date already rendered
    # as an ISO string, so the first ten characters are the date.
    text = session_date if isinstance(session_date, str) else session_date.isoformat()
    date_part = text[:10]
    # Legacy applies String(...).trim() to the subject before interpolating.
    return f"{date_part}|{str(subject or '').strip()}"


def evaluate(rendered: dict, rendered_items) -> dict:
    """
    Decide whether ONE finalized QA result is a Perfect Lecture.

    Returns the answer plus the facts it was derived from, so the decision can
    be audited later without recomputation.

    Two deliberate differences from the legacy expression, both strictly
    narrower - this can refuse where legacy would accept, never the reverse:

    1. legacy checks `uniqueOrders.size === 11`, which a twelve-row payload
       with one duplicated order would also satisfy. This requires exactly
       eleven rows whose orders are exactly 1..11.
    2. legacy checks only `met_count`; this also requires partial_count and
       not_met_count to be zero. Under an eleven-row all-Met checklist they
       are zero anyway, so no real payload is classified differently - the
       checks exist so a self-inconsistent payload is refused rather than
       written.
    """
    rows = list(rendered_items or [])
    orders = [row["checklist_order"] for row in rows]
    statuses = [str(row.get("status") or "").strip() for row in rows]

    met_count = _as_int(rendered.get("met_count"))
    partial_count = _as_int(rendered.get("partial_count"))
    not_met_count = _as_int(rendered.get("not_met_count"))
    cancelled = is_cancelled(rendered.get("cancelled_session"))

    facts = {
        "checklist_row_count": len(rows),
        "distinct_order_count": len({order for order in orders if order is not None}),
        "orders": sorted(order for order in orders if order is not None),
        "met_status_count": sum(1 for status in statuses if status == MET),
        "met_count": met_count,
        "partial_count": partial_count,
        "not_met_count": not_met_count,
        "cancelled_session": cancelled,
        "eligibility_version": PERFECT_ELIGIBILITY_VERSION,
    }

    reason = _reason(rendered, facts, rows)
    facts["is_perfect"] = reason == ELIGIBLE
    facts["reason"] = reason
    return facts


def _reason(rendered, facts, rows) -> str:
    # Legacy throws outright without these; refusing is the coded equivalent.
    if not rendered.get("session_id") or not rendered.get("meeting_id"):
        return NOT_ELIGIBLE_MISSING_IDENTIFIERS
    # Ordered so the reported reason is the most specific structural fault.
    if facts["checklist_row_count"] != CHECKLIST_ITEM_COUNT:
        return NOT_ELIGIBLE_CHECKLIST_ROW_COUNT
    if facts["distinct_order_count"] != facts["checklist_row_count"]:
        return NOT_ELIGIBLE_DUPLICATE_ORDER
    if set(facts["orders"]) != EXPECTED_ORDERS:
        return NOT_ELIGIBLE_ORDERS_NOT_1_TO_11
    if facts["met_status_count"] != CHECKLIST_ITEM_COUNT:
        return NOT_ELIGIBLE_STATUS_NOT_ALL_MET
    if facts["met_count"] != CHECKLIST_ITEM_COUNT:
        return NOT_ELIGIBLE_MET_COUNT
    if facts["partial_count"] != 0:
        return NOT_ELIGIBLE_PARTIAL_COUNT
    if facts["not_met_count"] != 0:
        return NOT_ELIGIBLE_NOT_MET_COUNT
    if facts["cancelled_session"]:
        return NOT_ELIGIBLE_CANCELLED
    return ELIGIBLE


def attendance_policy_required(eligibility_version: str) -> bool:
    """Whether this eligibility version requires attendance source evidence."""
    return eligibility_version == PERFECT_ELIGIBILITY_VERSION_V2


def apply_attendance_policy(facts: dict, *, coverage_status,
                            eligibility_version: str) -> dict:
    """
    Layer the attendance-source condition on top of a v1 eligibility answer.

    Returns a NEW facts dict; the v1 answer is preserved alongside it as
    `base_reason` / `base_is_perfect` so the two policies can always be
    compared without recomputation.

    Only one transition exists, and only in one direction:

        ELIGIBLE + non-authoritative coverage  ->  PENDING_ATTENDANCE_DATA

    A lecture that is not perfect anyway keeps its ordinary NOT_ELIGIBLE
    reason: a missing attendance source is not an extra way to fail, it is a
    reason a PASS cannot yet be acted on. And a lecture whose source
    authoritatively records zero attendance is NOT pending - the source
    answered, so the ordinary rule applies.
    """
    result = {**facts,
              "eligibility_version": eligibility_version,
              "base_is_perfect": facts["is_perfect"],
              "base_reason": facts["reason"],
              "attendance_coverage_status": coverage_status,
              "attendance_source_authoritative": is_authoritative(coverage_status),
              "attendance_pending": False}
    if not attendance_policy_required(eligibility_version):
        return result
    if facts["reason"] == ELIGIBLE and not is_authoritative(coverage_status):
        result.update({"is_perfect": False, "reason": PENDING_ATTENDANCE_DATA,
                       "attendance_pending": True})
    return result


def legacy_is_perfect(rows, met_count, cancelled_session) -> bool:
    """
    The legacy expression, transcribed literally, for parity testing only.

    Nothing in the platform decides anything with this: it exists so a test can
    assert that `evaluate` agrees with the executable legacy branch on every
    payload the legacy branch could actually have produced.
    """
    unique_orders = {row["checklist_order"] for row in rows
                     if isinstance(row.get("checklist_order"), int)}
    all_met = all(str(row.get("status") or "").strip() == MET for row in rows)
    return (_as_int(met_count) == CHECKLIST_ITEM_COUNT
            and len(unique_orders) == CHECKLIST_ITEM_COUNT
            and all_met
            and not is_cancelled(cancelled_session))


def _as_int(value) -> int:
    """Legacy uses Number(x ?? 0); a non-numeric becomes a refusal, not a 0."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
