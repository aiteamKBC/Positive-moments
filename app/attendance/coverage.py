"""
Phase 3C3D: attendance SOURCE COVERAGE, as a fact separate from the count.

THE AMBIGUITY THIS CLOSES
-------------------------
On 2026-09-17 two of three real lectures produced

    attended_count = 0
    spoke_count    = 0
    calculation_status = NO_ATTENDED_LEARNERS

and `public.kbc_attendance` held no rows at all for either of them. The
resolver was right and the arithmetic was right, but the RESULT is ambiguous:
"the source says nobody attended" and "the source says nothing" are different
facts, and only the first one is evidence. Downstream - QA Item 7, Perfect
Lecture eligibility, Operations, Recovery - had no way to tell them apart, so
a lecture could be published as an 11/11 Perfect Lecture on the strength of a
model's opinion about learners the platform had never heard of.

WHAT MAKES THE DISTINCTION DETERMINISTIC
----------------------------------------
`public.lecture_attendance_snapshots` already froze the counts the roster rule
observed, so no new column and no re-query is needed:

    source_row_count        rows the (date, module) query returned
    present_row_count       of those, rows marked present
    effective_member_count  after bot, no-name and duplicate filtering

  source_row_count = 0          no row survived the roster contract. Which of
                                two very different things that means depends
                                on `source_rows_any_status` (below).
  effective_member_count > 0    SOURCE_AVAILABLE_WITH_MEMBERS.
  present_row_count = 0         the source DOES describe this lecture and
                                every row says absent -> the only case that is
                                an authoritative zero.
  otherwise                     rows exist and were all filtered away as bots
                                or nameless -> SOURCE_PARTIAL_OR_INVALID, which
                                is treated as non-authoritative.

Only SOURCE_AVAILABLE_WITH_MEMBERS and SOURCE_AVAILABLE_CONFIRMED_ZERO are
authoritative. Nothing here invents a "confirmed zero": it is claimed only
when the external source itself carries rows for the lecture that say so.

THE `Attendance = 1` BLIND SPOT (found in Phase 3C3E)
-----------------------------------------------------
The roster query is the legacy contract: exact date + exact normalized module
+ `Attendance = 1`. Everything it hands to `build_roster` is therefore already
a present row, so `source_row_count` and `present_row_count` cannot disagree
and the CONFIRMED_ZERO branch was unreachable in production.

G2 Keith on 2026-09-17 is the case that exposed it: `kbc_attendance` holds one
row for that module, marked absent. Under the roster contract alone that is
indistinguishable from the table never having heard of the lecture - but they
are not the same fact, and Operations needs to see the difference.

So `source_rows_any_status` - the count for the same (date, module) with NO
attendance filter - is captured alongside, and:

    source_row_count = 0, any_status = 0   -> SOURCE_MISSING
                                              the source is silent.
    source_row_count = 0, any_status > 0   -> SOURCE_PARTIAL_OR_INVALID,
                                              ALL_SOURCE_ROWS_MARKED_ABSENT.
                                              The source spoke, but only in
                                              absences.

The second is deliberately NOT a confirmed zero. "Every row we have says
absent" only becomes "nobody attended" if you also know those rows cover
everyone who could have attended, and the only way to know that is to
cross-reference enrolment - which is inference, not evidence. Both states stay
non-authoritative, so nothing downstream changes behaviour; what changes is
that Operations can tell silence from a partial answer.
"""

ATTENDANCE_COVERAGE_VERSION = "attendance_coverage_v1"

SOURCE_AVAILABLE_WITH_MEMBERS = "SOURCE_AVAILABLE_WITH_MEMBERS"
SOURCE_AVAILABLE_CONFIRMED_ZERO = "SOURCE_AVAILABLE_CONFIRMED_ZERO"
SOURCE_MISSING = "SOURCE_MISSING"
SOURCE_PARTIAL_OR_INVALID = "SOURCE_PARTIAL_OR_INVALID"
# No snapshot row exists at all, so not even the counts are known.
SOURCE_UNKNOWN = "SOURCE_UNKNOWN"

COVERAGE_STATUSES = (SOURCE_AVAILABLE_WITH_MEMBERS, SOURCE_AVAILABLE_CONFIRMED_ZERO,
                     SOURCE_MISSING, SOURCE_PARTIAL_OR_INVALID, SOURCE_UNKNOWN)

# The statuses on which a downstream decision may rely.
AUTHORITATIVE_STATUSES = frozenset(
    {SOURCE_AVAILABLE_WITH_MEMBERS, SOURCE_AVAILABLE_CONFIRMED_ZERO})

# The engagement detail reported when 0/0 is a data gap rather than a finding.
ATTENDANCE_SOURCE_MISSING = "ATTENDANCE_SOURCE_MISSING"

# Why a coverage status is PARTIAL. The two causes are not the same problem.
ALL_SOURCE_ROWS_MARKED_ABSENT = "ALL_SOURCE_ROWS_MARKED_ABSENT"
ALL_PRESENT_ROWS_FILTERED = "ALL_PRESENT_ROWS_FILTERED"


def classify(*, source_row_count, present_row_count, effective_member_count,
             source_rows_any_status=None) -> str:
    """
    Derive the coverage status from one snapshot's frozen counts.

    `source_rows_any_status` is optional because snapshots frozen before Phase
    3C3E do not carry it. Without it an empty roster stays SOURCE_MISSING,
    which is the safe reading: it can under-claim what the source said, never
    over-claim it.
    """
    if source_row_count is None:
        return SOURCE_UNKNOWN
    if int(source_row_count) <= 0:
        if source_rows_any_status is not None and int(source_rows_any_status) > 0:
            # The source DOES describe this lecture - entirely in absences.
            # Not silence, and not a confirmed zero either.
            return SOURCE_PARTIAL_OR_INVALID
        return SOURCE_MISSING
    if int(effective_member_count or 0) > 0:
        return SOURCE_AVAILABLE_WITH_MEMBERS
    if int(present_row_count or 0) <= 0:
        # The source described this lecture and recorded nobody as present.
        return SOURCE_AVAILABLE_CONFIRMED_ZERO
    # Rows were present but none survived bot / no-name / dedup filtering.
    return SOURCE_PARTIAL_OR_INVALID


def partial_reason(*, source_row_count, present_row_count,
                   source_rows_any_status=None):
    """Why coverage is PARTIAL. The two causes need different fixes."""
    if int(source_row_count or 0) <= 0:
        if source_rows_any_status is not None and int(source_rows_any_status) > 0:
            return ALL_SOURCE_ROWS_MARKED_ABSENT
        return None
    if int(present_row_count or 0) > 0:
        return ALL_PRESENT_ROWS_FILTERED
    return None


def is_authoritative(status) -> bool:
    """True only when the external source actually answered the question."""
    return status in AUTHORITATIVE_STATUSES


def confirms_zero_attendance(status, attended_count) -> bool:
    """
    Whether "nobody attended" is a SUPPORTED conclusion.

    A zero from a missing source is a placeholder, never a finding. This is
    the predicate downstream code must use instead of `attended_count == 0`.
    """
    return is_authoritative(status) and int(attended_count or 0) == 0


def provenance(*, status, source_row_count=None, present_row_count=None,
               effective_member_count=None, snapshot_id=None,
               source_rows_any_status=None) -> dict:
    """Printable coverage provenance. Counts and ids only, never a name."""
    return {
        "attendance_coverage_status": status,
        "attendance_coverage_version": ATTENDANCE_COVERAGE_VERSION,
        "attendance_source_authoritative": is_authoritative(status),
        "attendance_source_row_count": source_row_count,
        "attendance_present_row_count": present_row_count,
        "attendance_effective_member_count": effective_member_count,
        # Rows for this (date, module) with NO attendance filter. Distinguishes
        # "the source is silent" from "the source recorded only absences".
        "attendance_source_rows_any_status": source_rows_any_status,
        "attendance_coverage_partial_reason": partial_reason(
            source_row_count=source_row_count, present_row_count=present_row_count,
            source_rows_any_status=source_rows_any_status),
        "attendance_snapshot_id": str(snapshot_id) if snapshot_id is not None else None,
    }
