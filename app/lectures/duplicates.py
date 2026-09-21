"""
Phase 4C1: deterministic duplicate calendar event resolution.

THE PROBLEM, IN ONE SENTENCE
----------------------------
The same lecture is sometimes invited twice - two distinct calendar events,
two distinct iCalUIDs, one real Teams meeting - and only one of the two events
can be the lecture.

Discovery is right to keep both. Migration 003 is explicit that occurrences are
"never merged or deleted by subject+start", and that rule is load-bearing: two
events at the same time with the same title are sometimes two genuinely
different lectures, and a platform that merged them would silently destroy one.
So both rows exist, one carries the meeting, and the other used to sit in
MANUAL_REVIEW_REQUIRED for ever.

This module decides, deterministically, when that second row can be retired
without a human - and, just as importantly, when it cannot.

WHY THIS IS SAFE TO AUTOMATE, AND ONLY HERE
-------------------------------------------
The rule automates exactly one shape:

    one sibling holds a confidently resolved Teams meeting,
    every other sibling holds no meeting at all.

That shape has no ambiguity left in it. There is nothing to choose between,
because only one candidate exists. Every shape that DOES require a choice -
two valid meetings, two events sharing one meeting, an ambiguous lookup that
might yet resolve, or nothing resolved at all - is refused and handed to an
operator, because choosing between two real meetings is a judgement about which
class actually happened and no deterministic rule can make it.

NOTHING HERE GUESSES
--------------------
No fuzzy title matching, no nearest-start-time, no "the one with the longer
subject", no model. Group membership is exact equality on the identity a
lecture occurrence actually has, and the winner is the only member that already
resolved through the validated Phase 1 meeting authority. If the deterministic
criteria are not met, the answer is "ask a human", not "pick one".

NOTHING HERE DELETES
--------------------
A suppressed event keeps its row, its lecture_id, its calendar event id, its
iCalUID and its discovery history. Suppression is an annotation that says
"this occurrence is a duplicate of that one", and it points at the winner so
the decision can be audited or reversed.
"""
from datetime import datetime, timezone


DUPLICATE_RESOLUTION_VERSION = "duplicate_event_resolution_v1"

# The metadata key the suppression annotation lives under. One key, so a later
# discovery upsert can preserve it explicitly rather than by accident.
SUPPRESSION_KEY = "duplicate_suppression"


# --- outcomes ---------------------------------------------------------------

# Case A: exactly one confidently resolved sibling, every other unresolved.
CASE_SUPPRESSED = "DUPLICATE_EVENT_SUPPRESSED"
# Case B: more than one sibling holds a valid, DIFFERENT meeting.
CASE_MULTIPLE_VALID_MEETINGS = "DUPLICATE_EVENTS_MULTIPLE_VALID_MEETINGS"
# Case B, variant: two events resolved to the SAME meeting. Not two lectures,
# but still a choice about which event owns it, so still a human's call.
CASE_SHARED_MEETING = "DUPLICATE_EVENTS_SHARED_MEETING_ID"
# Case C: nothing in the group resolved. Suppressing one arbitrarily would be
# a guess, and the group may simply be waiting for a meeting to appear.
CASE_NONE_RESOLVED = "DUPLICATE_EVENTS_NONE_RESOLVED"
# One resolved sibling, but an unresolved sibling whose state might still
# become a meeting (an ambiguous lookup). Refused as a group: partial
# suppression inside a group nobody understands is worse than none.
CASE_UNRESOLVED_NOT_SUPPRESSIBLE = "DUPLICATE_EVENTS_SIBLING_NOT_SUPPRESSIBLE"
# Not a duplicate group at all.
CASE_NOT_DUPLICATE = "NOT_A_DUPLICATE_GROUP"

# The reason recorded on a suppressed row, and reported by the state resolver.
SUPPRESSED_REASON = CASE_SUPPRESSED
# The reason reported while a suppressible duplicate has not been suppressed
# yet. It is an action waiting to happen, not a problem.
PENDING_REASON = "DUPLICATE_EVENT_PENDING_SUPPRESSION"

# Cases that stay with a human. Everything that is not Case A.
MANUAL_REVIEW_CASES = frozenset({
    CASE_MULTIPLE_VALID_MEETINGS, CASE_SHARED_MEETING, CASE_NONE_RESOLVED,
    CASE_UNRESOLVED_NOT_SUPPRESSIBLE,
})


# --- what counts as resolved, and what counts as unresolved -----------------

# `RESOLVED` is the current spelling and `EXACT_JOIN_URL_MATCH` the
# pre-migration-002 one for the same fact; both are kept everywhere else in
# the platform and are kept here for the same reason.
RESOLVED_MAPPING_STATUSES = frozenset({"RESOLVED", "EXACT_JOIN_URL_MATCH"})

# A winner must have passed the validated organizer check, not merely have a
# meeting id. This is deliberately stricter than "downstream_ready": a lecture
# is about to be declared a duplicate of this one, and the cost of being too
# strict is a manual review that already happens today, while the cost of
# being too loose is retiring a real lecture.
CONFIRMED_ORGANIZER_STATUSES = frozenset({"ORGANIZER_ID_CONFIRMED"})

# Unresolved states that are final for this occurrence: Graph was asked, or
# deliberately not asked, and no meeting exists to be had.
SUPPRESSIBLE_MAPPING_STATUSES = frozenset({
    "ONLINE_MEETING_NOT_FOUND", "NOT_ATTEMPTED", "ORGANIZER_NOT_RESOLVABLE",
    "FORBIDDEN_FOR_ORGANIZER",
})

# Deliberately NOT suppressible. `AMBIGUOUS_ONLINE_MEETING` means Graph found
# SEVERAL candidate meetings and we declined to choose - that is evidence a
# real meeting exists, which is the opposite of the emptiness the rule needs.
AMBIGUOUS_MAPPING_STATUSES = frozenset({"AMBIGUOUS_ONLINE_MEETING"})


def is_confidently_resolved(row) -> bool:
    """Does this occurrence hold a meeting the platform already trusts?"""
    return bool(
        row.get("meeting_id")
        and row.get("calendar_mapping_status") in RESOLVED_MAPPING_STATUSES
        and row.get("organizer_validation_status") in CONFIRMED_ORGANIZER_STATUSES)


def is_suppressible(row) -> bool:
    """Is this occurrence empty in the final, unambiguous sense?"""
    return bool(
        not row.get("meeting_id")
        and row.get("calendar_mapping_status") in SUPPRESSIBLE_MAPPING_STATUSES)


# --- grouping ---------------------------------------------------------------

def group_key(row) -> tuple:
    """
    The identity two calendar events must share EXACTLY to be candidates.

    Every component is an exact equality on something the occurrence actually
    is, never a similarity score:

      * `session_date` and `normalized_subject` - the same business day and the
        same title under the platform's own normalization (`normalize_group`),
        which is the comparison discovery already used to match the Aptem
        group in the first place;
      * `module` - the matched ACTIVE Aptem group. Two events with the same
        title that matched different groups are different lectures;
      * `scheduled_start` AND `scheduled_end` - the same occurrence, to the
        second. "Compatible duration" is implemented as equality because a
        duplicate booking of the same class is normally an exact copy, and a
        tolerance window is a fuzzy rule wearing a precise costume.
    """
    return (row["session_date"], row["normalized_subject"], row["module"],
            row["scheduled_start"], row["scheduled_end"])


def group_rows(rows) -> dict:
    """Group occurrences by exact identity. Cancelled occurrences are ignored."""
    groups: dict = {}
    for row in rows:
        if row.get("is_cancelled"):
            continue
        groups.setdefault(group_key(row), []).append(row)
    return groups


# --- the decision -----------------------------------------------------------

def classify_group(rows, *, now=None) -> dict:
    """
    Decide one candidate group. Pure: no I/O, no clock unless one is given.

    Returns the case, the winner if there is one, and the suppressions to
    persist - which is an empty list for every case but A.
    """
    live = [row for row in rows if not row.get("is_cancelled")]
    if len(live) < 2:
        return {"case": CASE_NOT_DUPLICATE, "winner": None, "suppress": [],
                "group_size": len(live), "requires_manual_review": False,
                "members": [_member(row) for row in live],
                "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION}

    winners = [row for row in live if is_confidently_resolved(row)]
    others = [row for row in live if not is_confidently_resolved(row)]

    def result(case, *, winner=None, suppress=()):
        return {"case": case, "winner": winner, "suppress": list(suppress),
                "group_size": len(live),
                "requires_manual_review": case in MANUAL_REVIEW_CASES,
                "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
                "group_key": _key_detail(live[0]),
                "members": [_member(row) for row in live]}

    if len(winners) > 1:
        # Two real meetings, or two events holding one real meeting. Either
        # way a person decides which event is the lecture; the platform has no
        # deterministic basis to prefer one, and subject/title preference is
        # explicitly not a basis.
        distinct = {row["meeting_id"] for row in winners}
        return result(CASE_SHARED_MEETING if len(distinct) == 1
                      else CASE_MULTIPLE_VALID_MEETINGS)
    if not winners:
        return result(CASE_NONE_RESOLVED)

    unsuppressible = [row for row in others if not is_suppressible(row)]
    if unsuppressible:
        # One resolved sibling, but a sibling that might still become a
        # meeting. The group is refused whole: suppressing the clearly-empty
        # members while an ambiguous one remains would produce a half-resolved
        # group nobody can reason about.
        return result(CASE_UNRESOLVED_NOT_SUPPRESSIBLE)

    winner = winners[0]
    resolved_at = (now or datetime.now(timezone.utc)).isoformat()
    return result(CASE_SUPPRESSED, winner=winner, suppress=[
        _annotation(winner, loser, resolved_at) for loser in others])


def _annotation(winner, loser, resolved_at) -> dict:
    """
    The provenance a suppressed row carries.

    Enough, on its own, to answer "why is this not a lecture?" and to reverse
    the decision: both identities, the rule version that made it, and when.
    """
    return {
        "lecture_id": loser["lecture_id"],
        "annotation": {
            "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
            "duplicate_resolution_reason": SUPPRESSED_REASON,
            "duplicate_resolution_case": CASE_SUPPRESSED,
            "winner_lecture_id": winner["lecture_id"],
            "winner_calendar_event_id": winner["calendar_event_id"],
            "winner_i_cal_uid": winner["i_cal_uid"],
            "winner_meeting_id": winner["meeting_id"],
            "suppressed_lecture_id": loser["lecture_id"],
            "suppressed_calendar_event_id": loser["calendar_event_id"],
            "suppressed_i_cal_uid": loser["i_cal_uid"],
            "suppressed_calendar_mapping_status":
                loser.get("calendar_mapping_status"),
            "group_key": _key_detail(loser),
            "resolved_at": resolved_at,
        },
    }


def _key_detail(row) -> dict:
    return {"session_date": _text(row["session_date"]),
            "normalized_subject": row["normalized_subject"],
            "module": row["module"],
            "scheduled_start": _text(row["scheduled_start"]),
            "scheduled_end": _text(row["scheduled_end"])}


def _member(row) -> dict:
    return {"lecture_id": row["lecture_id"],
            "calendar_event_id": row["calendar_event_id"],
            "i_cal_uid": row["i_cal_uid"],
            "meeting_id": row["meeting_id"],
            "calendar_mapping_status": row.get("calendar_mapping_status"),
            "organizer_validation_status": row.get("organizer_validation_status"),
            "downstream_ready": row.get("downstream_ready"),
            "confidently_resolved": is_confidently_resolved(row),
            "suppressible": is_suppressible(row),
            # Whether the row ALREADY carries the annotation. Classification
            # describes the calendar shape and is unchanged by having acted on
            # it before, so re-deciding a settled group is always safe.
            "suppression": row.get("suppression")}


def _text(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def suppression_of(row) -> dict | None:
    """The stored annotation, if this occurrence has already been suppressed."""
    metadata = row.get("metadata") or {}
    found = metadata.get(SUPPRESSION_KEY)
    return found if isinstance(found, dict) else None
