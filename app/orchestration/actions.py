"""
Phase 5B: the guarded operator actions.

THREE ACTIONS, NAMED INDIVIDUALLY
---------------------------------
`RETRY`, `RECOVER_ATTENDANCE` and `RECONCILE`. There is deliberately no
generic `execute(action_name)` entry point anywhere in this module or the API
above it. A console that can post an arbitrary action name is a console that
can force-reprocess a settled lecture by typo, and no amount of validation
downstream makes that shape safe.

`FORCE_REPROCESS` is not here and must not be added. It creates new provenance,
it is an operator CLI decision, and nothing reachable from HTTP may perform it.

PLAN, THEN EXECUTE
------------------
Every mutating action is two calls. `plan_*` is read-only and answers three
questions an operator needs before clicking: what will actually run, what will
it cost, and what else will it touch. `execute_*` then re-derives the same plan
from the database and refuses if it has changed - so a stale browser tab cannot
execute a decision that was true five minutes ago.

EXECUTION GOES THROUGH THE REAL ORCHESTRATOR
--------------------------------------------
Not a lighter copy of it. `execute_*` calls `PipelineOrchestrator.run_window`
narrowed to one lecture, which means the operator's click gets the advisory
locks, the n8n precheck, the audit run row, the pass loop, the stall detection
and every refusal the scheduler gets. A retry from the console and a retry from
the nightly cycle are the same code path, and that is the only way they stay
the same behaviour.
"""
import logging
from datetime import date as _date

from app.orchestration.runner import (
    GRAPH_ACTIONS,
    LEGACY_WRITE_ACTIONS,
    PROVIDER_ACTIONS,
)
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    RECOVER_ATTENDANCE,
    RETRY,
    RUN_TYPE_MANUAL,
    RUN_TYPE_RECONCILE,
    WAIT_FOR_ATTENDANCE_SOURCE,
)


# The complete set of actions an operator may trigger from the console.
ACTION_RETRY = "RETRY"
ACTION_RECOVER_ATTENDANCE = "RECOVER_ATTENDANCE"
ACTION_RECONCILE = "RECONCILE"
OPERATOR_ACTIONS = frozenset({ACTION_RETRY, ACTION_RECOVER_ATTENDANCE,
                              ACTION_RECONCILE})

# Refusal codes. Each one is a specific, reportable reason - never a bare
# "not allowed", because an operator who is refused needs to know what to do.
REFUSED_NOT_ELIGIBLE = "RETRY_NOT_ELIGIBLE"
REFUSED_NOT_WAITING = "LECTURE_IS_NOT_WAITING_ON_ATTENDANCE"
REFUSED_PLAN_CHANGED = "PLAN_CHANGED_SINCE_IT_WAS_SHOWN"
REFUSED_SUPPRESSED = "LECTURE_IS_A_SUPPRESSED_DUPLICATE"


class ActionRefused(RuntimeError):
    """The action was not performed, and the reason is reportable."""

    def __init__(self, code: str, detail: str = "", plan: dict | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code
        self.plan = plan or {}


def action_cost(action: str, *, scope: str) -> dict:
    """
    What this action will spend, stated before it is confirmed.

    Derived from the runner's own gate sets rather than a second list, so an
    action that becomes expensive later becomes expensive here too.
    """
    return {
        "action": action,
        "scope": scope,
        "calls_microsoft_graph": action in GRAPH_ACTIONS,
        "buys_model_generation": action in PROVIDER_ACTIONS,
        "writes_legacy_production_row": action in LEGACY_WRITE_ACTIONS,
        "is_automatable": action in AUTOMATABLE_ACTIONS,
        # The honest caveat. Transcript acquisition, selection, canonical
        # parsing and the speaker inventory are day-scoped services: retrying
        # ONE lecture that is blocked on one of them runs it for the whole day.
        "affects_other_lectures_that_day": scope == "DAY",
    }


class GuardedActionService:
    """
    Plan and perform the three operator actions.

    `orchestrator_factory` is a callable rather than an orchestrator, because
    the two executing actions need DIFFERENT orchestrators: recovering
    attendance is built with the provider switched off, so that clicking it
    cannot buy a generation even if some future stage would like one.
    """

    def __init__(self, *, resolver, operations, orchestrator_factory):
        self.resolver = resolver
        self.operations = operations
        self.orchestrator_factory = orchestrator_factory
        self.log = logging.getLogger(__name__)

    # -- RETRY ---------------------------------------------------------------

    def plan_retry(self, connection, lecture_id) -> dict:
        """
        What a retry would do. Read-only.

        Retry means exactly one thing: resume from the earliest incomplete or
        stale stage using state that is already valid. It can never reach a
        COMPLETE stage, so the plan is simply the platform's own next action.
        """
        state = self.resolver.for_lecture(connection, lecture_id)
        plan = self.operations.request_retry(connection, lecture_id)
        runner_scope = self._scope_of(plan["action"])
        return {
            **plan,
            "lecture_id": state["lecture_id"],
            "subject": state["subject"],
            "session_date": state["session_date"],
            "semantics": RETRY,
            "force_reprocess": False,
            "current_state": {
                "next_action": state["next_action"],
                "next_executable_action": state["next_executable_action"],
                "blocking_stage": state["blocking_stage"],
                "is_waiting": state["is_waiting"],
                "requires_review": state["requires_review"],
            },
            "cost": action_cost(plan["action"], scope=runner_scope),
            # Stages that are already settled. Named so the confirmation can
            # say plainly what will NOT be redone.
            "stages_preserved": [name for name, item in state["stages"].items()
                                 if item["state"] in ("COMPLETE", "NOT_APPLICABLE")],
        }

    def execute_retry(self, connection, lecture_id, *, expected_action=None) -> dict:
        """
        Perform the retry through the real orchestrator, narrowed to one lecture.
        """
        plan = self.plan_retry(connection, lecture_id)
        self._require_eligible(plan, expected_action)
        orchestrator = self.orchestrator_factory(allow_provider=True)
        return self._run(orchestrator, connection, plan,
                         run_type=RUN_TYPE_MANUAL, requested=ACTION_RETRY)

    # -- RECOVER ATTENDANCE ----------------------------------------------------

    def plan_recover_attendance(self, connection, lecture_id) -> dict:
        """
        Offered only when the lecture is genuinely waiting on the attendance
        source. Anything else is refused rather than quietly turned into a
        different action.
        """
        state = self.resolver.for_lecture(connection, lecture_id)
        waiting = (state["next_executable_action"] == WAIT_FOR_ATTENDANCE_SOURCE
                   and state["stages"]["ATTENDANCE"]["state"] == "WAITING")
        return {
            "lecture_id": state["lecture_id"],
            "subject": state["subject"],
            "session_date": state["session_date"],
            "semantics": RETRY,
            "available": waiting,
            "reason": None if waiting else REFUSED_NOT_WAITING,
            "action": RECOVER_ATTENDANCE,
            "attendance_coverage_status": state["attendance_coverage_status"],
            "attendance_source_authoritative":
                state["attendance_source_authoritative"],
            # The probe is a read against the attendance source and nothing
            # more. If the source is still empty it writes nothing at all.
            "cost": {**action_cost(RECOVER_ATTENDANCE,
                                   scope=self._scope_of(RECOVER_ATTENDANCE)),
                     "buys_model_generation": False,
                     "reruns_qa": False},
        }

    def execute_recover_attendance(self, connection, lecture_id) -> dict:
        plan = self.plan_recover_attendance(connection, lecture_id)
        if not plan["available"]:
            raise ActionRefused(REFUSED_NOT_WAITING,
                                "this lecture is not waiting on the attendance source",
                                plan)
        # Provider OFF, structurally. Clicking "recover attendance" must never
        # be able to buy a generation, whatever any downstream stage would
        # like to do once the roster arrives.
        orchestrator = self.orchestrator_factory(allow_provider=False)
        return self._run(orchestrator, connection, plan,
                         run_type=RUN_TYPE_MANUAL,
                         requested=ACTION_RECOVER_ATTENDANCE)

    # -- RECONCILE ---------------------------------------------------------------

    def reconcile(self, connection, session_date: _date) -> dict:
        """
        Re-evaluate a day from persisted state. READ-ONLY.

        Deliberately not a cycle. Reconcile answers "what is true now?", which
        is a question about the database, not an instruction to do work - and
        an operator who wanted work done has the other two buttons. Turning
        this into a force reprocess is the single most likely way this feature
        could become dangerous, so it does not run the orchestrator at all.
        """
        report = self.operations.day_reconciliation(connection, session_date)
        return {"semantics": ACTION_RECONCILE, "read_only": True,
                "executed_any_stage": False, "report": report}

    # -- helpers -------------------------------------------------------------------

    def _scope_of(self, action) -> str:
        runner = getattr(self.orchestrator_factory(allow_provider=False), "runner",
                         None)
        if runner is None or action is None:
            return "LECTURE"
        return runner.scope_of(action)

    def _require_eligible(self, plan, expected_action) -> None:
        if plan.get("retry_eligible") is not True:
            raise ActionRefused(REFUSED_NOT_ELIGIBLE,
                                plan.get("retry_reason") or REFUSED_NOT_ELIGIBLE,
                                plan)
        if expected_action and plan["action"] != expected_action:
            # The operator confirmed a different plan from the one that is now
            # true. Refusing is right: a stale tab must not execute yesterday's
            # decision.
            raise ActionRefused(
                REFUSED_PLAN_CHANGED,
                f"the plan is now {plan['action']}, not {expected_action}", plan)

    def _run(self, orchestrator, connection, plan, *, run_type, requested) -> dict:
        lecture_id = plan["lecture_id"]
        session_date = _date.fromisoformat(plan["session_date"])
        self.log.info("operator action", extra={"fields": {
            "service": "operations_actions", "requested_action": requested,
            "lecture_id": lecture_id, "planned_action": plan.get("action")}})
        summary = orchestrator.run_window(
            connection, session_date, run_type=run_type,
            lecture_ids=[lecture_id])
        return {"requested_action": requested, "semantics": RETRY,
                "force_reprocess": False, "lecture_id": lecture_id,
                "plan": plan, "run": summary}


def reconcile_run_type() -> str:
    """The audit label for a reconcile, kept next to the action that uses it."""
    return RUN_TYPE_RECONCILE
