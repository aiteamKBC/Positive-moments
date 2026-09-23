"""
Write modes and the ownership policy.

The legacy QA tables hold production history written by n8n. The coded writer
must never take silent ownership of those rows, so every decision runs through
one policy function whose default outcome is "do nothing".
"""

# Modes, least to most capable. DISABLED and DRY_RUN never write.
DISABLED = "DISABLED"
DRY_RUN = "DRY_RUN"
CANARY_NEW_ONLY = "CANARY_NEW_ONLY"
PRODUCTION_NEW_ONLY = "PRODUCTION_NEW_ONLY"
EXPLICIT_BACKFILL = "EXPLICIT_BACKFILL"

WRITE_MODES = (DISABLED, DRY_RUN, CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY, EXPLICIT_BACKFILL)
# Anything not in here cannot touch a legacy table, by construction.
WRITE_ENABLED_MODES = frozenset({CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY, EXPLICIT_BACKFILL})
DEFAULT_MODE = DRY_RUN

# Decisions the planner can reach.
WOULD_INSERT = "WOULD_INSERT"
WOULD_UPDATE = "WOULD_UPDATE"
WOULD_SKIP_IDENTICAL = "WOULD_SKIP_IDENTICAL"
PROTECTED_EXISTING_LEGACY_ROW = "PROTECTED_EXISTING_LEGACY_ROW"
BLOCKED_NOT_READY = "BLOCKED_NOT_READY"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
BLOCKED_INVALID_PAYLOAD = "BLOCKED_INVALID_PAYLOAD"
BLOCKED_BACKFILL_NOT_AUTHORISED = "BLOCKED_BACKFILL_NOT_AUTHORISED"
# More than one existing legacy row could be this lecture occurrence, or the
# evidence cannot tell. Never resolved by a machine: see app/writer/legacy_identity.py.
BLOCKED_AMBIGUOUS_LEGACY_IDENTITY = "BLOCKED_AMBIGUOUS_LEGACY_IDENTITY"

INSERTING_DECISIONS = frozenset({WOULD_INSERT})
UPDATING_DECISIONS = frozenset({WOULD_UPDATE})
ACTIONABLE_DECISIONS = INSERTING_DECISIONS | UPDATING_DECISIONS

# Phase 3B render statuses that may reach the legacy tables at all.
READY_RENDER_STATUSES = frozenset({"RENDERED", "RENDERED_NON_DELIVERED"})
# Phase 3A evaluation statuses that may reach the legacy tables.
READY_QA_STATUSES = frozenset({"COMPLETED", "NON_DELIVERED"})


class WriterModeError(RuntimeError):
    """An unknown or unauthorised writer configuration."""


def validate_mode(mode: str) -> str:
    if mode not in WRITE_MODES:
        raise WriterModeError(f"unknown writer mode: {mode}")
    return mode


def writes_enabled(mode: str) -> bool:
    return validate_mode(mode) in WRITE_ENABLED_MODES


def plan_decision(*, mode: str, render_status: str, qa_status: str,
                  payload_valid: bool, target_exists: bool, coded_owned: bool,
                  fingerprint_matches: bool,
                  allow_update_existing: bool = False) -> str:
    """
    Decide what would happen to ONE lecture's legacy rows.

    The order matters, and every branch defaults to refusing:

    1. the source must be a finalized Phase 3B payload;
    2. the payload must satisfy the checklist invariant;
    3. a target that exists but was not written by this writer is PROTECTED -
       including all six historical 2026-09-04 sessions;
    4. a coded-owned target with an unchanged fingerprint is a NOOP;
    5. a coded-owned target with a changed fingerprint may be updated, and in
       EXPLICIT_BACKFILL only with an explicit allow-update flag.
    """
    validate_mode(mode)
    if qa_status not in READY_QA_STATUSES or render_status not in READY_RENDER_STATUSES:
        return BLOCKED_NOT_READY
    if not payload_valid:
        return BLOCKED_INVALID_PAYLOAD

    if not target_exists:
        return WOULD_INSERT
    if not coded_owned:
        # Legacy history is never taken over, in any mode.
        return PROTECTED_EXISTING_LEGACY_ROW
    if fingerprint_matches:
        return WOULD_SKIP_IDENTICAL
    if mode == EXPLICIT_BACKFILL and not allow_update_existing:
        return BLOCKED_BACKFILL_NOT_AUTHORISED
    return WOULD_UPDATE


# ---------------------------------------------------------------------------
# Phase 3C2.3D: the Perfect Lecture legacy output.
#
# A second, DERIVED target with its own lifecycle. It is planned separately
# from the QA session because legacy itself treats it separately - the child
# upserts it after the QA rows, in its own statement, with
# onError: continueRegularOutput - and because its row is subsequently
# enriched by workflows the coded platform does not own.
# ---------------------------------------------------------------------------
PERFECT_WOULD_INSERT = "PERFECT_WOULD_INSERT"
PERFECT_WOULD_UPDATE = "PERFECT_WOULD_UPDATE"
PERFECT_WOULD_SKIP_IDENTICAL = "PERFECT_WOULD_SKIP_IDENTICAL"
# Not eligible and no legacy row: the correct outcome is to do nothing at all.
PERFECT_NOT_ELIGIBLE = "PERFECT_NOT_ELIGIBLE"
# Not eligible, but a legacy-owned row exists. Reported, never resolved here.
PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS = "PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS"
# Was perfect, is no longer, and the row is OURS. The row is left standing and
# the divergence is recorded; deleting it is a separate explicit rollback.
PERFECT_SUPERSEDED_NOT_PERFECT = "PERFECT_SUPERSEDED_NOT_PERFECT"
PERFECT_PROTECTED_EXISTING_LEGACY_ROW = "PERFECT_PROTECTED_EXISTING_LEGACY_ROW"
PERFECT_BLOCKED_KEY_COLLISION = "PERFECT_BLOCKED_KEY_COLLISION"
PERFECT_BLOCKED_NOT_READY = "PERFECT_BLOCKED_NOT_READY"
PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED = "PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED"
PERFECT_BLOCKED_INVALID_PAYLOAD = "PERFECT_BLOCKED_INVALID_PAYLOAD"
# Phase 3C3D. Otherwise perfect, but the platform has no attendance evidence
# for the lecture. Explicitly NON-FINAL: nothing is written, nothing is owned,
# nothing is superseded, and it clears on its own when the source arrives.
PERFECT_PENDING_ATTENDANCE_DATA = "PERFECT_PENDING_ATTENDANCE_DATA"

PERFECT_ACTIONABLE_DECISIONS = frozenset({PERFECT_WOULD_INSERT, PERFECT_WOULD_UPDATE})
PERFECT_DECISIONS = frozenset({
    PERFECT_WOULD_INSERT, PERFECT_WOULD_UPDATE, PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_NOT_ELIGIBLE, PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS,
    PERFECT_SUPERSEDED_NOT_PERFECT, PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_BLOCKED_KEY_COLLISION, PERFECT_BLOCKED_NOT_READY,
    PERFECT_BLOCKED_INVALID_PAYLOAD, PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED,
    PERFECT_PENDING_ATTENDANCE_DATA})


# Why a coded-owned target would be rewritten.
PERFECT_UPDATE_SOURCE_CHANGED = "SOURCE_CHANGED"
PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED = "MAPPING_OUTPUT_CHANGED"


def plan_perfect_decision(*, mode: str, render_status: str, qa_status: str,
                          payload_valid: bool, is_perfect: bool,
                          target_exists: bool, coded_owned: bool,
                          foreign_session_on_key: bool,
                          fingerprint_matches: bool,
                          mapped_output_matches: bool = True,
                          allow_update_existing: bool = False,
                          attendance_pending: bool = False) -> str:
    """
    Decide what would happen to ONE lecture's legacy Perfect Lecture row.

    The order matters, and every branch defaults to doing nothing:

    1. the source must be a finalized Phase 3A/3B result;
    2. the payload must map cleanly onto the eleven legacy columns;
    3. a lecture_key already pointing at a DIFFERENT session is the known
       collision hazard and is refused before ownership is even considered -
       production already contains one date+subject pair mapping to two
       sessions;
    4. not eligible: never insert, never delete. If a row exists, say so;
    5. a row that exists but is not coded-owned is PROTECTED, in every mode;
    6. a coded-owned row is a no-op ONLY when the frozen source is unchanged
       AND the mapped payload this writer would produce today still equals
       what the target holds. An unchanged fingerprint alone is not enough:
       the source can be frozen while the MAPPING changes, which is exactly
       how a wrong attended_count reached production and then survived a
       second identical run.
    """
    validate_mode(mode)
    if qa_status not in READY_QA_STATUSES or render_status not in READY_RENDER_STATUSES:
        return PERFECT_BLOCKED_NOT_READY
    if not payload_valid:
        return PERFECT_BLOCKED_INVALID_PAYLOAD
    if foreign_session_on_key:
        return PERFECT_BLOCKED_KEY_COLLISION

    # Phase 3C3D, and deliberately BEFORE the not-eligible branch. "Cannot be
    # decided yet" must never be mistaken for "decided: no", because the
    # not-eligible branch can SUPERSEDE a row this platform already owns. A
    # pending state supersedes nothing and deletes nothing.
    if attendance_pending:
        return PERFECT_PENDING_ATTENDANCE_DATA

    if not is_perfect:
        if not target_exists:
            return PERFECT_NOT_ELIGIBLE
        if coded_owned:
            return PERFECT_SUPERSEDED_NOT_PERFECT
        return PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS

    if not target_exists:
        return PERFECT_WOULD_INSERT
    if not coded_owned:
        return PERFECT_PROTECTED_EXISTING_LEGACY_ROW
    if fingerprint_matches and mapped_output_matches:
        return PERFECT_WOULD_SKIP_IDENTICAL
    if mode == EXPLICIT_BACKFILL and not allow_update_existing:
        # Same posture as the QA writer: a backfill may only rewrite an
        # existing row when the caller says so explicitly.
        return PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED
    return PERFECT_WOULD_UPDATE
