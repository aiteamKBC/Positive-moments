"""
Phase 4A Part S: the Operations backend interface.

No frontend. This is the service layer a future Operations UI will call, and it
exists now so that the UI is built against a stable contract rather than
against whatever SQL happens to be convenient later.

THE SHAPE OF IT
---------------
Every read is a method that returns plain JSON-serialisable data. Every
mutation is separate, explicitly named, and guarded - there is no generic
`execute(action)` entry point, because a UI that can post an arbitrary action
name is a UI that can force-reprocess a settled lecture by typo.

RETRY vs FORCE REPROCESS
------------------------
The distinction is exposed as data, not as a parameter. `retry_eligibility`
answers "may this lecture resume from its earliest incomplete stage?" and is
usually yes. `force_reprocess_eligibility` answers "may an operator
deliberately create new provenance here?" and carries the reasons it is
refused. Nothing in the automated path can reach the second one.
"""
from datetime import date as _date

from app.orchestration.reconciliation import DayReconciliation, bucket_for
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    COMPLETE,
    FORCE_REPROCESS,
    IDLE_ACTIONS,
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    ORCHESTRATION_VERSION,
    PRODUCTION_WRITE_ACTIONS,
    QA_EVALUATION,
    RECORDING_LINK,
    RETRY,
    STAGE_ORDER,
)
from app.recordings.models import (
    AMBIGUOUS_RECORDING_FILES,
    GRAPH_RECORDING_AMBIGUOUS,
    TIMESTAMP_MISMATCH,
)

# A recording outcome where more than one answer was possible (or none was
# exact) and the stage refused to pick one.
REFUSED_TO_GUESS_STATUSES = frozenset({
    AMBIGUOUS_RECORDING_FILES, GRAPH_RECORDING_AMBIGUOUS, TIMESTAMP_MISMATCH})


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


class OperationsService:
    """Read-mostly facade over orchestration state, runs and reconciliation."""

    def __init__(self, *, resolver, run_repository=None, reconciliation=None,
                 preflight=None, directory_repository=None,
                 media_repository=None, recording_links=None):
        self.resolver = resolver
        self.run_repository = run_repository
        self.reconciliation = reconciliation or DayReconciliation(resolver=resolver)
        self.preflight = preflight
        self.directory_repository = directory_repository
        self.media_repository = media_repository
        # The RECORDING_LINK stage's durable state. Read only in `write` mode,
        # exactly like the resolver: `observe` never touches that table.
        self.recording_links = recording_links

    # -- delivered media ----------------------------------------------------

    def lecture_media(self, connection, legacy_session_id: str) -> dict:
        """
        What media actually exists for one lecture.

        Every status here is worker or asset truth. A clip is 'ready' only when
        it was uploaded and carries a durable SharePoint URL - never because a
        plan or a job exists.
        """
        if self.media_repository is None or not legacy_session_id:
            return {"positive_moments": [], "lecture_parts": [],
                    "split_plan": None}
        return {
            "positive_moments": self.media_repository.moments(
                connection, legacy_session_id),
            "lecture_parts": self.media_repository.parts(
                connection, legacy_session_id),
            "split_plan": self.media_repository.split_plan(
                connection, legacy_session_id),
        }

    def media_jobs(self, connection, *, status=None, limit: int = 100) -> dict:
        if self.media_repository is None:
            return {"counts": {"by_status": {}, "by_type": {}}, "jobs": []}
        return {"counts": self.media_repository.job_counts(connection),
                "jobs": self.media_repository.jobs(connection, status=status,
                                                   limit=limit)}

    # -- descriptive registry metadata --------------------------------------

    def lecture_calendar(self, connection, start, end) -> list[dict]:
        """
        Which dates carry lectures. One cheap query, no stage resolution.

        The console uses it to answer "this day is empty - where is the last
        day that was not?" without guessing, and to keep a date picker honest
        about which days have anything on them.
        """
        if self.directory_repository is None:
            return []
        return self.directory_repository.calendar(connection, start, end)

    def lecture_directory(self, connection, start, end) -> list[dict]:
        """
        Trainer, module and scheduled window for every lecture in a range.

        Descriptive only. Nothing here says whether a lecture is complete -
        that is `day_reconciliation`'s answer and the UI joins the two on
        `lecture_id` rather than deriving a second one.
        """
        if self.directory_repository is None:
            return []
        return self.directory_repository.for_range(connection, start, end)

    # -- runs ---------------------------------------------------------------

    def recent_runs(self, connection, limit: int = 20) -> list[dict]:
        if self.run_repository is None:
            return []
        return self.run_repository.recent_runs(connection, limit)

    def run_detail(self, connection, run_id) -> dict:
        if self.run_repository is None:
            return {"run_id": str(run_id), "items": []}
        return {"run_id": str(run_id),
                "items": self.run_repository.items_for_run(connection, run_id)}

    def lecture_run_history(self, connection, lecture_id, limit: int = 20) -> list[dict]:
        if self.run_repository is None:
            return []
        return self.run_repository.history_for_lecture(connection, lecture_id, limit)

    # -- state --------------------------------------------------------------

    def day_reconciliation(self, connection, session_date: _date) -> dict:
        return self.reconciliation.for_day(connection, session_date)

    def lecture_stage_matrix(self, connection, lecture_id) -> dict:
        state = self.resolver.for_lecture(connection, lecture_id)
        return {
            "lecture_id": state["lecture_id"], "subject": state["subject"],
            "session_date": state["session_date"],
            "module": state.get("module"),
            "scheduled_start": state.get("scheduled_start"),
            "orchestration_version": ORCHESTRATION_VERSION,
            "stage_order": list(STAGE_ORDER),
            "stages": state["stages"],
            "next_action": state["next_action"],
            "next_executable_action": state["next_executable_action"],
            "blocking_stage": state["blocking_stage"],
            "versions": state["versions"],
            "retry_eligibility": retry_eligibility(state),
            "force_reprocess_eligibility": force_reprocess_eligibility(state),
            # Carried so the detail page can state attendance honestly and can
            # show a suppressed duplicate as what it is rather than as a
            # lecture whose fifteen stages all happen to be not-applicable.
            "attendance_coverage_status": state["attendance_coverage_status"],
            "attendance_source_authoritative":
                state["attendance_source_authoritative"],
            "is_suppressed_duplicate": bool(state.get("is_suppressed_duplicate")),
            "duplicate_resolution": state.get("duplicate_resolution"),
            "bucket": bucket_for(state),
            "recording_link": self.recording_link_detail(connection, state),
        }

    def recording_link_detail(self, connection, state) -> dict:
        """
        RECORDING_LINK for one lecture, in words an operator can act on.

        The stage state and action are the resolver's; the last evaluation is
        the stage's own durable row. `recording_available` is a yes/no - the
        URL itself is never returned here (the console links to it through the
        directory's existing "watch" behaviour), and neither is any Graph id,
        file name or payload.
        """
        stage = (state.get("stages") or {}).get(RECORDING_LINK) or {}
        mode = getattr(self.resolver, "recording_link_mode", "observe")
        detail = None
        if mode == "write" and self.recording_links is not None and not state.get(
                "is_suppressed_duplicate"):
            detail = self.recording_links.details(
                connection, [state["lecture_id"]]).get(str(state["lecture_id"]))
            if detail and detail.get("legacy_session_id") != stage.get("legacy_session_id"):
                detail = None
        status = (detail or {}).get("status")
        return {
            "recording_link_mode": mode,
            "state": stage.get("state"),
            "action": stage.get("action"),
            "reason": stage.get("reason"),
            "owner": stage.get("owner"),
            "recording_available": stage.get("state") == COMPLETE,
            "last_status": status,
            "last_reason": (detail or {}).get("reason"),
            "last_checked_at": _iso((detail or {}).get("last_attempted_at")),
            "first_checked_at": _iso((detail or {}).get("first_attempted_at")),
            "attempt_count": (detail or {}).get("attempt_count"),
            "next_attempt_after": _iso((detail or {}).get("next_attempt_after")),
            "written_by_platform": bool((detail or {}).get("recording_url_written")),
            "written_at": _iso((detail or {}).get("written_at")),
            "source": (detail or {}).get("source"),
            "timestamp_difference_seconds": (detail or {}).get("timestamp_difference_seconds"),
            "candidate_file_count": (detail or {}).get("candidate_file_count"),
            "exact_candidate_count": (detail or {}).get("exact_candidate_count"),
            # The stage declined to choose between candidates. Said as data so
            # the console can explain the refusal instead of implying a fault.
            "refused_to_guess": status in REFUSED_TO_GUESS_STATUSES,
        }

    def lecture_next_action(self, connection, lecture_id) -> dict:
        state = self.resolver.for_lecture(connection, lecture_id)
        return {"lecture_id": state["lecture_id"],
                "next_action": state["next_action"],
                "next_executable_action": state["next_executable_action"],
                "blocking_stage": state["blocking_stage"],
                "is_waiting": state["is_waiting"],
                "requires_review": state["requires_review"]}

    # -- the operator's working lists ---------------------------------------

    def pending_attendance(self, connection, session_date: _date) -> list[dict]:
        report = self.reconciliation.for_day(connection, session_date)
        return [row for row in report["lectures"]
                if not row["attendance_source_authoritative"]]

    def review_required(self, connection, session_date: _date) -> list[dict]:
        report = self.reconciliation.for_day(connection, session_date)
        return [row for row in report["lectures"]
                if row["bucket"] in ("review", "failed")]

    def error_reasons(self, connection, session_date: _date) -> list[dict]:
        report = self.reconciliation.for_day(connection, session_date)
        return [{"lecture_id": row["lecture_id"], "subject": row["subject"],
                 "blocking_stage": row["blocking_stage"],
                 "next_action": row["next_action"],
                 "reason_codes": row["reason_codes"]}
                for row in report["lectures"] if row["reason_codes"]]

    # -- guarded mutation surface -------------------------------------------

    def legacy_qa_precheck(self) -> dict:
        """Expose the read-only n8n safety answer to the UI without running a cycle."""
        if self.preflight is None:
            return {"status": "PRECHECK_NOT_CONFIGURED", "read_only": True,
                    "n8n_modified": False}
        return self.preflight.check()

    def request_retry(self, connection, lecture_id) -> dict:
        """
        Describe the retry an operator is asking for. It does NOT run it.

        Returning a plan rather than performing the work keeps the UI's power
        bounded to what the orchestrator would have done anyway - a retry is
        only ever "resume from the earliest incomplete stage", and this says
        which stage that is.
        """
        state = self.resolver.for_lecture(connection, lecture_id)
        eligibility = retry_eligibility(state)
        return {"lecture_id": state["lecture_id"], "semantics": RETRY,
                "resume_stage": state["blocking_stage"],
                "action": state["next_executable_action"],
                **eligibility}


def retry_eligibility(state) -> dict:
    """
    A retry resumes from the earliest incomplete stage using state that is
    already valid. It can never touch a COMPLETE stage, so it is refused only
    when there is nothing incomplete to resume from, or when the thing that is
    incomplete is not ours to fix.
    """
    action = state["next_executable_action"]
    stages = state["stages"]
    if action == NOTHING_TO_DO:
        return {"retry_eligible": False, "retry_reason": "NOTHING_TO_RETRY"}
    if action == MANUAL_REVIEW_REQUIRED:
        return {"retry_eligible": False,
                "retry_reason": stages.get(state["blocking_stage"], {}).get("reason")
                or "MANUAL_REVIEW_REQUIRED"}
    if action in IDLE_ACTIONS:
        return {"retry_eligible": False, "retry_reason": "WAITING_ON_EXTERNAL_SOURCE"}
    if action in PRODUCTION_WRITE_ACTIONS:
        return {"retry_eligible": True, "retry_reason": "OPERATOR_CONFIRMATION_REQUIRED",
                "automated": False}
    return {"retry_eligible": True, "retry_reason": None,
            "automated": action in AUTOMATABLE_ACTIONS}


def force_reprocess_eligibility(state) -> dict:
    """
    Force reprocess creates NEW provenance where the versioning rules allow it.

    It is refused whenever the thing an operator would be reaching for is not
    actually blocked - a settled lecture does not need new provenance, it needs
    to be left alone - and it is refused outright while the generation budget
    for the current contract is exhausted, because "force" must not become a
    way to buy a fourth attempt at a contract that already failed three times.
    """
    reasons = []
    evaluation = state["stages"].get(QA_EVALUATION, {})
    if state["next_executable_action"] == NOTHING_TO_DO:
        reasons.append("LECTURE_IS_SETTLED")
    if evaluation.get("attempts_remaining") == 0:
        reasons.append("GENERATION_BUDGET_EXHAUSTED_FOR_CURRENT_CONTRACT")
    if state["stages"].get("LEGACY_QA_SYNC", {}).get("coded_owned") is False and (
            state["stages"]["LEGACY_QA_SYNC"]["state"] != COMPLETE):
        reasons.append("LEGACY_ROW_NOT_CODED_OWNED")
    return {"semantics": FORCE_REPROCESS,
            "force_reprocess_eligible": not reasons,
            "refusal_reasons": reasons,
            # Never reachable from the scheduler. Stated as data so the UI can
            # show it rather than assume it.
            "available_to_scheduler": False,
            "requires_explicit_operator_action": True}
