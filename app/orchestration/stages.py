"""
Phase 4A: the orchestration vocabulary.

One module, no behaviour, no imports from the services it describes. The stage
order defined here IS the resume rule: "resume from the earliest incomplete
stage" is only a well-defined instruction if "earliest" has exactly one
meaning, and this tuple is that meaning.

WHY STATES AND ACTIONS ARE SEPARATE
-----------------------------------
A stage state answers "what is true?". An action answers "what should happen
next?". They are not the same question, and collapsing them was the mistake
the legacy pipeline made: it had no way to say "this lecture is fine, it is
simply waiting on somebody else", so it re-ran everything on every cycle.

`WAITING` is the state that makes unattended operation affordable. A lecture
waiting on an external source is not failed, not incomplete work we can do,
and not something to retry - it is a lecture whose next move belongs to
someone else. Recognising that is the difference between a scheduler that
costs nothing while it waits and one that buys a model generation every hour
for a lecture that will not change until the attendance source does.
"""

# --- stage states -----------------------------------------------------------

COMPLETE = "COMPLETE"
MISSING = "MISSING"
STALE = "STALE"
FAILED = "FAILED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
WAITING = "WAITING"
NOT_APPLICABLE = "NOT_APPLICABLE"

STAGE_STATES = (COMPLETE, MISSING, STALE, FAILED, REVIEW_REQUIRED, WAITING,
                NOT_APPLICABLE)

# A stage in one of these states needs nothing from the orchestrator. Every
# other state means the orchestrator has something to decide.
SETTLED_STATES = frozenset({COMPLETE, NOT_APPLICABLE})


# --- stages, in resume order ------------------------------------------------

DISCOVERY = "DISCOVERY"
MEETING = "MEETING"
TRANSCRIPT = "TRANSCRIPT"
SELECTION = "SELECTION"
CANONICAL_CUES = "CANONICAL_CUES"
SPEAKERS = "SPEAKERS"
ATTENDANCE = "ATTENDANCE"
ENGAGEMENT = "ENGAGEMENT"
QA_EVALUATION = "QA_EVALUATION"
QA_RENDER = "QA_RENDER"
PERFECT_ELIGIBILITY = "PERFECT_ELIGIBILITY"
LEGACY_QA_SYNC = "LEGACY_QA_SYNC"
PERFECT_SYNC = "PERFECT_SYNC"
RECORDING_LINK = "RECORDING_LINK"
EXCEL_SYNC = "EXCEL_SYNC"

# Phase 4B reordered PERFECT_ELIGIBILITY ahead of the two legacy writes,
# because that is the real dependency: eligibility is derived from the frozen
# Phase 3B render and has never needed the legacy row. The writes then happen
# QA-first and Perfect-second, which is also the order legacy's own child
# workflow uses.
STAGE_ORDER = (
    DISCOVERY, MEETING, TRANSCRIPT, SELECTION, CANONICAL_CUES, SPEAKERS,
    ATTENDANCE, ENGAGEMENT, QA_EVALUATION, QA_RENDER, PERFECT_ELIGIBILITY,
    LEGACY_QA_SYNC, PERFECT_SYNC, RECORDING_LINK, EXCEL_SYNC,
)

# Phase 4A OBSERVES these two; it never runs them. The recording branch of QA
# Master Daily v8 still owns recording links, and "QA Perfect Lectures - Excel
# Sync" still owns the Perfect export. Both are live and both are correct, so
# the coded platform reports their state and stays out of their way.
OBSERVED_ONLY_STAGES = frozenset({RECORDING_LINK, EXCEL_SYNC})

# The stages the orchestrator may itself execute.
EXECUTABLE_STAGES = tuple(stage for stage in STAGE_ORDER
                          if stage not in OBSERVED_ONLY_STAGES)


# --- next actions -----------------------------------------------------------

RUN_DISCOVERY = "RUN_DISCOVERY"
RESOLVE_MEETING = "RESOLVE_MEETING"
ACQUIRE_TRANSCRIPT = "ACQUIRE_TRANSCRIPT"
SELECT_TRANSCRIPT = "SELECT_TRANSCRIPT"
BUILD_CANONICAL_CUES = "BUILD_CANONICAL_CUES"
RESOLVE_SPEAKERS = "RESOLVE_SPEAKERS"
RESOLVE_ATTENDANCE = "RESOLVE_ATTENDANCE"
CALCULATE_ENGAGEMENT = "CALCULATE_ENGAGEMENT"
RUN_QA = "RUN_QA"
# Project-consistent name, already established by Phase 3C3E: a QA result made
# stale by NEW ATTENDANCE is refreshed deterministically from the frozen model
# answer, not regenerated. The two are different actions with different costs,
# so they keep different names.
REFRESH_DETERMINISTIC_QA = "REFRESH_DETERMINISTIC_QA"
# Phase 4B. Re-judge a STORED model answer under the current evidence policy.
# A third distinct cost: RUN_QA buys a generation, REFRESH_DETERMINISTIC_QA
# recomputes deterministic fields from a reusable answer, and this one only
# re-applies the evidence rule. All three touch QA_EVALUATION; only one of them
# spends money, which is exactly why they have separate names.
REVALIDATE_EVIDENCE = "REVALIDATE_EVIDENCE"
RENDER_QA = "RENDER_QA"
# Phase 4C1. Retire the empty sibling of a duplicate calendar booking. It is a
# registry annotation and nothing else: no Graph call, no provider call, no
# legacy row, no deletion. It is automatable because the deterministic rule in
# app/lectures/duplicates.py leaves nothing to choose between - every shape
# that requires a choice becomes MANUAL_REVIEW_REQUIRED instead.
SUPPRESS_DUPLICATE_EVENT = "SUPPRESS_DUPLICATE_EVENT"
SYNC_LEGACY_QA = "SYNC_LEGACY_QA"
WAIT_FOR_ATTENDANCE_SOURCE = "WAIT_FOR_ATTENDANCE_SOURCE"
RECOVER_ATTENDANCE = "RECOVER_ATTENDANCE"
EVALUATE_PERFECT = "EVALUATE_PERFECT"
SYNC_PERFECT = "SYNC_PERFECT"
WAIT_FOR_RECORDING = "WAIT_FOR_RECORDING"
WAIT_FOR_EXCEL_SYNC = "WAIT_FOR_EXCEL_SYNC"
NOTHING_TO_DO = "NOTHING_TO_DO"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

NEXT_ACTIONS = (
    RUN_DISCOVERY, RESOLVE_MEETING, ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT,
    BUILD_CANONICAL_CUES, RESOLVE_SPEAKERS, RESOLVE_ATTENDANCE,
    CALCULATE_ENGAGEMENT, RUN_QA, REFRESH_DETERMINISTIC_QA, REVALIDATE_EVIDENCE,
    RENDER_QA, SUPPRESS_DUPLICATE_EVENT, SYNC_LEGACY_QA,
    WAIT_FOR_ATTENDANCE_SOURCE, RECOVER_ATTENDANCE,
    EVALUATE_PERFECT, SYNC_PERFECT, WAIT_FOR_RECORDING, WAIT_FOR_EXCEL_SYNC,
    NOTHING_TO_DO, MANUAL_REVIEW_REQUIRED,
)

# Actions the scheduler is allowed to perform without an operator. Everything
# outside this set is either somebody else's job or a human decision, and the
# orchestrator records it rather than doing it.
AUTOMATABLE_ACTIONS = frozenset({
    RUN_DISCOVERY, ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT, BUILD_CANONICAL_CUES,
    RESOLVE_SPEAKERS, RESOLVE_ATTENDANCE, CALCULATE_ENGAGEMENT, RUN_QA,
    REFRESH_DETERMINISTIC_QA, REVALIDATE_EVIDENCE, RENDER_QA, RECOVER_ATTENDANCE,
    EVALUATE_PERFECT, SUPPRESS_DUPLICATE_EVENT,
    # Phase 4B. Automatable, but only for the short safe list in
    # app/orchestration/sync_safety.py, and only after the writer's own
    # planner has produced that decision under DRY_RUN. Anything else becomes
    # MANUAL_REVIEW_REQUIRED with the writer's own reason code.
    SYNC_LEGACY_QA, SYNC_PERFECT,
})

# Actions that mean "nothing for anyone to do right now". They are successful
# outcomes, not failures, and a cycle made entirely of them is a good cycle.
IDLE_ACTIONS = frozenset({
    NOTHING_TO_DO, WAIT_FOR_ATTENDANCE_SOURCE, WAIT_FOR_RECORDING,
    WAIT_FOR_EXCEL_SYNC,
})

# Actions that write to a legacy production table. Still lecture-scoped, still
# ownership-aware, still routed through the existing guarded writers - and
# since Phase 4B, performable by the scheduler when the writer's own planner
# says the decision is a clean insert or a true no-op.
PRODUCTION_WRITE_ACTIONS = frozenset({SYNC_LEGACY_QA, SYNC_PERFECT})

# Actions no automated path may ever take. Empty since Phase 4B, and kept
# because the concept is load-bearing: the executable search consults it, so
# re-gating an action is a one-line change here rather than a rewrite.
OPERATOR_ONLY_ACTIONS: frozenset = frozenset()


# --- run item outcomes ------------------------------------------------------

ITEM_SUCCEEDED = "SUCCEEDED"
ITEM_SKIPPED = "SKIPPED"
ITEM_WAITING = "WAITING"
ITEM_REVIEW = "REVIEW_REQUIRED"
ITEM_FAILED = "FAILED"
ITEM_BLOCKED = "BLOCKED"
ITEM_LOCKED = "LOCKED"

ITEM_STATUSES = (ITEM_SUCCEEDED, ITEM_SKIPPED, ITEM_WAITING, ITEM_REVIEW,
                 ITEM_FAILED, ITEM_BLOCKED, ITEM_LOCKED)


# --- run statuses -----------------------------------------------------------

RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
RUN_FAILED = "FAILED"
RUN_BLOCKED_LEGACY_QA_ACTIVE = "BLOCKED_LEGACY_QA_ACTIVE"
RUN_BLOCKED_WRITER_INTEGRITY = "BLOCKED_WRITER_INTEGRITY"
RUN_ABORTED = "ABORTED"

RUN_STATUSES = (RUN_RUNNING, RUN_COMPLETED, RUN_COMPLETED_WITH_FAILURES,
                RUN_FAILED, RUN_BLOCKED_LEGACY_QA_ACTIVE,
                RUN_BLOCKED_WRITER_INTEGRITY, RUN_ABORTED)


# --- run types --------------------------------------------------------------

RUN_TYPE_MANUAL = "MANUAL"
RUN_TYPE_SCHEDULED = "SCHEDULED"
RUN_TYPE_RECONCILE = "RECONCILE"
RUN_TYPE_DRY_RUN = "DRY_RUN"

RUN_TYPES = (RUN_TYPE_MANUAL, RUN_TYPE_SCHEDULED, RUN_TYPE_RECONCILE,
             RUN_TYPE_DRY_RUN)


# --- retry vs force reprocess ----------------------------------------------

# RETRY resumes from the earliest missing or stale stage using the state that
# is already valid. It creates no new provenance for anything that is already
# COMPLETE, and it is all the scheduler is ever allowed to do.
RETRY = "RETRY"
# FORCE_REPROCESS deliberately creates new provenance where the versioning
# rules allow it. It is an operator action, it is never scheduled, and nothing
# in this package can reach it.
FORCE_REPROCESS = "FORCE_REPROCESS"

SCHEDULER_SEMANTICS = RETRY


ORCHESTRATION_VERSION = "lecture_orchestration_v1"
