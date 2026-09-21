"""
Phase 4B: which writer decisions a scheduler may act on without a human.

WHY THIS IS A SEPARATE MODULE
-----------------------------
Phase 4A treated every legacy production write as operator-only. That was the
right default for a first orchestrator, and the wrong one for unattended
operation: a lecture whose QA is finished, rendered and verified would sit at
`OPERATOR_ACTION_REQUIRED` for ever because nobody clicked anything.

But "the scheduler may write" and "the scheduler may write ANYTHING" are very
different statements. The safe set is small, closed, and listed here by name,
so that widening it is a visible edit to a file whose only job is to say what
is safe - not an emergent consequence of a change somewhere in the writer.

WHAT MAKES A DECISION SAFE
--------------------------
Exactly two things, and they are both about the ABSENCE of ambiguity:

  * `WOULD_INSERT` - no legacy row exists, so there is nothing to overwrite,
    nobody else's history to take over, and no question about who owns what.
  * `WOULD_SKIP_IDENTICAL` - the row exists, we wrote it, and it already says
    what we would say. Doing nothing is the whole action.

Everything else is ambiguous in a way a machine must not resolve alone:

  * `WOULD_UPDATE` - something changed. WHAT changed, and whether rewriting a
    published row is the right response, is a judgement.
  * `PROTECTED_EXISTING_LEGACY_ROW` - the row is n8n's. Never, in any mode.
  * `PERFECT_BLOCKED_KEY_COLLISION` - `date|subject` already points at a
    different session. Production already contains one such pair.
  * `PERFECT_SUPERSEDED_NOT_PERFECT` - a row we own now disagrees with the
    current answer. Deleting or rewriting it has outward consequences: the
    Excel Sync workflow may already have published it.
  * anything `BLOCKED_*` - the writer refused for a stated reason, and a
    scheduler's job is to relay that reason, not to look for a way around it.

THE DRY RUN IS NOT OPTIONAL
---------------------------
The gate reads a decision the writer's own planner produced. It never predicts
one. The orchestrator plans under `DRY_RUN`, asks this module, and only then
runs the writer again in a write-enabled mode - where the planner runs a SECOND
time and can still refuse. Two independent refusals guard every automated
write, and the last word always belongs to the writer.
"""
from app.writer.modes import (
    BLOCKED_BACKFILL_NOT_AUTHORISED,
    BLOCKED_INVALID_PAYLOAD,
    BLOCKED_NOT_READY,
    PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED,
    PERFECT_BLOCKED_INVALID_PAYLOAD,
    PERFECT_BLOCKED_KEY_COLLISION,
    PERFECT_BLOCKED_NOT_READY,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_SUPERSEDED_NOT_PERFECT,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_WOULD_UPDATE,
    PROTECTED_EXISTING_LEGACY_ROW,
    REVIEW_REQUIRED,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
)


AUTO_SYNC_POLICY_VERSION = "automated_legacy_sync_v1"

# --- QA -----------------------------------------------------------------

# The scheduler performs the write.
QA_AUTO_WRITE = frozenset({WOULD_INSERT})
# Already correct. The action is to do nothing, and that counts as success.
QA_AUTO_NOOP = frozenset({WOULD_SKIP_IDENTICAL})
QA_AUTO_APPROVED = QA_AUTO_WRITE | QA_AUTO_NOOP

# Named so the reason an operator sees is the writer's own word, not a
# paraphrase invented here.
QA_MANUAL = {
    WOULD_UPDATE: "LEGACY_ROW_WOULD_BE_UPDATED",
    PROTECTED_EXISTING_LEGACY_ROW: "LEGACY_ROW_NOT_CODED_OWNED",
    BLOCKED_NOT_READY: "SOURCE_NOT_READY",
    BLOCKED_INVALID_PAYLOAD: "PAYLOAD_INVARIANT_FAILED",
    BLOCKED_BACKFILL_NOT_AUTHORISED: "BACKFILL_NOT_AUTHORISED",
    REVIEW_REQUIRED: "WRITER_REQUIRES_REVIEW",
}

# --- Perfect ------------------------------------------------------------

PERFECT_AUTO_WRITE = frozenset({PERFECT_WOULD_INSERT})
PERFECT_AUTO_NOOP = frozenset({PERFECT_WOULD_SKIP_IDENTICAL})
# Not eligible with no legacy row is the correct, complete outcome: there is
# nothing to sync and nothing to report.
PERFECT_AUTO_NOTHING_TO_SYNC = frozenset({PERFECT_NOT_ELIGIBLE})
# Not a failure and not work: the attendance source has not answered yet.
PERFECT_AUTO_WAITING = frozenset({PERFECT_PENDING_ATTENDANCE_DATA})
PERFECT_AUTO_APPROVED = (PERFECT_AUTO_WRITE | PERFECT_AUTO_NOOP
                         | PERFECT_AUTO_NOTHING_TO_SYNC | PERFECT_AUTO_WAITING)

PERFECT_MANUAL = {
    PERFECT_WOULD_UPDATE: "PERFECT_ROW_WOULD_BE_UPDATED",
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW: "PERFECT_ROW_NOT_CODED_OWNED",
    PERFECT_BLOCKED_KEY_COLLISION: "PERFECT_LECTURE_KEY_COLLISION",
    PERFECT_SUPERSEDED_NOT_PERFECT: "PERFECT_ROW_SUPERSEDED_NOT_PERFECT",
    PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS: "PERFECT_ROW_EXISTS_BUT_NOT_ELIGIBLE",
    PERFECT_BLOCKED_NOT_READY: "PERFECT_SOURCE_NOT_READY",
    PERFECT_BLOCKED_INVALID_PAYLOAD: "PERFECT_PAYLOAD_INVARIANT_FAILED",
    PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED: "PERFECT_BACKFILL_NOT_AUTHORISED",
}


UNKNOWN_DECISION = "UNKNOWN_WRITER_DECISION"


def classify_qa(decision: str) -> dict:
    """What the scheduler may do about one QA writer decision."""
    if decision in QA_AUTO_WRITE:
        return _safe(decision, "WRITE")
    if decision in QA_AUTO_NOOP:
        return _safe(decision, "NOOP")
    # An unrecognised decision is never auto-approved. A new writer outcome
    # must be classified deliberately, not inherit permission by default.
    return _manual(decision, QA_MANUAL.get(decision, UNKNOWN_DECISION))


def classify_perfect(decision: str) -> dict:
    """What the scheduler may do about one Perfect writer decision."""
    if decision in PERFECT_AUTO_WRITE:
        return _safe(decision, "WRITE")
    if decision in PERFECT_AUTO_NOOP:
        return _safe(decision, "NOOP")
    if decision in PERFECT_AUTO_NOTHING_TO_SYNC:
        return _safe(decision, "NOTHING_TO_SYNC")
    if decision in PERFECT_AUTO_WAITING:
        return _safe(decision, "WAITING")
    return _manual(decision, PERFECT_MANUAL.get(decision, UNKNOWN_DECISION))


def classify_plan(plan: dict) -> dict:
    """
    Classify a whole per-lecture writer plan: the QA target and, when the
    Perfect planner ran, the derived Perfect target too.

    The QA verdict governs whether the write pass happens at all. An unsafe
    PERFECT decision does not stop a safe QA insert, because the Perfect
    planner will refuse its own write on the second pass anyway - but it is
    reported, so nothing unsafe passes silently.
    """
    qa = classify_qa(plan.get("decision"))
    perfect_plan = plan.get("perfect_lecture") or {}
    perfect_decision = perfect_plan.get("perfect_decision")
    perfect = (classify_perfect(perfect_decision) if perfect_decision
               else {"decision": None, "auto_approved": True, "action": "NOT_PLANNED",
                     "reason_code": None})
    reasons = [item["reason_code"] for item in (qa, perfect)
               if not item["auto_approved"]]
    may_write_qa = qa["auto_approved"] and qa["action"] == "WRITE"
    may_write_perfect = perfect["auto_approved"] and perfect["action"] == "WRITE"
    return {
        "auto_sync_policy_version": AUTO_SYNC_POLICY_VERSION,
        "qa": qa, "perfect": perfect,
        "may_write_qa": may_write_qa,
        "may_write_perfect": may_write_perfect,
        # The Perfect planner is attached to the write pass ONLY when its own
        # decision is safe. `PERFECT_WOULD_UPDATE` is actionable to the writer
        # and would be performed under PRODUCTION_NEW_ONLY, so leaving the
        # planner out is what actually prevents an unexpected Perfect rewrite -
        # rather than trusting the writer to decline something it is entitled
        # to do.
        "include_perfect_planner": perfect["auto_approved"],
        # A run happens if either target has safe work. An unsafe Perfect
        # never blocks a safe QA insert: the two are separate legacy targets
        # with separate lifecycles, which is why Phase 3C2.3D planned them
        # separately in the first place.
        "may_write": may_write_qa or may_write_perfect,
        "requires_manual_review": bool(reasons),
        "reason_codes": reasons,
    }


def _safe(decision, action) -> dict:
    return {"decision": decision, "auto_approved": True, "action": action,
            "reason_code": None}


def _manual(decision, reason) -> dict:
    return {"decision": decision, "auto_approved": False,
            "action": "MANUAL_REVIEW_REQUIRED", "reason_code": reason}
