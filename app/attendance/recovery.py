"""
Phase 3C3E: lecture-scoped attendance recovery.

THE PROBLEM THIS SOLVES
-----------------------
`public.kbc_attendance` is an external, read-only upstream source. On
2026-09-17 two of three real lectures had no rows in it at all. Phase 3C3D made
the platform say so honestly - `SOURCE_MISSING`, `PENDING_ATTENDANCE_DATA` -
but saying so is only half an answer. A lecture parked in a pending state needs
a deterministic way out for when the data finally lands.

This is that way out. It is a PRIMITIVE, not a scheduler: it runs for exactly
one lecture, only when asked, and the future reconciliation layer will call it
once per lecture rather than the other way round.

WHAT IT DOES NOT DO
-------------------
It never writes to `public.kbc_attendance`, never fabricates a row, never adds
a Graph attendance pipeline, and never infers attendance from LMS enrolment,
group membership or transcript speakers. If the source is still empty, the
honest answer is still `SOURCE_MISSING`, and the recovery says
`WAIT_FOR_ATTENDANCE_SOURCE` and writes nothing.

THE STAGE-AWARE PRINCIPLE
-------------------------
Attendance arriving invalidates attendance, and nothing upstream of it. So
recovery resumes at the earliest AFFECTED stage and no earlier:

    attendance -> engagement -> deterministic Item 7 -> (QA refresh)
               -> Perfect eligibility -> (Perfect sync)

Discovery, meeting resolution, transcript acquisition, selection, canonical
parsing and - above all - the Phase 3A model generation are NOT repeated. The
model was asked about a transcript, not about learners, so its answer is still
the answer. Attendance-only recovery costs zero provider calls, and a run that
would cost one is a defect, not an expense.

IMMUTABILITY
------------
Nothing is rewritten in place. Attendance snapshots are content-addressed on
the roster fingerprint, so a changed source produces a NEW snapshot and the old
`SOURCE_MISSING` one stays exactly where it is. The same is true of the
engagement row, the QA evaluation, the render and the Perfect result: each is
keyed on provenance that the new evidence changes. The pending history survives
the recovery that ends it.
"""
import logging
import time
from datetime import date

from app.attendance.coverage import (
    SOURCE_UNKNOWN,
    classify,
    is_authoritative,
    provenance as coverage_provenance,
)
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.qa.perfect import (
    ELIGIBLE,
    PENDING_ATTENDANCE_DATA,
)
from app.qa.service import (
    NO_REUSABLE_MODEL_OUTPUT,
    QA_DETERMINISTIC_REFRESHED,
)


# Machine-readable outcomes. Ordered roughly as a recovery progresses.
WAIT_FOR_ATTENDANCE_SOURCE = "WAIT_FOR_ATTENDANCE_SOURCE"
ATTENDANCE_SOURCE_RECOVERED = "ATTENDANCE_SOURCE_RECOVERED"
ATTENDANCE_SNAPSHOT_CREATED = "ATTENDANCE_SNAPSHOT_CREATED"
ENGAGEMENT_UPDATED = "ENGAGEMENT_UPDATED"
ENGAGEMENT_UNCHANGED = "ENGAGEMENT_UNCHANGED"
PERFECT_BECAME_ELIGIBLE = "PERFECT_BECAME_ELIGIBLE"
PERFECT_REMAINS_NOT_ELIGIBLE = "PERFECT_REMAINS_NOT_ELIGIBLE"
PERFECT_STILL_PENDING_ATTENDANCE_DATA = "PERFECT_STILL_PENDING_ATTENDANCE_DATA"
LEGACY_QA_UPDATE_REQUIRED = "LEGACY_QA_UPDATE_REQUIRED"
PERFECT_LEGACY_SYNC_AVAILABLE = "PERFECT_LEGACY_SYNC_AVAILABLE"
RENDER_REFRESHED = "RENDER_REFRESHED"
RENDER_REUSED = "RENDER_REUSED"
PHASE_3B_REFRESH_REQUIRED = "PHASE_3B_REFRESH_REQUIRED"
NO_CHANGE = "NO_CHANGE"
BLOCKED_NO_REUSABLE_MODEL_OUTPUT = "BLOCKED_NO_REUSABLE_MODEL_OUTPUT"


class AttendanceRecoveryError(RuntimeError):
    """The recovery cannot be performed as asked."""


class AttendanceRecoveryService:
    """
    Recover ONE lecture whose attendance source was missing.

    The writer is deliberately not wired in. Recovery produces the coded state
    and reports what a legacy write WOULD now be; performing that write stays
    an explicit, separately confirmed, ownership-aware operation through the
    existing guarded writer, exactly as every other production write does.
    """

    def __init__(self, *, resolution_service, engagement_service, qa_service,
                 coverage_repository, perfect_planner=None,
                 payload_repository=None, renderer_version=None,
                 rendering_service_factory=None,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION):
        self.resolution_service = resolution_service
        self.engagement_service = engagement_service
        self.qa_service = qa_service
        self.coverage_repository = coverage_repository
        self.perfect_planner = perfect_planner
        self.payload_repository = payload_repository
        self.renderer_version = renderer_version
        # A factory rather than a service, because Phase 3B is scoped at
        # construction time and recovery must never render a sibling.
        self.rendering_service_factory = rendering_service_factory
        self.attendance_resolution_version = attendance_resolution_version
        self.log = logging.getLogger(__name__)

    # -- the entry point ----------------------------------------------------

    def recover(self, connection, lecture_id, *, persist: bool = True) -> dict:
        started = time.monotonic()
        lecture_id = str(lecture_id)
        before = self._coverage(connection, lecture_id)

        summary = {
            "lecture_id": lecture_id,
            "scope": "LECTURE", "scope_value": lecture_id, "lectures_in_scope": 1,
            "mode": "RECOVER" if persist else "DRY_RUN",
            "attendance_resolution_version": self.attendance_resolution_version,
            "attendance_coverage_before": before["attendance_coverage_status"],
            "attendance_snapshot_id_before": before.get("attendance_snapshot_id"),
            "attendance_authoritative_before": before["attendance_source_authoritative"],
            # The boundaries this operation must hold, asserted as data.
            "provider_calls": 0, "graph_calls": 0, "transcript_reads": 0,
            "attendance_source_rows_written": 0, "external_tables_written": 0,
            "stages": [],
            "results": [],
        }

        # -- stage 1: re-read the external source, WITHOUT writing ----------
        # A dry resolve first, so a still-missing source costs literally zero
        # writes rather than an idempotent no-op that still touches rows.
        probe = self.resolution_service.resolve_lecture(
            connection, lecture_id, persist=False)
        observed = probe["lectures"][0]
        after_status = classify(
            source_row_count=observed["attendance_source_row_count"],
            present_row_count=observed["attendance_present_row_count"],
            effective_member_count=observed["effective_attendance_count"],
            source_rows_any_status=observed.get("attendance_source_rows_any_status"))
        new_snapshot_id = observed["attendance_snapshot_id"]
        snapshot_changed = str(new_snapshot_id) != str(
            before.get("attendance_snapshot_id") or "")

        summary.update({
            "attendance_coverage_after": after_status,
            "attendance_authoritative_after": is_authoritative(after_status),
            "attendance_snapshot_id_after": str(new_snapshot_id),
            "attendance_snapshot_changed": snapshot_changed,
            "attendance_source_row_count": observed["attendance_source_row_count"],
            "attendance_present_row_count": observed["attendance_present_row_count"],
            "attendance_effective_member_count": observed["effective_attendance_count"],
            "attendance_source_fingerprint_prefix": observed["source_fingerprint_prefix"],
            "attendance_source_rows_any_status": observed.get(
                "attendance_source_rows_any_status"),
        })
        summary["stages"].append({"stage": "ATTENDANCE_SOURCE_RECHECK",
                                  "source": "public.kbc_attendance",
                                  "read_only": True, "rows_written": 0})

        # The PERSISTED coverage can legitimately differ from what the source
        # says today - a snapshot frozen before Phase 3C3E carries no
        # unfiltered row count, so an empty roster reads there as
        # SOURCE_MISSING while the live probe can now see it as
        # "only absences". That is a difference in what is OBSERVABLE, not a
        # change in the source, and it must not be mistaken for one: the
        # snapshot is frozen evidence of what was seen then, and re-stamping it
        # from today's source would be a different fact wearing an old date.
        summary["attendance_coverage_persisted"] = before["attendance_coverage_status"]
        summary["attendance_coverage_observed_now"] = after_status
        summary["coverage_observation_improved"] = (
            before["attendance_coverage_status"] != after_status
            and not snapshot_changed)

        if not snapshot_changed:
            # Nothing about the source has moved. Do not touch a single row.
            summary["status"] = (NO_CHANGE if is_authoritative(after_status)
                                 else WAIT_FOR_ATTENDANCE_SOURCE)
            summary["results"] = [summary["status"]]
            summary["next_action"] = (None if is_authoritative(after_status)
                                      else WAIT_FOR_ATTENDANCE_SOURCE)
            summary["writes"] = _zero_writes()
            return _finish(summary, started)

        summary["results"].append(ATTENDANCE_SOURCE_RECOVERED)

        if not persist:
            summary["status"] = ATTENDANCE_SOURCE_RECOVERED
            summary["next_action"] = "RECOVER_ATTENDANCE"
            summary["writes"] = _zero_writes()
            return _finish(summary, started)

        # -- stage 2: freeze the new snapshot -------------------------------
        summary["session_date"] = probe["target_date"]
        resolved = self.resolution_service.resolve_lecture(
            connection, lecture_id, persist=True)
        snapshots_created = resolved["snapshots_created"]
        summary["stages"].append({
            "stage": "ATTENDANCE_SNAPSHOT",
            "snapshots_created": snapshots_created,
            "snapshots_reused": resolved["snapshots_reused"],
            "resolutions_created": resolved["resolutions_created"],
            "roles_created": resolved["roles_created"],
            "graph_calls": resolved["graph_calls"], "ai_calls": resolved["ai_calls"]})
        if snapshots_created:
            summary["results"].append(ATTENDANCE_SNAPSHOT_CREATED)

        # -- stage 3: lecture-scoped engagement ------------------------------
        engagement = self.engagement_service.calculate_lecture(
            connection, lecture_id, persist=True)
        if engagement["scope"] != "LECTURE" or engagement["lectures_in_scope"] != 1:
            raise AttendanceRecoveryError("engagement widened beyond the lecture")
        # The lecture now has more than one snapshot, so `calculate_lecture`
        # returns a row per snapshot - history included. The recovery is about
        # the NEW one; picking `lectures[0]` would report the superseded
        # answer and quietly undo the whole point of the operation.
        rows = [item for item in engagement["lectures"]
                if str(item["attendance_snapshot_id"]) == str(new_snapshot_id)]
        if len(rows) != 1:
            raise AttendanceRecoveryError(
                f"expected exactly one engagement row for the recovered snapshot, "
                f"found {len(rows)}")
        row = rows[0]
        engagement_changed = bool(engagement["engagements_created"]
                                  or engagement["engagements_updated"])
        summary["stages"].append({
            "stage": "ENGAGEMENT", "scope": engagement["scope"],
            "lectures_in_scope": engagement["lectures_in_scope"],
            "created": engagement["engagements_created"],
            "updated": engagement["engagements_updated"],
            "graph_calls": engagement["graph_calls"],
            "ai_calls": engagement["ai_calls"]})
        summary.update({
            "engagement_id": row["engagement_id"],
            "calculation_status": row["calculation_status"],
            "engagement_status_detail": row["engagement_status_detail"],
            "attended_count": row["attended_count"], "spoke_count": row["spoke_count"],
            "engagement_score": row["engagement_score"],
            "item7_override_applied": row["item7_override_applied"],
            "learner_engagement_status": row["learner_engagement_status"],
            "attendance_zero_confirmed": row["attendance_zero_confirmed"]})
        summary["results"].append(ENGAGEMENT_UPDATED if engagement_changed
                                  else ENGAGEMENT_UNCHANGED)

        # -- stage 4: deterministic QA refresh, reusing the frozen answer ----
        refresh = self.qa_service.refresh_deterministic(connection, lecture_id,
                                                        persist=True)
        summary["stages"].append({
            "stage": "QA_DETERMINISTIC", "refresh_status": refresh["refresh_status"],
            "provider_calls": refresh["provider_calls"],
            "openai_reused": refresh["openai_reused"]})
        summary["qa"] = {key: refresh.get(key) for key in (
            "refresh_status", "qa_status", "evaluation_id", "origin_evaluation_id",
            "checklist_changed", "previous_final_item7_status", "final_item7_status",
            "met_count", "partial_count", "not_met_count",
            "source_fingerprint_prefix", "model_input_fingerprint_prefix",
            "deterministic_refresh_version")}
        if refresh["refresh_status"] == NO_REUSABLE_MODEL_OUTPUT:
            # Fail closed. A missing reusable answer must never become a
            # silent, unbudgeted provider call.
            summary["status"] = BLOCKED_NO_REUSABLE_MODEL_OUTPUT
            summary["results"].append(BLOCKED_NO_REUSABLE_MODEL_OUTPUT)
            summary["next_action"] = "RUN_PHASE_3A"
            summary["writes"] = _writes(summary)
            return _finish(summary, started)
        summary["results"].append(refresh["refresh_status"])
        if refresh["refresh_status"] == QA_DETERMINISTIC_REFRESHED and refresh[
                "checklist_changed"]:
            summary["results"].append(LEGACY_QA_UPDATE_REQUIRED)

        # -- stage 5: Phase 3B, only if the deterministic result moved -------
        summary["render"] = self._refresh_render(connection, lecture_id, refresh,
                                                 summary)

        # -- stage 6: Perfect eligibility under the attendance-aware policy --
        summary["perfect"] = self._reevaluate_perfect(
            connection, lecture_id, refresh.get("evaluation_id"), summary)

        summary["status"] = summary["results"][-1] if summary["results"] else NO_CHANGE
        summary["writes"] = _writes(summary)
        summary["next_action"] = self._next_action(summary)
        return _finish(summary, started)

    # -- helpers ------------------------------------------------------------

    def _coverage(self, connection, lecture_id) -> dict:
        if self.coverage_repository is None:
            return coverage_provenance(status=SOURCE_UNKNOWN)
        return self.coverage_repository.for_lecture(
            connection, lecture_id,
            attendance_resolution_version=self.attendance_resolution_version)

    def _refresh_render(self, connection, lecture_id, refresh, summary) -> dict | None:
        """
        Re-render ONLY when the deterministic result actually moved.

        A refresh that changed nothing already has a valid render, and
        producing a second identical one would add a version that means
        nothing. No provider call and no Graph call either way.
        """
        if self.payload_repository is None:
            return None
        evaluation_id = refresh.get("evaluation_id")
        existing = [item for item in self.payload_repository.load_sessions_for_lecture(
            connection, lecture_id, self.renderer_version)
            if str(item["evaluation_id"]) == str(evaluation_id)
            and item["render_status"] == "RENDERED"]
        if existing:
            summary["results"].append(RENDER_REUSED)
            return {"status": RENDER_REUSED,
                    "rendered_session_id": str(existing[0]["rendered_session_id"]),
                    "provider_calls": 0, "graph_calls": 0}
        if self.rendering_service_factory is None:
            # Honest about the gap rather than silently leaving a stale render
            # attached to a superseded evaluation.
            summary["results"].append(PHASE_3B_REFRESH_REQUIRED)
            return {"status": PHASE_3B_REFRESH_REQUIRED,
                    "reason": "no rendering service supplied to this recovery"}

        service = self.rendering_service_factory(lecture_id)
        outcome = service.render_day(
            connection, date.fromisoformat(summary["session_date"]))
        # `lectures_considered` counts EVALUATIONS, and this lecture now has
        # several - the superseded ones and the refreshed one. The scope
        # invariant is therefore about lecture ids, not row counts.
        touched = {str(item["lecture_id"]) for item in outcome["lectures"]}
        if touched != {str(lecture_id)}:
            raise AttendanceRecoveryError(
                f"render widened beyond the lecture: {sorted(touched)}")
        summary["results"].append(RENDER_REFRESHED)
        summary["stages"].append({
            "stage": "PHASE_3B", "sessions_rendered": outcome["sessions_rendered"],
            "sessions_reused": outcome["sessions_reused"],
            "lms_snapshots_created": outcome["lms_snapshots_created"],
            "provider_calls": outcome["provider_calls"]})
        return {"status": RENDER_REFRESHED,
                "sessions_rendered": outcome["sessions_rendered"],
                "lms_snapshots_created": outcome["lms_snapshots_created"],
                "provider_calls": outcome["provider_calls"]}

    def _reevaluate_perfect(self, connection, lecture_id, evaluation_id,
                            summary) -> dict | None:
        """
        Re-derive Perfect eligibility from the refreshed deterministic result.

        Only the coded answer is persisted here. Whether the legacy Perfect row
        may now be created is reported, never performed: that write is
        ownership-aware, mode-gated and separately confirmed, and recovery is
        not a licence to bypass it.
        """
        if self.perfect_planner is None or self.payload_repository is None:
            return None
        # The render must belong to the REFRESHED evaluation. Judging a Perfect
        # Lecture from a render of the superseded one would decide today's
        # question with yesterday's checklist.
        rendered = [item for item in self.payload_repository.load_sessions_for_lecture(
            connection, lecture_id, self.renderer_version)
            if item["render_status"] == "RENDERED"
            and str(item["evaluation_id"]) == str(evaluation_id)]
        if not rendered:
            if PHASE_3B_REFRESH_REQUIRED not in summary["results"]:
                summary["results"].append(PHASE_3B_REFRESH_REQUIRED)
            return {"perfect_decision": None, "render_status": "STALE_OR_MISSING",
                    "note": "the refreshed evaluation has no render of its own yet"}
        payload = rendered[0]
        items = self.payload_repository.load_items(connection,
                                                   payload["rendered_session_id"])
        plan = self.perfect_planner.plan_one(connection, payload, items)
        reason = plan["eligibility_reason"]
        if reason == PENDING_ATTENDANCE_DATA:
            summary["results"].append(PERFECT_STILL_PENDING_ATTENDANCE_DATA)
        elif reason == ELIGIBLE:
            summary["results"].append(PERFECT_BECAME_ELIGIBLE)
            if not plan["legacy_row_exists"]:
                summary["results"].append(PERFECT_LEGACY_SYNC_AVAILABLE)
        else:
            summary["results"].append(PERFECT_REMAINS_NOT_ELIGIBLE)
        return {key: plan.get(key) for key in (
            "eligibility_version", "eligibility_reason", "is_perfect",
            "base_eligibility_reason", "attendance_pending",
            "attendance_coverage_status", "perfect_decision",
            "legacy_row_exists", "coded_owned", "result_id")}

    def _next_action(self, summary) -> str | None:
        """The earliest stage that is still missing or stale."""
        if not summary["attendance_authoritative_after"]:
            return WAIT_FOR_ATTENDANCE_SOURCE
        if LEGACY_QA_UPDATE_REQUIRED in summary["results"]:
            return "UPDATE_LEGACY_QA"
        if PERFECT_LEGACY_SYNC_AVAILABLE in summary["results"]:
            return "SYNC_PERFECT"
        if PHASE_3B_REFRESH_REQUIRED in summary["results"]:
            return "REFRESH_RENDER"
        return None


def _zero_writes() -> dict:
    return {"attendance_snapshots_created": 0, "engagement_rows_written": 0,
            "qa_evaluations_created": 0, "renders_created": 0,
            "legacy_qa_rows_written": 0, "perfect_legacy_rows_written": 0,
            "perfect_ownership_rows_written": 0}


def _writes(summary) -> dict:
    stages = {item["stage"]: item for item in summary["stages"]}
    attendance = stages.get("ATTENDANCE_SNAPSHOT", {})
    engagement = stages.get("ENGAGEMENT", {})
    return {**_zero_writes(),
            "attendance_snapshots_created": attendance.get("snapshots_created", 0),
            "engagement_rows_written": (engagement.get("created", 0)
                                        + engagement.get("updated", 0)),
            "qa_evaluations_created": int(bool(
                (summary.get("qa") or {}).get("evaluation_id")
                and (summary.get("qa") or {}).get("refresh_status")
                == QA_DETERMINISTIC_REFRESHED))}


def _finish(summary, started) -> dict:
    summary["duration_ms"] = round((time.monotonic() - started) * 1000)
    # Restated at the end so the boundaries are visible in every result, not
    # only the ones that happened to take the long path.
    summary.setdefault("writes", _zero_writes())
    summary["openai_calls"] = 0
    summary["graph_calls"] = 0
    summary["external_attendance_rows_written"] = 0
    return summary
