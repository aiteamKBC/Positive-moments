"""
Phase 3C3D: the single read that answers "where is this lecture, and what
would resuming it cost?".

Before this, the answer lived in seven tables and two of them disagreed: the
evaluation's `provider_attempts` held the HTTP retry count of the last call
while `lecture_qa_generation_attempts` held the real generation history, so a
recovery layer reading the evaluation alone under-counted and would have
re-bought generations that the cap had already refused.

Everything here is READ-ONLY and derives from persisted evidence. No Graph
call, no provider call, no recomputation of a checklist, no attendance query.

Attempts are reported PER GENERATION CONTRACT, because that is what the cap is
keyed on. A lecture whose contract changed legitimately holds two independent
histories: three exhausted attempts under `json_object_v1` and a fresh budget
under `strict_json_schema_v1`. Collapsing them into one number would either
strand a recoverable lecture forever or silently reset a cap that was working.
"""
from app.attendance.coverage import (
    ATTENDANCE_COVERAGE_VERSION,
    ATTENDANCE_SOURCE_MISSING,
    classify,
    is_authoritative,
)
from app.common.errors import DATABASE_ERROR, PlatformError
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    PENDING_ATTENDANCE_DATA,
)
from app.qa.delivery import TRANSCRIPT_COVERAGE_INCOMPLETE
from app.qa.evidence_policy import EVIDENCE_POLICY_V1
from app.qa.generation_budget import (
    GENERATION_BUDGET_POLICY_VERSION,
    attempt_row,
    generation_budget,
)
from app.qa.service import MAX_MODEL_GENERATIONS


# Phase 3C3E. The next action Operations should take, named so the future
# reconciliation layer can branch on it without re-deriving the state model.
WAIT_FOR_ATTENDANCE_SOURCE = "WAIT_FOR_ATTENDANCE_SOURCE"
RECOVER_ATTENDANCE = "RECOVER_ATTENDANCE"
RECALCULATE_ENGAGEMENT = "RECALCULATE_ENGAGEMENT"
RUN_PHASE_3A = "RUN_PHASE_3A"
REFRESH_DETERMINISTIC_QA = "REFRESH_DETERMINISTIC_QA"
REFRESH_RENDER = "REFRESH_RENDER"
EVALUATE_PERFECT = "EVALUATE_PERFECT"
SYNC_PERFECT = "SYNC_PERFECT"
REVIEW_REQUIRED_MANUAL = "REVIEW_REQUIRED_MANUAL"
NOTHING_TO_DO = "NOTHING_TO_DO"


LECTURE = """
SELECT l.lecture_id, l.subject, l.session_date, l.is_cancelled, l.downstream_ready
  FROM public.lecture_sessions l WHERE l.lecture_id = %s
"""

COVERAGE = """
SELECT sn.snapshot_id, sn.source_row_count, sn.present_row_count,
       sn.effective_member_count, sn.attendance_resolution_version,
       (sn.metadata ->> 'source_rows_any_status')::int
  FROM public.lecture_attendance_snapshots sn
 WHERE sn.lecture_id = %s
 ORDER BY sn.created_at DESC, sn.snapshot_id DESC LIMIT 1
"""

ENGAGEMENT = """
SELECT m.engagement_id, m.calculation_status, m.attended_count, m.spoke_count,
       m.engagement_score, m.item7_override_applied,
       m.metadata ->> 'attendance_coverage_status',
       m.metadata ->> 'engagement_status_detail',
       m.attendance_snapshot_id,
       coalesce((m.metadata ->> 'attendance_zero_confirmed')::boolean, false),
       m.metadata ->> 'attendance_coverage_version'
  FROM public.lecture_engagement_metrics m
 WHERE m.lecture_id = %s
 ORDER BY m.updated_at DESC, m.engagement_id DESC LIMIT 1
"""

EVALUATIONS = """
SELECT e.evaluation_id, e.source_fingerprint, e.qa_status, e.review_reason,
       e.provider_attempts, e.ai_called, e.met_count, e.partial_count, e.not_met_count,
       e.metadata ->> 'provider_contract_version',
       e.metadata ->> 'punctuality_source_version',
       e.updated_at, e.engagement_id, e.attendance_snapshot_id,
       e.metadata ->> 'model_input_fingerprint',
       e.metadata #>> '{deterministic_refresh,deterministic_refresh_version}',
       e.document_id, e.metadata ->> 'evidence_policy_version'
  FROM public.lecture_qa_evaluations e
 WHERE e.lecture_id = %s
 ORDER BY e.updated_at DESC, e.evaluation_id DESC
"""

ATTEMPTS = """
SELECT a.source_fingerprint, count(*)::int, max(a.generation_number),
       min(a.created_at), max(a.created_at),
       array_agg(a.outcome ORDER BY a.generation_number),
       array_agg(coalesce(a.error_code, '') ORDER BY a.generation_number),
       array_agg(coalesce(a.metadata ->> 'consumes_generation_budget', '')
                 ORDER BY a.generation_number),
       array_agg(coalesce(a.metadata ->> 'generation_budget_used', '')
                 ORDER BY a.generation_number),
       (SELECT jsonb_build_object('error_code', q.error_code,
                                  'http_status', q.metadata ->> 'http_status',
                                  'has_model_output', q.ai_raw_output IS NOT NULL)
          FROM public.lecture_qa_evaluations AS q
         WHERE q.source_fingerprint = a.source_fingerprint)
  FROM public.lecture_qa_generation_attempts a
 WHERE a.lecture_id = %s
 GROUP BY a.source_fingerprint
"""

RENDERED = """
SELECT rs.rendered_session_id, rs.render_status, rs.renderer_version,
       rs.source_fingerprint, rs.met_count, rs.partial_count, rs.not_met_count,
       rs.evaluation_id, rs.session_id
  FROM public.lecture_qa_rendered_sessions rs
 WHERE rs.lecture_id = %s
 ORDER BY rs.updated_at DESC, rs.rendered_session_id DESC LIMIT 1
"""

QA_WRITES = """
SELECT w.write_id, w.write_status, w.writer_version, w.legacy_session_id
  FROM public.lecture_qa_legacy_writes w
 WHERE w.lecture_id = %s ORDER BY w.written_at DESC
"""

PERFECT_RESULTS = """
SELECT r.eligibility_version, r.is_perfect, r.reason,
       r.metadata ->> 'attendance_coverage_status',
       r.metadata ->> 'base_reason', r.computed_at
  FROM public.lecture_perfect_lecture_results r
 WHERE r.lecture_id = %s ORDER BY r.computed_at DESC
"""

PERFECT_WRITES = """
SELECT p.write_status, p.writer_version, p.eligibility_version, p.legacy_lecture_key
  FROM public.lecture_perfect_lecture_legacy_writes p
 WHERE p.lecture_id = %s ORDER BY p.written_at DESC
"""


def _rows(connection, sql, lecture_id):
    try:
        return connection.execute(sql, (lecture_id,)).fetchall()
    except Exception as exc:
        raise PlatformError(DATABASE_ERROR, "recovery state read failed") from exc


def recovery_state(connection, lecture_id, *,
                   max_model_generations: int = MAX_MODEL_GENERATIONS) -> dict:
    """Assemble every fact a resume decision needs, for ONE lecture."""
    lecture = _rows(connection, LECTURE, lecture_id)
    if not lecture:
        raise PlatformError(DATABASE_ERROR, f"unknown lecture {lecture_id}")
    lecture = lecture[0]

    coverage_row = _rows(connection, COVERAGE, lecture_id)
    if coverage_row:
        row = coverage_row[0]
        coverage = {
            "attendance_coverage_status": classify(
                source_row_count=row[1], present_row_count=row[2],
                effective_member_count=row[3], source_rows_any_status=row[5]),
            "attendance_source_row_count": row[1],
            "attendance_present_row_count": row[2],
            "attendance_effective_member_count": row[3],
            "attendance_source_rows_any_status": row[5],
            "attendance_resolution_version": row[4],
            "attendance_snapshot_id": str(row[0]),
        }
    else:
        coverage = {"attendance_coverage_status": "SOURCE_UNKNOWN",
                    "attendance_snapshot_id": None}
    coverage["attendance_source_authoritative"] = is_authoritative(
        coverage["attendance_coverage_status"])

    engagement_row = _rows(connection, ENGAGEMENT, lecture_id)
    engagement = None
    if engagement_row:
        row = engagement_row[0]
        engagement = {"engagement_id": str(row[0]), "calculation_status": row[1],
                      "attended_count": row[2], "spoke_count": row[3],
                      "engagement_score": row[4], "item7_override_applied": row[5],
                      "attendance_coverage_status": row[6],
                      "engagement_status_detail": row[7] or row[1],
                      "attendance_snapshot_id": str(row[8]),
                      # Never inferred from the count, only from the source.
                      "attendance_zero_confirmed": row[9],
                      "attendance_coverage_version": row[10],
                      # Phase 3C3E: is the engagement row calculated from the
                      # CURRENT attendance snapshot, or from a superseded one?
                      # That single question is what makes recovery stage-aware.
                      "is_current_snapshot": str(row[8]) == str(
                          coverage.get("attendance_snapshot_id"))}

    # Attempts are authoritative and are keyed on the generation contract's
    # fingerprint, so they are indexed that way rather than summed.
    # The BUDGET is the shared policy's answer, not the row count: a call the
    # provider refused on the credential is on record but bought nothing.
    attempts_by_fingerprint = {}
    for row in _rows(connection, ATTEMPTS, lecture_id):
        budget = generation_budget(
            [attempt_row(*values) for values in zip(row[5], row[6], row[7], row[8])],
            row[9], max_generations=max_model_generations)
        attempts_by_fingerprint[row[0]] = {
            "attempt_records": row[1],
            "attempts_used": budget["generation_budget_used"],
            "configuration_failures": budget["configuration_failures"],
            "last_attempt_configuration_failure":
                budget["last_attempt_configuration_failure"],
            "legacy_configuration_failures_inferred":
                budget["legacy_configuration_failures_inferred"],
            "highest_generation_number": row[2],
            "first_attempt_at": str(row[3]), "last_attempt_at": str(row[4]),
            "outcomes": list(row[5]),
            "error_codes": [code for code in row[6] if code]}

    evaluations = []
    for row in _rows(connection, EVALUATIONS, lecture_id):
        fingerprint = row[1]
        attempts = attempts_by_fingerprint.get(fingerprint, {})
        used = attempts.get("attempts_used", 0)
        records = attempts.get("attempt_records", 0)
        evaluations.append({
            "evaluation_id": str(row[0]),
            "source_fingerprint": fingerprint,
            "source_fingerprint_prefix": fingerprint[:16],
            "qa_status": row[2], "review_reason": row[3],
            "ai_called": row[5],
            "met_count": row[6], "partial_count": row[7], "not_met_count": row[8],
            "provider_contract_version": row[9] or "json_object_v1",
            "punctuality_source_version": row[10],
            "updated_at": str(row[11]),
            "engagement_id": str(row[12]),
            "attendance_snapshot_id": str(row[13]),
            # Phase 3C3E: whether this answer can be reused without paying the
            # provider again, and whether it was itself produced that way.
            "model_output_reusable": row[14] is not None,
            "deterministic_refresh_version": row[15],
            # Which canonical document this answer was built from. A lecture
            # can legitimately hold two (Phase 3C2.3B rebuilt one under seam
            # dedup), and the evaluation is what declares which one is current.
            "document_id": str(row[16]) if row[16] is not None else None,
            # Phase 4B: which rule judged this answer's evidence. An answer
            # rejected by an older rule can be re-judged for nothing.
            "evidence_policy_version": row[17] or EVIDENCE_POLICY_V1,
            # The stored aggregate and the authoritative count, side by side:
            # a disagreement is a defect, not something to average.
            # `provider_attempts` counts every recorded call; the budget below
            # is what those calls actually spent.
            "provider_attempts_stored": row[4],
            "attempt_records": records,
            "attempts_used": used,
            "configuration_failures": attempts.get("configuration_failures", 0),
            "last_attempt_configuration_failure": attempts.get(
                "last_attempt_configuration_failure", False),
            "attempts_aggregate_agrees": row[4] == records,
            "max_model_generations": max_model_generations,
            "attempts_remaining": max(max_model_generations - used, 0),
            "generation_budget_policy_version": GENERATION_BUDGET_POLICY_VERSION,
            "attempt_outcomes": attempts.get("outcomes", []),
            "attempt_error_codes": attempts.get("error_codes", []),
        })

    # Attempt history with no surviving evaluation row is still history.
    orphaned = [{"source_fingerprint_prefix": key[:16], **value}
                for key, value in attempts_by_fingerprint.items()
                if key not in {item["source_fingerprint"] for item in evaluations}]

    rendered_row = _rows(connection, RENDERED, lecture_id)
    rendered = None
    if rendered_row:
        row = rendered_row[0]
        rendered = {"rendered_session_id": str(row[0]), "render_status": row[1],
                    "renderer_version": row[2],
                    "source_fingerprint_prefix": row[3][:16],
                    "met_count": row[4], "partial_count": row[5], "not_met_count": row[6],
                    # Phase 4A: which evaluation this render belongs to. A
                    # render of a superseded evaluation is a real render of
                    # the wrong checklist, and only the lineage says so.
                    "evaluation_id": str(row[7]),
                    # The legacy session id this render targets. Needed to ask
                    # whether a legacy row already exists and who owns it.
                    "legacy_session_id": row[8]}

    perfect_results = [
        {"eligibility_version": row[0], "is_perfect": row[1], "reason": row[2],
         "attendance_coverage_status": row[3], "base_reason": row[4],
         "computed_at": str(row[5])}
        for row in _rows(connection, PERFECT_RESULTS, lecture_id)]
    perfect_writes = [
        {"write_status": row[0], "writer_version": row[1],
         "eligibility_version": row[2], "legacy_lecture_key": row[3]}
        for row in _rows(connection, PERFECT_WRITES, lecture_id)]

    current = evaluations[0] if evaluations else None
    state = {
        "lecture_id": str(lecture[0]), "subject": lecture[1],
        "session_date": lecture[2].isoformat(), "is_cancelled": lecture[3],
        "downstream_ready": lecture[4],
        **coverage,
        "engagement": engagement,
        "current_evaluation": current,
        "evaluations": evaluations,
        "orphaned_attempt_histories": orphaned,
        "generation_contracts_seen": sorted(
            {item["provider_contract_version"] for item in evaluations}),
        "rendered": rendered,
        "qa_legacy_writes": [
            {"write_id": str(row[0]), "write_status": row[1],
             "writer_version": row[2], "legacy_session_id": row[3]}
            for row in _rows(connection, QA_WRITES, lecture_id)],
        "perfect_results": perfect_results,
        "perfect_legacy_writes": perfect_writes,
        "resume_stage": _resume_stage(current, rendered, perfect_results),
        "attempt_accounting_consistent": all(
            item["attempts_aggregate_agrees"] for item in evaluations),
        "attendance_coverage_version": ATTENDANCE_COVERAGE_VERSION,
        "perfect_policy_version": DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    }
    state["next_action"] = _next_action(state)
    return state


def _perfect_under(results, version):
    return next((item for item in results
                 if item["eligibility_version"] == version), None)


def _next_action(state) -> str:
    """
    The EARLIEST stage that is missing or stale.

    Deliberately ordered, because recovery must resume from the earliest
    affected stage rather than from wherever the last thing happened to stop.
    Attendance comes first: everything below it is derived from it, so a stale
    attendance answer makes every later answer stale too.
    """
    coverage = state["attendance_coverage_status"]
    engagement = state.get("engagement")
    current = state.get("current_evaluation")

    # 1. the source has not answered. Nothing downstream can be fixed by us.
    if not state["attendance_source_authoritative"]:
        return WAIT_FOR_ATTENDANCE_SOURCE

    # 2. the source answered, but the engagement we hold predates that answer.
    if engagement is None or not engagement.get("is_current_snapshot"):
        return RECALCULATE_ENGAGEMENT

    # 3. no usable QA result yet.
    if current is None:
        return RUN_PHASE_3A
    if (current["qa_status"] == "REVIEW_REQUIRED"
            and current.get("review_reason") == TRANSCRIPT_COVERAGE_INCOMPLETE):
        # The same inputs give the same answer; no amount of re-running helps.
        return REVIEW_REQUIRED_MANUAL
    if current["qa_status"] not in ("COMPLETED", "NON_DELIVERED"):
        return (RUN_PHASE_3A if current["attempts_remaining"] > 0
                else REVIEW_REQUIRED_MANUAL)

    # 4. the QA result was computed from a superseded engagement row.
    if current.get("engagement_id") and engagement.get("engagement_id") and             current["engagement_id"] != engagement["engagement_id"]:
        return REFRESH_DETERMINISTIC_QA

    if state["rendered"] is None or state["rendered"]["render_status"] != "RENDERED":
        return REFRESH_RENDER

    # 5. Perfect, under the policy that governs NEW work.
    perfect = _perfect_under(state["perfect_results"],
                             DEFAULT_PERFECT_ELIGIBILITY_VERSION)
    if perfect is None:
        return EVALUATE_PERFECT
    if perfect["reason"] == PENDING_ATTENDANCE_DATA:
        return WAIT_FOR_ATTENDANCE_SOURCE
    if perfect["is_perfect"] and not state["perfect_legacy_writes"]:
        return SYNC_PERFECT
    return NOTHING_TO_DO


def _resume_stage(current, rendered, perfect_results) -> str:
    """The first stage a resume would actually have to execute."""
    if current is None:
        return "PHASE_3A"
    if current["qa_status"] not in ("COMPLETED", "NON_DELIVERED"):
        return "PHASE_3A" if current["attempts_remaining"] > 0 else "PHASE_3A_BLOCKED"
    if rendered is None or rendered["render_status"] != "RENDERED":
        return "PHASE_3B"
    if not perfect_results:
        return "PERFECT_ELIGIBILITY"
    return "LEGACY_WRITE"
