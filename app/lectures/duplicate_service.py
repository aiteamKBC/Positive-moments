"""
Phase 4C1: the duplicate resolution service.

The rule lives in `app.lectures.duplicates` and knows nothing about a database.
The SQL lives in `app.db.repositories.lecture_duplicates` and knows nothing
about the rule. This service is the only place they meet, and it adds exactly
two things of its own:

  1. DRY RUN BY DEFAULT. `plan_*` decides and returns; `resolve_*` decides and
     writes. Nothing here writes unless a caller asked for `persist=True`, so
     the historical scan can be run and read before anything is changed.

  2. A LAST-MOMENT SAFETY CHECK. The rule decides from the calendar; this
     re-reads the candidate's downstream footprint immediately before writing
     and refuses if the occurrence ever produced a transcript, a canonical
     document, an evaluation or a legacy row. The rule should never select
     such a row - an unresolved occurrence has no meeting and so can have no
     transcript - but "should never" is not a guarantee, and this is the
     cheapest possible way to make it one.

No Graph call, no provider call, no write to any table other than the
registry, and no DELETE anywhere in the path.
"""
import logging

from app.db.repositories.lecture_duplicates import DuplicateEventRepository
from app.lectures.duplicates import (
    CASE_NOT_DUPLICATE,
    CASE_SUPPRESSED,
    DUPLICATE_RESOLUTION_VERSION,
    classify_group,
    group_rows,
)


# Outcomes of one suppression attempt.
SUPPRESSED = "SUPPRESSED"
ALREADY_SUPPRESSED = "ALREADY_SUPPRESSED"
REFUSED_DOWNSTREAM_FOOTPRINT = "REFUSED_DOWNSTREAM_FOOTPRINT"
REFUSED_RACED = "REFUSED_NO_LONGER_SUPPRESSIBLE"
NOT_APPLICABLE = "NOT_A_SUPPRESSIBLE_DUPLICATE"


class DuplicateResolutionService:
    """Group, decide, and - only when asked - annotate."""

    def __init__(self, *, repository=None, now=None):
        self.repository = repository or DuplicateEventRepository()
        self.now = now
        self.log = logging.getLogger(__name__)

    # -- deciding -----------------------------------------------------------

    def classify_lecture(self, connection, lecture_id) -> dict:
        """
        The group this lecture belongs to, decided.

        Also reports this lecture's own role in it, which is what the state
        resolver needs: winner, suppressible loser, or a member of a group
        that needs a human.
        """
        rows = self.repository.load_group_for_lecture(connection, lecture_id)
        return self._with_role(classify_group(rows, now=self.now), str(lecture_id))

    def plan_day(self, connection, session_date) -> dict:
        """Every duplicate group on one day, decided. Reads only."""
        return self._plan(self.repository.load_day(connection, session_date),
                          scope=str(session_date))

    def plan_all(self, connection) -> dict:
        """
        Every duplicate group in the registry, decided. Reads only.

        This is the historical scan, and it is deliberately separate from
        anything that writes: the point of it is to be read by a person before
        a single historical row is touched.
        """
        return self._plan(self.repository.load_all_duplicate_groups(connection),
                          scope="ALL")

    # -- acting -------------------------------------------------------------

    def resolve_lecture(self, connection, lecture_id, *, persist: bool = False) -> dict:
        """
        Suppress this lecture if - and only if - it is the clearly-empty
        sibling of exactly one confidently resolved occurrence.
        """
        lecture_id = str(lecture_id)
        decision = self.classify_lecture(connection, lecture_id)
        outcome = {
            "lecture_id": lecture_id,
            "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
            "case": decision["case"], "role": decision["role"],
            "requires_manual_review": decision["requires_manual_review"],
            "winner_lecture_id": (decision["winner"]["lecture_id"]
                                  if decision["winner"] else None),
            "persisted": False, "suppressed_count": 0,
            "outcome": NOT_APPLICABLE, "footprint": None,
        }
        if decision["already_suppressed"]:
            outcome["outcome"] = ALREADY_SUPPRESSED
            return outcome
        annotation = next((item["annotation"] for item in decision["suppress"]
                           if item["lecture_id"] == lecture_id), None)
        if annotation is None:
            return outcome
        outcome["annotation"] = annotation
        if not persist:
            outcome["outcome"] = SUPPRESSED
            outcome["would_suppress"] = True
            return outcome

        footprint = self.repository.downstream_footprint(connection, lecture_id)
        outcome["footprint"] = footprint
        if any(footprint.values()):
            # The calendar says duplicate, the pipeline says this occurrence
            # produced real work. The pipeline wins and a human looks.
            self.log.warning("refusing to suppress a duplicate with downstream work",
                             extra={"fields": {"lecture_id": lecture_id,
                                               "footprint": footprint}})
            outcome["outcome"] = REFUSED_DOWNSTREAM_FOOTPRINT
            outcome["requires_manual_review"] = True
            return outcome

        written = self.repository.suppress(connection, lecture_id, annotation)
        outcome["persisted"] = written
        outcome["suppressed_count"] = 1 if written else 0
        # `suppress` refuses a row that gained a meeting, or was already
        # annotated, between the decision and the write.
        outcome["outcome"] = SUPPRESSED if written else REFUSED_RACED
        return outcome

    def resolve_day(self, connection, session_date, *, persist: bool = False) -> dict:
        """Decide every group on one day, and suppress the safe ones."""
        plan = self.plan_day(connection, session_date)
        results = []
        for group in plan["groups"]:
            for item in group["suppress"]:
                results.append(self.resolve_lecture(
                    connection, item["lecture_id"], persist=persist))
        return {**plan, "persisted": persist, "results": results,
                "suppressed_count": sum(item["suppressed_count"]
                                        for item in results)}

    # -- helpers ------------------------------------------------------------

    def _plan(self, rows, *, scope) -> dict:
        groups = []
        for members in group_rows(rows).values():
            if len(members) < 2:
                continue
            decision = classify_group(members, now=self.now)
            if decision["case"] == CASE_NOT_DUPLICATE:
                continue
            groups.append(_public(decision, members))
        return {
            "scope": scope,
            "duplicate_resolution_version": DUPLICATE_RESOLUTION_VERSION,
            "duplicate_group_count": len(groups),
            # What is still OUTSTANDING. A group decided and acted on last
            # cycle is still a Case A group, so counting its members here
            # would make a settled day look like it had work left for ever.
            "auto_suppressible_count": sum(
                len(group["pending_suppression"]) for group in groups
                if group["case"] == CASE_SUPPRESSED),
            "manual_review_group_count": sum(
                1 for group in groups if group["requires_manual_review"]),
            "already_suppressed_count": sum(
                1 for row in rows if row.get("suppression")),
            "groups": groups,
        }

    def _with_role(self, decision, lecture_id) -> dict:
        members = {item["lecture_id"]: item for item in decision["members"]}
        me = members.get(lecture_id, {})
        suppressible = {item["lecture_id"] for item in decision["suppress"]}
        winner = decision["winner"]
        if lecture_id in suppressible:
            role = "SUPPRESSIBLE_DUPLICATE"
        elif winner and winner["lecture_id"] == lecture_id:
            role = "WINNER"
        elif decision["case"] == CASE_NOT_DUPLICATE:
            role = "NOT_IN_A_DUPLICATE_GROUP"
        else:
            role = "MEMBER"
        return {**decision, "role": role,
                "already_suppressed": bool(me.get("suppression")),
                "lecture_id": lecture_id}


def _public(decision, members) -> dict:
    """The decision as a report row: no raw database rows leak out of here."""
    return {
        "case": decision["case"],
        "group_key": decision["group_key"],
        "group_size": decision["group_size"],
        "requires_manual_review": decision["requires_manual_review"],
        "winner_lecture_id": (decision["winner"]["lecture_id"]
                              if decision["winner"] else None),
        "winner_meeting_id": (decision["winner"]["meeting_id"]
                              if decision["winner"] else None),
        "members": decision["members"],
        "suppress": [{"lecture_id": item["lecture_id"],
                      "annotation": item["annotation"]}
                     for item in decision["suppress"]],
        "already_suppressed": [row["lecture_id"] for row in members
                               if row.get("suppression")],
        "pending_suppression": [item["lecture_id"]
                                for item in decision["suppress"]
                                if not _suppressed(members, item["lecture_id"])],
    }


def _suppressed(members, lecture_id) -> bool:
    return any(row["lecture_id"] == lecture_id and row.get("suppression")
               for row in members)
