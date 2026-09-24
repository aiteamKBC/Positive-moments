"""
QA document lineage: an evaluation is an answer about ONE transcript document.

When the current canonical document is a different one, the stored answer is
not stale-but-refreshable, it is an answer to a different question. The state
resolver therefore asks for a real re-evaluation (RUN_QA), never the
deterministic refresh that reuses the frozen model output. That holds for an
old NON_DELIVERED verdict as well: a verdict drawn from a short transcript
must not survive the arrival of a better one.

The scheduler and the backfill are exercised through the real
PipelineOrchestrator and the real PipelineStateResolver - the only path either
of them has - so neither can hold a private copy of these rules.
"""
import inspect
import uuid
from datetime import date, timedelta

from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.stages import (
    CANONICAL_CUES,
    COMPLETE,
    LEGACY_QA_SYNC,
    MANUAL_REVIEW_REQUIRED,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    QA_EVALUATION,
    REFRESH_DETERMINISTIC_QA,
    REVIEW_REQUIRED,
    RUN_QA,
    RUN_TYPE_BACKFILL,
    RUN_TYPE_SCHEDULED,
    STALE,
)
from app.orchestration.state import PipelineStateResolver
from app.qa.delivery import TRANSCRIPT_COVERAGE_INCOMPLETE
from app.qa.evidence_policy import DEFAULT_EVIDENCE_POLICY
from test_orchestration import StubPreflight
from test_pipeline_state import (
    DOCUMENT_ID,
    ENGAGEMENT_ID,
    EVALUATION_ID,
    FINGERPRINT,
    LECTURE_ID,
    NOW,
    PARSER_VERSION,
    RENDER_ID,
    SCHEDULED_END,
    SCHEDULED_START,
    SELECTION_ID,
    SESSION_DATE,
    SNAPSHOT_ID,
    StubConnection,
    StubObservations,
    complete_rows,
    resolve,
    selection_row,
)
from tests.unit.test_shadow_qa import (
    TARGET,
    StubEvaluations,
    StubProvider,
    good_output,
    package,
    service,
)


NEW_DOCUMENT_ID = "33333333-3333-5333-8333-3333333333bb"
NEW_ENGAGEMENT_ID = "55555555-5555-5555-8555-5555555555bb"
COVERAGE_EVALUATION_ID = "66666666-6666-5666-8666-6666666666cc"


def evaluation_row(*, evaluation_id=EVALUATION_ID, status="COMPLETED", review=None,
                   document_id=DOCUMENT_ID, engagement_id=ENGAGEMENT_ID,
                   fingerprint=FINGERPRINT, updated_at=NOW, ai_called=True):
    """One row of the recovery EVALUATIONS query, in column order."""
    counts = (11, 0, 0) if status == "COMPLETED" else (
        (0, 0, 11) if status == "NON_DELIVERED" else (None, None, None))
    return (evaluation_id, fingerprint, status, review, 1 if ai_called else 0,
            ai_called, *counts, "strict_json_schema_v1", "canonical_cue_bounds_v1",
            updated_at, engagement_id, SNAPSHOT_ID,
            "c" * 64 if ai_called else None, None, document_id,
            DEFAULT_EVIDENCE_POLICY)


def engagement_on(document_id, engagement_id=ENGAGEMENT_ID):
    return {
        "engagement_lineage": [(engagement_id, document_id, SNAPSHOT_ID, "CALCULATED",
                                12, 9, True, 4, "CALCULATED", False, NOW)],
        "engagement_recovery": [(engagement_id, "CALCULATED", 12, 9, 4, True,
                                 "SOURCE_AVAILABLE_WITH_MEMBERS", "CALCULATED",
                                 SNAPSHOT_ID, False, "attendance_coverage_v1")],
    }


def reselected_rows(*, old_status="COMPLETED"):
    """
    A reselection changed the combined bytes ("e" * 64). The selection and
    combined rows were rewritten in place, so the OLD document (parsed from
    "b" * 64) still carries the same selection_id. The new document B has been
    parsed and engagement recalculated on it; QA has not run again yet.
    """
    selection = list(selection_row())
    selection[3] = "e" * 64
    return complete_rows(
        selection=[tuple(selection)],
        documents=[
            (NEW_DOCUMENT_ID, PARSER_VERSION, "PARSED", 900, SELECTION_ID,
             NOW + timedelta(hours=1), "e" * 64),
            (DOCUMENT_ID, PARSER_VERSION, "PARSED", 100, SELECTION_ID, NOW, "b" * 64)],
        evaluations=[evaluation_row(status=old_status,
                                    ai_called=old_status == "COMPLETED")],
        **engagement_on(NEW_DOCUMENT_ID, NEW_ENGAGEMENT_ID))


# --- 11. same document: the deterministic refresh is preserved ----------------

def test_11_same_document_new_engagement_is_still_a_deterministic_refresh():
    rows = complete_rows(**engagement_on(DOCUMENT_ID, NEW_ENGAGEMENT_ID))
    result = resolve(rows)
    stage = result["stages"][QA_EVALUATION]
    assert stage["state"] == STALE
    assert stage["action"] == REFRESH_DETERMINISTIC_QA
    assert stage["reason"] == "EVALUATION_PREDATES_CURRENT_ENGAGEMENT"


# --- 12. a different document means a real re-evaluation ----------------------

def test_12_evaluation_on_a_superseded_document_is_stale_and_reruns_qa():
    result = resolve(reselected_rows())
    assert result["stages"][CANONICAL_CUES]["state"] == COMPLETE
    assert result["stages"][CANONICAL_CUES]["document_id"] == NEW_DOCUMENT_ID
    stage = result["stages"][QA_EVALUATION]
    assert stage["state"] == STALE
    assert stage["action"] == RUN_QA
    assert stage["action"] != REFRESH_DETERMINISTIC_QA
    assert stage["reason"] == "EVALUATION_DOCUMENT_SUPERSEDED"
    assert stage["evaluation_document_id"] == DOCUMENT_ID
    assert stage["current_document_id"] == NEW_DOCUMENT_ID
    assert result["next_action"] == RUN_QA


def test_12b_a_document_parsed_from_superseded_bytes_is_not_current():
    """Even though the evaluation still declares it, and it shares the selection."""
    rows = reselected_rows()
    rows["documents"] = rows["documents"][1:]          # B not parsed yet
    result = resolve(rows)
    assert result["stages"][CANONICAL_CUES]["state"] == STALE
    assert result["stages"][CANONICAL_CUES]["reason"] == \
        "NO_DOCUMENT_FOR_CURRENT_SELECTION"


def test_12c_the_qa_loader_only_accepts_the_document_of_the_current_bytes():
    from app.db.repositories.qa_shadow import (
        LOAD_QA_INPUTS,
        LOAD_QA_INPUTS_FOR_LECTURE,
    )
    for sql in (LOAD_QA_INPUTS, LOAD_QA_INPUTS_FOR_LECTURE):
        assert "d.source_content_sha256 = cb.content_sha256" in sql


# --- 13. an old NON_DELIVERED is never silently reused -------------------------

def test_13_old_non_delivered_on_a_superseded_document_reruns_qa():
    stage = resolve(reselected_rows(old_status="NON_DELIVERED"))["stages"][QA_EVALUATION]
    assert stage["state"] == STALE
    assert stage["action"] == RUN_QA
    assert stage["reason"] == "EVALUATION_DOCUMENT_SUPERSEDED"


def test_13b_the_rerun_does_not_reuse_the_old_non_delivered_answer():
    """New document -> new fingerprint -> the old verdict is not a match."""
    provider = StubProvider(good_output())
    old = package(duration_minutes=13, duration_seconds=780,
                  actual_end=package()["actual_start"] + timedelta(minutes=13),
                  last_cue_end_ms=780_000)
    evaluations = StubEvaluations()
    service([old], provider, evaluations).run_day(None, TARGET, execute=True)
    assert [row["qa_status"] for row in evaluations.rows.values()] == ["NON_DELIVERED"]

    better = package(document_id=uuid.UUID(int=44), document_source_fingerprint="f" * 64)
    summary = service([better], provider, evaluations).run_day(None, TARGET, execute=True)
    assert provider.calls == 1
    assert summary["reused_evaluations"] == 0
    assert summary["lectures"][0]["qa_status"] == "COMPLETED"
    # The old verdict is kept as history, not deleted.
    assert sorted(row["qa_status"] for row in evaluations.rows.values()) == \
        ["COMPLETED", "NON_DELIVERED"]


def ai_project_control_rows(*, rerun=False):
    """
    The real 2026-09-04 shape on the fixture occurrence: a call from 1m23s
    before the schedule to 50 minutes after it, a 13-minute transcript, and a
    NON_DELIVERED evaluation and render. `rerun` adds the evaluation the QA
    service writes under delivery_coverage_guard_v1.
    """
    evaluations = [evaluation_row(status="NON_DELIVERED", ai_called=False)]
    if rerun:
        evaluations.insert(0, evaluation_row(
            evaluation_id=COVERAGE_EVALUATION_ID, status="REVIEW_REQUIRED",
            review=TRANSCRIPT_COVERAGE_INCOMPLETE, ai_called=False,
            fingerprint="9" * 64, updated_at=NOW + timedelta(hours=1)))
    return complete_rows(
        selection=[selection_row(duration_minutes=13,
                                 actual_start=SCHEDULED_START - timedelta(seconds=83),
                                 actual_end=SCHEDULED_END + timedelta(minutes=50))],
        evaluations=evaluations, attempts=[],
        rendered=[(RENDER_ID, "RENDERED_NON_DELIVERED", "legacy_qa_v8_renderer_v1",
                   FINGERPRINT, 0, 0, 11, EVALUATION_ID, "legacy-session-1")])


def test_13c_same_document_non_delivered_is_reclassified_by_the_delivery_policy():
    result = resolve(ai_project_control_rows())
    stage = result["stages"][QA_EVALUATION]
    assert stage["state"] == STALE
    assert stage["action"] == RUN_QA
    assert stage["reason"] == "DELIVERY_POLICY_RECLASSIFIES_NON_DELIVERED"
    assert stage["delivery_classification"] == TRANSCRIPT_COVERAGE_INCOMPLETE
    assert stage["delivery"]["call_schedule_coverage_ratio"] == 1.0


def test_13d_after_the_rerun_the_lecture_waits_for_a_human_not_the_scheduler():
    result = resolve(ai_project_control_rows(rerun=True))
    stage = result["stages"][QA_EVALUATION]
    assert stage["state"] == REVIEW_REQUIRED
    assert stage["action"] == MANUAL_REVIEW_REQUIRED
    assert stage["reason"] == TRANSCRIPT_COVERAGE_INCOMPLETE
    assert result["next_executable_action"] == MANUAL_REVIEW_REQUIRED
    assert result["requires_review"] is True


def test_13e_a_genuine_non_delivered_stays_complete_and_never_loops():
    rows = complete_rows(
        selection=[selection_row(duration_minutes=6,
                                 actual_end=SCHEDULED_START + timedelta(minutes=6))],
        evaluations=[evaluation_row(status="NON_DELIVERED", ai_called=False)],
        attempts=[],
        rendered=[(RENDER_ID, "RENDERED_NON_DELIVERED", "legacy_qa_v8_renderer_v1",
                   FINGERPRINT, 0, 0, 11, EVALUATION_ID, "legacy-session-1")])
    assert resolve(rows)["stages"][QA_EVALUATION]["state"] == COMPLETE


# --- 14. idempotent ------------------------------------------------------------

def test_14_nothing_changed_means_nothing_to_do():
    result = resolve()
    assert result["next_action"] == NOTHING_TO_DO
    assert result["stages"][QA_EVALUATION]["state"] == COMPLETE


# --- 15, 16. the scheduler and the backfill share one path --------------------

class DatedConnection(StubConnection):
    """The stub, but the day query answers only for the lecture's own date."""

    def execute(self, sql, params=None):
        if "WHERE l.session_date" in sql and params and params[0] != SESSION_DATE:
            self.seen.append("lecture_day")
            from test_pipeline_state import StubCursor
            return StubCursor([])
        return super().execute(sql, params)


class QaRunner:
    """
    Stands in for StageRunner. RUN_QA writes what the real QA service writes
    for this lecture under the new delivery policy; nothing else is expected.
    """

    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def scope_of(self, action):
        return "LECTURE"

    def can_run(self, action):
        return True

    def refusal_reason(self, action):
        return "REFUSED"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append((action, str(lecture_id), session_date))
        assert action == RUN_QA, action
        self.connection.rows.update(ai_project_control_rows(rerun=True))
        return {"graph_calls": 0, "provider_calls": 0, "legacy_rows_written": 0,
                "summary": {}}


def run_through_orchestrator(*, run_type, lookback_days, target):
    connection = DatedConnection(ai_project_control_rows())
    resolver = PipelineStateResolver(legacy_observations=StubObservations(
        recording_url="https://example.invalid/recording", excel_synced_at=NOW))
    runner = QaRunner(connection)
    orchestrator = PipelineOrchestrator(resolver=resolver, runner=runner,
                                        preflight=StubPreflight(), max_passes=6)
    outcome = orchestrator.run_window(connection, target, lookback_days=lookback_days,
                                      run_type=run_type)
    return outcome, runner


def test_15_the_scheduled_cycle_reruns_qa_once_and_stops_at_review():
    outcome, runner = run_through_orchestrator(
        run_type=RUN_TYPE_SCHEDULED, lookback_days=3,
        target=SESSION_DATE + timedelta(days=2))
    assert runner.calls == [(RUN_QA, LECTURE_ID, SESSION_DATE)]
    assert outcome["provider_calls"] == 0
    (lecture,) = outcome["lectures"]
    assert lecture["lecture_id"] == LECTURE_ID
    assert lecture["final_action"] == MANUAL_REVIEW_REQUIRED


def test_16_the_backfill_repairs_the_same_lecture_id_and_creates_no_other():
    outcome, runner = run_through_orchestrator(
        run_type=RUN_TYPE_BACKFILL, lookback_days=0, target=SESSION_DATE)
    assert runner.calls == [(RUN_QA, LECTURE_ID, SESSION_DATE)]
    assert [item["lecture_id"] for item in outcome["lectures"]] == [LECTURE_ID]
    assert outcome["lectures"][0]["final_action"] == MANUAL_REVIEW_REQUIRED


def test_15b_neither_the_scheduler_nor_the_backfill_holds_a_private_rule():
    from app.orchestration import backfill, scheduler
    for module in (backfill, scheduler):
        source = inspect.getsource(module)
        for private in ("classify_delivery", "relevant_candidates",
                        "selection_input_fingerprint", "TRANSCRIPT_COVERAGE_INCOMPLETE",
                        "EVALUATION_DOCUMENT_SUPERSEDED"):
            assert private not in source, (module.__name__, private)
        assert "run_window" in source


# --- 17. RC4 legacy protection -------------------------------------------------

def test_17_a_foreign_legacy_row_stays_protected_while_qa_is_stale():
    rows = reselected_rows()
    rows["qa_writes"] = []
    rows["legacy_writes_state"] = []
    result = resolve(rows, observations=StubObservations(
        recording_url="https://example.invalid/recording", excel_synced_at=NOW,
        legacy_row=True))
    assert result["stages"][QA_EVALUATION]["action"] == RUN_QA
    legacy = result["stages"][LEGACY_QA_SYNC]
    assert legacy["state"] == NOT_APPLICABLE
    assert legacy["reason"] == "LEGACY_ROW_NOT_CODED_OWNED"
    assert legacy["coded_owned"] is False


def test_17b_the_automated_writer_still_never_updates_an_existing_row():
    from app.orchestration.runner import StageRunner
    assert "allow_update_existing=False" in inspect.getsource(StageRunner._writer)


# --- after the rerun: nothing downstream may claim a verdict -----------------

def test_18_a_coverage_review_is_not_renderable_and_not_perfect_eligible():
    result = resolve(ai_project_control_rows(rerun=True))
    assert result["stages"]["QA_RENDER"]["state"] == NOT_APPLICABLE
    assert result["stages"]["QA_RENDER"]["reason"] == TRANSCRIPT_COVERAGE_INCOMPLETE
    assert result["stages"]["PERFECT_ELIGIBILITY"]["state"] == NOT_APPLICABLE
    assert result["stages"]["PERFECT_ELIGIBILITY"]["reason"] == "QA_REVIEW_REQUIRED"
    assert result["stages"]["PERFECT_SYNC"]["state"] == NOT_APPLICABLE


def test_19_a_coded_row_still_saying_cancelled_is_reported_as_a_contradiction():
    stage = resolve(ai_project_control_rows(rerun=True))["stages"][LEGACY_QA_SYNC]
    assert stage["state"] == REVIEW_REQUIRED
    assert stage["action"] == MANUAL_REVIEW_REQUIRED
    assert stage["reason"] == "LEGACY_ROW_CONTRADICTS_CURRENT_EVALUATION"
    assert stage["coded_owned"] is True
    assert stage["written_from_evaluation_id"] == EVALUATION_ID
    assert stage["current_evaluation_id"] == COVERAGE_EVALUATION_ID


def test_20_after_the_withdrawal_there_is_no_legacy_work_at_all():
    rows = ai_project_control_rows(rerun=True)
    withdrawn = list(rows["legacy_writes_state"][0])
    withdrawn[3] = "ROLLED_BACK"
    rows["legacy_writes_state"] = [tuple(withdrawn)]
    result = resolve(rows, observations=StubObservations(legacy_row=False))
    stage = result["stages"][LEGACY_QA_SYNC]
    assert stage["state"] == NOT_APPLICABLE
    assert stage["reason"] == "NO_PUBLISHABLE_QA_RESULT"
    # The superseded NON_DELIVERED render is never offered for re-insertion.
    actions = {item["action"] for item in result["stages"].values()}
    assert "SYNC_LEGACY_QA" not in actions and "RENDER_QA" not in actions
    assert result["next_executable_action"] == MANUAL_REVIEW_REQUIRED


def test_21_a_foreign_row_under_a_coverage_review_is_never_offered_a_write():
    rows = ai_project_control_rows(rerun=True)
    rows["legacy_writes_state"] = []
    rows["qa_writes"] = []
    result = resolve(rows, observations=StubObservations(legacy_row=True))
    stage = result["stages"][LEGACY_QA_SYNC]
    assert stage["state"] == NOT_APPLICABLE
    assert stage.get("action") is None


# --- the delivery_status consumer audit, pinned ------------------------------

AUDITED_DELIVERY_STATUS_READERS = {
    # writes it, from the delivery decision
    "app/qa/service.py", "app/qa/delivery.py", "app/qa/deterministic.py",
    "app/qa/inputs.py",
    # selects it as a column, never branches on it
    "app/db/repositories/qa_shadow.py", "app/db/repositories/qa_rendering.py",
    # branches on it ONLY after `qa_status in RENDERABLE_QA_STATUSES`
    "app/rendering/service.py",
}


def test_every_delivery_status_reader_has_been_audited():
    """A new reader must be reviewed: DELIVERED alone never means 'QA done'."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    readers = {str(path.relative_to(root)).replace("\\", "/")
               for path in (root / "app").rglob("*.py")
               if "delivery_status" in path.read_text(encoding="utf-8")}
    assert readers == AUDITED_DELIVERY_STATUS_READERS


def test_the_renderer_checks_qa_status_before_it_reads_delivery_status():
    from app.rendering import service as rendering
    source = inspect.getsource(rendering)
    assert source.index("not in RENDERABLE_QA_STATUSES") < \
        source.index('evaluation["delivery_status"] == "NON_DELIVERED"')
    assert "REVIEW_REQUIRED" not in rendering.RENDERABLE_QA_STATUSES


def test_publishing_paths_gate_on_qa_status_not_delivery_status():
    from app.writer.modes import READY_QA_STATUSES
    assert "REVIEW_REQUIRED" not in READY_QA_STATUSES
    from app.db.repositories.qa_writer import LOAD_RENDERED, LOAD_RENDERED_FOR_LECTURE
    for statement in (LOAD_RENDERED, LOAD_RENDERED_FOR_LECTURE):
        assert "e2.updated_at > e.updated_at" in statement
