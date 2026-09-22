"""
Phase 4A Part D: the one-shot orchestrator.

This is the only entry point that MAKES the pipeline happen, and the scheduler
calls exactly this - there is no second implementation for the automated path.
A scheduler that duplicates its manual counterpart is a scheduler that is
tested in one shape and run in another.

WHAT IT IS
----------
A loop over lectures that asks the state resolver "what is the earliest thing
this lecture needs?" and performs exactly that, then asks again. Nothing here
knows what a checklist item is or what makes a lecture Perfect. Every decision
comes from `PipelineStateResolver`; every action goes to `StageRunner`.

WHY PASSES
----------
One lecture can need several stages in sequence, and a resolver answer is only
valid until something changes. Rather than hard-code a pipeline order a second
time, each pass advances every lecture by AT MOST ONE stage and then the state
is re-derived from the database. The loop stops when a whole pass changes
nothing, which is also precisely the definition of "this run is idempotent" -
so the property is enforced by the control flow rather than asserted about it.

The pass cap exists so a stage that reports success without changing its own
state cannot spin. Hitting the cap is a defect, and it is recorded as one.

ISOLATION
---------
A lecture is processed under its own advisory lock, and its failure is caught,
recorded and stepped over. Two exceptions, both deliberate:

  * a WRITER-INTEGRITY refusal stops the entire run. Whatever produced it,
    continuing means making more production writes while the guard that is
    supposed to bound them has already objected once.
  * a SHARED-INFRASTRUCTURE failure - Graph auth, the database itself - stops
    the run too, because the next lecture will fail the same way and a hundred
    identical failures in the audit log is noise, not information.
"""
import logging
import time
from datetime import date as _date, timedelta

from app.common.errors import (
    DATABASE_ERROR,
    GRAPH_AUTH_ERROR,
    GRAPH_PERMISSION_ERROR,
    WRITER_GUARD_REFUSED,
    PlatformError,
)
from app.orchestration.locks import (
    LectureBusy,
    NullCycleLock,
    NullLockManager,
    SchedulerCycleBusy,
)
from app.orchestration.runner import ManualReviewRequired
from app.orchestration.n8n_preflight import (
    LEGACY_QA_DISABLED,
    PRECHECK_SKIPPED,
    LegacyQaPreflight,
)
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    DEFAULT_MAX_PASSES,
    IDLE_ACTIONS,
    ITEM_BLOCKED,
    ITEM_FAILED,
    ITEM_LOCKED,
    ITEM_REVIEW,
    ITEM_SKIPPED,
    ITEM_SUCCEEDED,
    ITEM_WAITING,
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    ORCHESTRATION_VERSION,
    PRODUCTION_WRITE_ACTIONS,
    RECOVER_ATTENDANCE,
    RETRY,
    RUN_ABORTED,
    RUN_BLOCKED_LEGACY_QA_ACTIVE,
    RUN_BLOCKED_WRITER_INTEGRITY,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_FAILURES,
    RUN_RUNNING,
    RUN_TYPE_DRY_RUN,
    RUN_TYPE_MANUAL,
    WAIT_FOR_ATTENDANCE_SOURCE,
)


# A refusal from the writer guard is never "this lecture"; it is "the rules
# that bound production writes have objected", and the correct response is to
# stop making them.
WRITER_INTEGRITY_CODES = frozenset({WRITER_GUARD_REFUSED})
# Failures that will repeat identically for every remaining lecture.
SHARED_INFRASTRUCTURE_CODES = frozenset({GRAPH_AUTH_ERROR, GRAPH_PERMISSION_ERROR,
                                         DATABASE_ERROR})


class PipelineOrchestrator:
    def __init__(self, *, resolver, runner=None, run_repository=None,
                 lock_manager=None, cycle_lock=None, preflight=None,
                 discovery_service=None,
                 max_passes: int = DEFAULT_MAX_PASSES,
                 orchestration_version: str = ORCHESTRATION_VERSION):
        self.resolver = resolver
        self.runner = runner
        self.run_repository = run_repository
        self.lock_manager = lock_manager or NullLockManager()
        self.cycle_lock = cycle_lock or NullCycleLock()
        self.preflight = preflight or LegacyQaPreflight(required=False)
        self.discovery_service = discovery_service
        self.max_passes = max_passes
        self.orchestration_version = orchestration_version
        self.log = logging.getLogger(__name__)

    # -- entry point --------------------------------------------------------

    def run_window(self, connection, target_date: _date, *, lookback_days: int = 0,
                   run_type: str = RUN_TYPE_MANUAL, dry_run: bool = False,
                   discover: bool = False, aptem_connection=None,
                   aptem_connection_factory=None, lecture_ids=None) -> dict:
        """
        Reconcile one day plus an optional lookback, once.

        `dry_run` reports and writes nothing - not a run row, not a coded row,
        not a legacy row. The semantics are always RETRY: resume from the
        earliest missing or stale stage using state that is already valid.
        Nothing here can force-reprocess a COMPLETE stage, and there is no
        parameter that would let it.

        `lecture_ids` NARROWS the set of lectures considered and changes
        nothing else. Phase 5B needs it so that an operator asking to retry one
        lecture gets the real orchestrator - its locks, its audit row, its pass
        loop, its refusals - rather than a second, thinner execution path built
        for the console. A filter cannot make the cycle do anything it could
        not already do to that lecture on its own.

        It does not narrow DAY-SCOPED services: transcript acquisition,
        selection, canonical parsing and the speaker inventory are day-scoped
        by construction, so a retry that lands on one of them advances the
        whole day. That is a property of those services, not a decision made
        here, and `plan_action` states it before an operator confirms.
        """
        started = time.monotonic()
        # Part P: one cycle at a time. Taken before any Graph sweep or provider
        # call, so a second cycle cannot duplicate the expensive day-wide work
        # that per-lecture locks were never meant to cover.
        if not self.cycle_lock.try_acquire(connection):
            raise SchedulerCycleBusy(
                "another scheduler cycle is already running; standing down")
        try:
            return self._run_window(
                connection, target_date, lookback_days=lookback_days,
                run_type=run_type, dry_run=dry_run, discover=discover,
                aptem_connection=aptem_connection,
                aptem_connection_factory=aptem_connection_factory,
                lecture_ids=lecture_ids, started=started)
        finally:
            self.cycle_lock.release(connection)

    def _run_window(self, connection, target_date: _date, *, lookback_days: int,
                    run_type: str, dry_run: bool, discover: bool,
                    aptem_connection, aptem_connection_factory, started,
                    lecture_ids=None) -> dict:
        window_start = target_date - timedelta(days=max(lookback_days, 0))
        window = [window_start + timedelta(days=offset)
                  for offset in range((target_date - window_start).days + 1)]

        summary = {
            "run_id": None,
            "run_type": RUN_TYPE_DRY_RUN if dry_run else run_type,
            "orchestration_version": self.orchestration_version,
            "retry_semantics": RETRY, "force_reprocess": False,
            "target_date": target_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": target_date.isoformat(),
            "window_days": [day.isoformat() for day in window],
            "dry_run": dry_run,
            "graph_calls": 0, "provider_calls": 0, "legacy_rows_written": 0,
            "passes": 0, "discovery": None,
            "lectures": [], "day_stages": [], "errors": [],
            # Part N. Always present, so a real run also states plainly that it
            # would not have written a legacy row - the same claim, checkable
            # the same way, in both modes.
            **_projection(None),
        }

        # -- 1. the read-only legacy QA safety precheck ----------------------
        precheck = self.preflight.check()
        summary["legacy_qa_precheck"] = precheck
        blocked = precheck["status"] not in (LEGACY_QA_DISABLED, PRECHECK_SKIPPED)
        summary["writes_permitted"] = bool(precheck.get("writes_permitted")) and not dry_run

        run_id = None
        if self.run_repository is not None and not dry_run:
            run_id = self.run_repository.start(
                connection, run_type=run_type,
                orchestration_version=self.orchestration_version,
                target_date=target_date, window_start=window_start,
                window_end=target_date, status=RUN_RUNNING,
                legacy_qa_precheck_status=precheck["status"],
                legacy_qa_node_disabled=precheck.get("legacy_qa_node_disabled"),
                metadata={"window_days": summary["window_days"],
                          "lookback_days": lookback_days,
                          "retry_semantics": RETRY})
            summary["run_id"] = run_id

        if blocked:
            # No coded QA or Perfect write of any kind. The legacy pipeline may
            # be about to write the same rows, and two writers on one row is
            # the failure this whole precheck exists to prevent.
            return self._blocked(connection, summary, run_id, window, started,
                                 status=RUN_BLOCKED_LEGACY_QA_ACTIVE,
                                 reason=precheck.get("reason"),
                                 lecture_ids=lecture_ids)

        # -- 2. discover new lectures ----------------------------------------
        if discover and self.discovery_service is not None and not dry_run:
            summary["discovery"] = self._discover(
                connection, aptem_connection, aptem_connection_factory, window,
                summary)

        # -- 3..6. resolve, act, resolve again -------------------------------
        states = self._resolve_window(connection, window, lecture_ids)
        summary["initial"] = _snapshot(states)
        outcomes = {state["lecture_id"]: {
            "lecture_id": state["lecture_id"], "subject": state["subject"],
            "session_date": state["session_date"],
            "initial_action": state["next_executable_action"],
            "initial_blocking_stage": state["blocking_stage"],
            "actions": [], "planned_actions": [], "status": ITEM_SKIPPED,
            "error_code": None, "error_message": None, "retryable": False,
            "graph_calls": 0, "provider_calls": 0} for state in states}

        status = RUN_COMPLETED
        try:
            self._passes(connection, window, states, outcomes, summary,
                         dry_run=dry_run, lecture_ids=lecture_ids)
        except _RunStopped as stop:
            status = stop.status
            summary["errors"].append({"error_code": stop.error_code,
                                      "error": stop.message,
                                      "stopped_run": True})

        # -- 7. reconcile the day again --------------------------------------
        final = self._resolve_window(connection, window, lecture_ids)
        summary["final"] = _snapshot(final)
        by_id = {state["lecture_id"]: state for state in final}
        for lecture_id, outcome in outcomes.items():
            state = by_id.get(lecture_id)
            if state is None:
                continue
            outcome["final_action"] = state["next_executable_action"]
            outcome["final_blocking_stage"] = state["blocking_stage"]
            outcome["changed"] = (outcome["final_action"]
                                  != outcome["initial_action"])
            outcome["status"] = _item_status(outcome, state)
        summary["lectures"] = [outcomes[state["lecture_id"]] for state in final
                               if state["lecture_id"] in outcomes]

        counts = _counts(summary["lectures"])
        summary["counts"] = counts
        if status == RUN_COMPLETED and counts["failed_count"]:
            status = RUN_COMPLETED_WITH_FAILURES
        summary["status"] = status
        summary["idempotent"] = not any(item.get("changed")
                                        for item in summary["lectures"])
        summary["duration_ms"] = round((time.monotonic() - started) * 1000)

        # -- 8. persist the run summary --------------------------------------
        if run_id is not None:
            for item in summary["lectures"]:
                self.run_repository.record_item(
                    connection, run_id, lecture_id=item["lecture_id"],
                    initial_state=item["initial_action"] or NOTHING_TO_DO,
                    action=",".join(item["actions"]) or NOTHING_TO_DO,
                    final_state=item.get("final_action"), status=item["status"],
                    error_code=item.get("error_code"),
                    error_message=item.get("error_message"),
                    retryable=item.get("retryable", False),
                    graph_calls=item["graph_calls"],
                    provider_calls=item["provider_calls"],
                    metadata={"blocking_stage": item.get("final_blocking_stage"),
                              "changed": item.get("changed", False)})
            self.run_repository.finish(
                connection, run_id, status=status, counts=counts,
                graph_calls=summary["graph_calls"],
                provider_calls=summary["provider_calls"],
                legacy_rows_written=summary["legacy_rows_written"],
                metadata={"passes": summary["passes"],
                          "idempotent": summary["idempotent"],
                          "day_stages": summary["day_stages"]})
        return summary

    # -- the pass loop ------------------------------------------------------

    def _passes(self, connection, window, states, outcomes, summary, *, dry_run,
                lecture_ids=None):
        # The memo is RUN-scoped, not pass-scoped. A day-scoped service that
        # did not unblock a lecture will not unblock it by being run a second
        # time in the same cycle - the inputs have not changed - and a
        # per-pass memo would let one transcript acquisition become eight
        # Graph sweeps in a single run.
        executed_days = {}
        # (lecture, action) pairs that ran cleanly and moved nothing. The
        # commonest one is real and correct: a lecture waiting on attendance is
        # handed the recovery primitive, the recovery reads the still-empty
        # source and writes nothing. That is the right answer, and asking again
        # in the same cycle cannot produce a different one.
        stalled = set()
        previous = _actions_of(states)
        for pass_number in range(1, self.max_passes + 1):
            summary["passes"] = pass_number
            attempted = False
            for state in states:
                if self._advance(connection, state, outcomes, summary,
                                 executed_days, stalled, dry_run=dry_run):
                    attempted = True
            if not attempted:
                return
            states = self._resolve_window(connection, window, lecture_ids)
            current = _actions_of(states)
            for lecture_id, action in previous.items():
                if current.get(lecture_id) == action:
                    stalled.add((lecture_id, action))
            if current == previous:
                # Nothing anywhere moved. Whatever ran was a no-op, which is a
                # legitimate outcome and not a reason to try it again.
                return
            previous = current
        # The cap is a defect detector, not a budget: reaching it means a stage
        # kept changing the state without ever settling.
        summary["errors"].append({
            "error_code": "MAX_PASSES_REACHED",
            "error": f"the window did not settle within {self.max_passes} passes"})

    def _advance(self, connection, state, outcomes, summary, executed_days,
                 stalled, *, dry_run) -> bool:
        lecture_id = state["lecture_id"]
        outcome = outcomes.setdefault(lecture_id, {
            "lecture_id": lecture_id, "subject": state["subject"],
            "session_date": state["session_date"],
            "initial_action": state["next_executable_action"],
            "initial_blocking_stage": state["blocking_stage"],
            "actions": [], "planned_actions": [], "status": ITEM_SKIPPED,
            "error_code": None, "error_message": None, "retryable": False,
            "graph_calls": 0, "provider_calls": 0})
        action = state["next_executable_action"]

        # A lecture waiting on the attendance source is the ONLY wait the
        # orchestrator acts on, and the action it takes is the free one:
        # `recover-attendance` probes the source read-only and returns without
        # writing if it is still empty. No QA, no provider, no Graph.
        if action == WAIT_FOR_ATTENDANCE_SOURCE and state["stages"][
                "ATTENDANCE"]["state"] == "WAITING":
            action = RECOVER_ATTENDANCE

        if action == NOTHING_TO_DO and state.get("operator_actions"):
            outcome["status"] = ITEM_REVIEW
            outcome["error_code"] = "OPERATOR_ACTION_REQUIRED"
            outcome["operator_actions"] = [item["action"]
                                           for item in state["operator_actions"]]
            return False
        if action in IDLE_ACTIONS or action == NOTHING_TO_DO:
            return False
        if (lecture_id, state["next_executable_action"]) in stalled:
            # Already attempted this cycle and it changed nothing.
            return False
        if action == MANUAL_REVIEW_REQUIRED:
            outcome["status"] = ITEM_REVIEW
            outcome["error_code"] = state["stages"].get(
                state["blocking_stage"], {}).get("reason") or "MANUAL_REVIEW_REQUIRED"
            return False
        if action not in AUTOMATABLE_ACTIONS or self.runner is None or not (
                self.runner.can_run(action)):
            outcome["status"] = ITEM_SKIPPED
            outcome["error_code"] = (self.runner.refusal_reason(action)
                                     if self.runner is not None
                                     else "NO_RUNNER_CONFIGURED")
            return False
        if dry_run:
            # A dry run reports the decision and stops there. The action goes
            # into `planned_actions`, never into `actions`: a run that says it
            # DID something it only considered is a run whose audit trail
            # cannot be trusted, and the dry run's entire value is being
            # trusted about what it would have done.
            if action not in outcome["planned_actions"]:
                outcome["planned_actions"].append(action)
            projection = _projection(action)
            for key, value in projection.items():
                outcome[key] = outcome.get(key, False) or value
                summary[key] = summary.get(key, False) or value
            outcome["status"] = ITEM_SKIPPED
            outcome["error_code"] = "DRY_RUN"
            return False

        session_date = _date.fromisoformat(state["session_date"])
        scope = self.runner.scope_of(action)
        if scope == "DAY":
            key = (state["session_date"], action)
            if key in executed_days:
                # Already run once this cycle for this day. Sharing it is the
                # point: these services are day-scoped by construction.
                outcome["actions"].append(f"{action}(shared)")
                return False
            executed_days[key] = True
            summary["day_stages"].append({"session_date": state["session_date"],
                                          "action": action})
            return self._execute(connection, action, state, outcome, summary,
                                 session_date, locked=False)
        return self._execute(connection, action, state, outcome, summary,
                             session_date, locked=True)

    def _execute(self, connection, action, state, outcome, summary, session_date,
                 *, locked) -> bool:
        lecture_id = state["lecture_id"]
        started = time.monotonic()
        try:
            if locked:
                with self.lock_manager.hold(connection, lecture_id):
                    result = self.runner.execute(
                        connection, action, session_date=session_date,
                        lecture_id=lecture_id)
            else:
                result = self.runner.execute(connection, action,
                                             session_date=session_date,
                                             lecture_id=lecture_id)
        except LectureBusy:
            # Another process owns this lecture right now. Not a failure, and
            # not something to wait for: the other cycle will finish it.
            outcome["status"] = ITEM_LOCKED
            outcome["error_code"] = "LECTURE_LOCKED"
            outcome["retryable"] = True
            return False
        except ManualReviewRequired as exc:
            # Not a failure: the writer reached a decision this platform will
            # not take unattended, and nothing was half done. The reason is
            # the writer's own word, recorded for an operator.
            outcome["status"] = ITEM_REVIEW
            outcome["error_code"] = str(exc)
            outcome["retryable"] = False
            return False
        except PlatformError as exc:
            self._record_failure(outcome, exc.code, str(exc))
            if exc.code in WRITER_INTEGRITY_CODES:
                raise _RunStopped(RUN_BLOCKED_WRITER_INTEGRITY, exc.code,
                                  str(exc)) from exc
            if exc.code in SHARED_INFRASTRUCTURE_CODES:
                raise _RunStopped(RUN_ABORTED, exc.code, str(exc)) from exc
            return False
        except Exception as exc:  # noqa: BLE001 - one lecture must not stop the rest
            self._record_failure(outcome, type(exc).__name__, str(exc))
            return False

        outcome["actions"].append(action)
        outcome["graph_calls"] += result.get("graph_calls", 0)
        outcome["provider_calls"] += result.get("provider_calls", 0)
        outcome["duration_ms"] = round((time.monotonic() - started) * 1000)
        summary["graph_calls"] += result.get("graph_calls", 0)
        summary["provider_calls"] += result.get("provider_calls", 0)
        summary["legacy_rows_written"] += result.get("legacy_rows_written", 0)
        return True

    def _record_failure(self, outcome, code, message) -> None:
        outcome["status"] = ITEM_FAILED
        outcome["error_code"] = code
        outcome["error_message"] = message
        # Retryable means "resuming from the same stage next cycle is a
        # sensible thing to do", not "this will succeed".
        outcome["retryable"] = code not in WRITER_INTEGRITY_CODES

    # -- helpers ------------------------------------------------------------

    def _resolve_window(self, connection, window, lecture_ids=None) -> list[dict]:
        states = []
        for day in window:
            states.extend(self.resolver.for_day(connection, day))
        if lecture_ids is None:
            return states
        wanted = {str(item) for item in lecture_ids}
        return [state for state in states if state["lecture_id"] in wanted]

    def _discover(self, connection, aptem_connection, aptem_connection_factory,
                  window, summary) -> list[dict]:
        """
        Discovery is the only stage that reads the separate Aptem database.

        Prefer the factory, so that connection is opened here and closed the
        moment discovery finishes. Holding it for the whole cycle means holding
        it through transcript acquisition, parsing and QA - minutes of not
        using it - and a remote server is entitled to close an idle connection
        in that time. The first real scheduler cycle is how we found that out.
        """
        found = []
        try:
            if aptem_connection_factory is not None:
                with aptem_connection_factory() as aptem:
                    return self._discover_with(connection, aptem, window, summary)
            return self._discover_with(connection, aptem_connection, window, summary)
        except PlatformError as exc:
            # Could not even open the Aptem source. Same reasoning: the
            # registry we already hold is still worth reconciling.
            self.log.warning("discovery unavailable: %s", exc.code)
            summary["errors"].append({
                "error_code": exc.code, "stage": "DISCOVERY", "stopped_run": False,
                "error": "discovery unavailable; continuing with known lectures"})
            return [{"discovery_status": "UNAVAILABLE", "error_code": exc.code}]

    def _discover_with(self, connection, aptem, window, summary) -> list[dict]:
        """
        Discover each day, and treat a failure as a failure to DISCOVER rather
        than a failure of the cycle.

        Discovery answers "are there lectures we have not seen?". A Graph
        timeout says nothing about the sixteen lectures already in the
        registry, and abandoning them because a calendar query was slow would
        make every transient network blip cost a whole night's processing.
        The undiscovered day is picked up by the next cycle - which is what the
        lookback window exists for.
        """
        found = []
        for day in window:
            try:
                outcome = self.discovery_service.discover_day(
                    connection, aptem, day, persist=True)
            except PlatformError as exc:
                self.log.warning("discovery failed for %s: %s", day, exc.code)
                summary["errors"].append({
                    "error_code": exc.code, "session_date": day.isoformat(),
                    "stage": "DISCOVERY", "stopped_run": False,
                    "error": "discovery failed; continuing with known lectures"})
                found.append({"session_date": day.isoformat(),
                              "discovery_status": "FAILED",
                              "error_code": exc.code})
                continue
            # One calendar sweep per day, plus one onlineMeetings resolution
            # per candidate. Discovery publishes the second but not the first.
            summary["graph_calls"] += 1 + (
                outcome.get("meeting_lookups_attempted") or 0)
            found.append({
                "session_date": day.isoformat(),
                "discovery_status": "COMPLETED",
                "canonical_lecture_candidates": outcome.get(
                    "canonical_lecture_candidates", 0),
                "registry_rows_created": outcome.get("registry_rows_created", 0),
                "registry_rows_updated": outcome.get("registry_rows_updated", 0),
                "downstream_ready_lectures": outcome.get(
                    "downstream_ready_lectures", 0)})
        return found

    def _blocked(self, connection, summary, run_id, window, started, *, status,
                 reason, lecture_ids=None) -> dict:
        states = self._resolve_window(connection, window, lecture_ids)
        summary["lectures"] = [{
            "lecture_id": state["lecture_id"], "subject": state["subject"],
            "session_date": state["session_date"],
            "initial_action": state["next_executable_action"],
            "final_action": state["next_executable_action"],
            "actions": [], "status": ITEM_BLOCKED, "error_code": reason,
            "error_message": None, "retryable": True, "changed": False,
            "graph_calls": 0, "provider_calls": 0} for state in states]
        summary["initial"] = summary["final"] = _snapshot(states)
        counts = _counts(summary["lectures"])
        summary.update({"status": status, "counts": counts, "idempotent": True,
                        "duration_ms": round((time.monotonic() - started) * 1000)})
        if run_id is not None:
            for item in summary["lectures"]:
                self.run_repository.record_item(
                    connection, run_id, lecture_id=item["lecture_id"],
                    initial_state=item["initial_action"] or NOTHING_TO_DO,
                    action=NOTHING_TO_DO, final_state=item["final_action"],
                    status=ITEM_BLOCKED, error_code=reason, retryable=True)
            self.run_repository.finish(connection, run_id, status=status,
                                       counts=counts,
                                       metadata={"blocked_reason": reason})
        return summary


class _RunStopped(Exception):
    def __init__(self, status, error_code, message):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message


def _projection(action) -> dict:
    """
    What performing this action WOULD cost and touch.

    Derived from the action name alone, so the dry run and the real run cannot
    disagree about it: there is no second list of which stages are expensive.
    """
    from app.orchestration.runner import GRAPH_ACTIONS, PROVIDER_ACTIONS
    from app.orchestration.stages import SYNC_LEGACY_QA, SYNC_PERFECT
    return {
        "would_call_graph": action in GRAPH_ACTIONS,
        "would_call_provider": action in PROVIDER_ACTIONS,
        "would_write_coded_state": action in AUTOMATABLE_ACTIONS,
        "would_write_legacy_qa": action == SYNC_LEGACY_QA,
        "would_write_perfect": action == SYNC_PERFECT,
    }


def _snapshot(states) -> dict:
    return {"lectures": len(states),
            "by_action": _tally(state["next_executable_action"] for state in states),
            "by_blocking_stage": _tally(str(state["blocking_stage"])
                                        for state in states)}


def _tally(values) -> dict:
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _item_status(outcome, state) -> str:
    """
    What this RUN did to this lecture.

    Note this is a different question from where the lecture IS, which the day
    reconciliation answers with its own buckets. A lecture can be WAITING here
    and `complete` there, or SUCCEEDED here and `in_progress` there, and both
    statements are true.

    WAITING outranks SUCCEEDED deliberately. When the cycle hands a waiting
    lecture to the recovery primitive and the source is still empty, "we asked
    and it is still waiting" is the useful thing to record; "a call returned
    without raising" is not.
    """
    if outcome["status"] in (ITEM_FAILED, ITEM_BLOCKED, ITEM_LOCKED):
        return outcome["status"]
    if state["requires_review"]:
        return ITEM_REVIEW
    if state["next_executable_action"] == NOTHING_TO_DO:
        return ITEM_SUCCEEDED if outcome["actions"] else ITEM_SKIPPED
    if state["is_waiting"] or state["next_executable_action"] in IDLE_ACTIONS:
        return ITEM_WAITING
    if outcome["actions"]:
        return ITEM_SUCCEEDED
    if outcome["status"] == ITEM_REVIEW:
        return ITEM_REVIEW
    return ITEM_SKIPPED


def _actions_of(states) -> dict:
    return {state["lecture_id"]: state["next_executable_action"]
            for state in states}


def _counts(items) -> dict:
    """
    A PARTITION: every lecture lands in exactly one bucket and the buckets sum
    to `lectures_seen`.

    Overlapping counters are worse than no counters. "5 seen, 4 complete, 3
    waiting" is not a summary an operator can act on - it is a puzzle - and the
    reconciliation report is checked against these numbers, so they have to
    mean something arithmetically.
    """
    buckets = {"completed_count": 0, "waiting_count": 0, "review_count": 0,
               "failed_count": 0, "skipped_count": 0}
    for item in items:
        status = item["status"]
        if status == ITEM_FAILED:
            key = "failed_count"
        elif status == ITEM_REVIEW:
            key = "review_count"
        elif status == ITEM_WAITING:
            key = "waiting_count"
        elif item.get("final_action") == NOTHING_TO_DO:
            key = "completed_count"
        else:
            key = "skipped_count"
        buckets[key] += 1
    return {"lectures_seen": len(items), **buckets}
