"""
Lectures that did not happen.

2026-09-30 is the production shape. "G2 Keith - Commercial Intelligence" had no
Teams transcript at all that day and sat "in progress" re-selecting nothing on
every cycle; "Steve - EVM" and "Andrew - SP" were correctly judged NON_DELIVERED
by QA, yet the console still said they were waiting for attendance. Both are
the same fact - the lecture did not happen - and the state says so once, as
`not_delivered`, so no consumer has to re-derive it.
"""
from datetime import timedelta

from app.orchestration.reconciliation import DayReconciliation
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    ATTENDANCE,
    CANONICAL_CUES,
    COMPLETE,
    MISSING,
    NOT_APPLICABLE,
    NOTHING_TO_DO,
    QA_EVALUATION,
    SELECT_TRANSCRIPT,
    SELECTION,
    TRANSCRIPT,
)
from app.orchestration.state import (
    EMPTY_TRANSCRIPT_FOR_OCCURRENCE,
    NO_TRANSCRIPT_DEPENDENT_STAGES,
    NO_TRANSCRIPT_FOR_OCCURRENCE,
    NO_TRANSCRIPT_GRACE,
    QA_NON_DELIVERED,
)
from test_pipeline_state import (
    EVALUATION_ID,
    FINGERPRINT,
    RENDER_ID,
    SCHEDULED_END,
    SCHEDULED_START,
    complete_rows,
    resolve,
    selection_row,
)
from test_qa_document_lineage import evaluation_row


def no_transcript_rows(*, selected_at):
    """Graph had transcripts for other days of the series, none for this one."""
    return complete_rows(
        selection=[selection_row(status="NO_SAME_DAY_CANDIDATES", duration_minutes=None,
                                 actual_start=None, actual_end=None,
                                 updated_at=selected_at)],
        documents=[], speakers=[], evaluations=[], attempts=[], rendered=[],
        coverage=[])


def test_1_no_transcript_after_the_grace_is_not_delivered_and_owes_nothing():
    result = resolve(no_transcript_rows(
        selected_at=SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=5)))

    assert result["stages"][SELECTION]["state"] == NOT_APPLICABLE
    assert result["stages"][SELECTION]["reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE
    for stage in NO_TRANSCRIPT_DEPENDENT_STAGES:
        assert result["stages"][stage]["state"] == NOT_APPLICABLE, stage
    assert result["next_executable_action"] == NOTHING_TO_DO
    assert result["is_complete"] is True
    assert result["not_delivered"] is True
    assert result["not_delivered_reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE


def test_2_an_answer_given_before_the_grace_is_still_retried():
    """A "nothing" from just after the lecture may predate the transcript."""
    result = resolve(no_transcript_rows(selected_at=SCHEDULED_END + timedelta(hours=2)))

    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["stages"][SELECTION]["action"] == SELECT_TRANSCRIPT
    assert result["stages"][CANONICAL_CUES]["state"] != NOT_APPLICABLE
    assert result["not_delivered"] is False


def test_3_a_qa_non_delivered_lecture_is_flagged_not_delivered():
    rows = complete_rows(
        selection=[selection_row(duration_minutes=6,
                                 actual_end=SCHEDULED_START + timedelta(minutes=6))],
        evaluations=[evaluation_row(status="NON_DELIVERED", ai_called=False)],
        attempts=[],
        rendered=[(RENDER_ID, "RENDERED_NON_DELIVERED", "legacy_qa_v8_renderer_v1",
                   FINGERPRINT, 0, 0, 11, EVALUATION_ID, "legacy-session-1")])
    result = resolve(rows)
    assert result["not_delivered"] is True
    assert result["not_delivered_reason"] == QA_NON_DELIVERED


def test_4_a_delivered_lecture_is_not_flagged():
    result = resolve()
    assert result["not_delivered"] is False
    assert result["not_delivered_reason"] is None


def test_5_the_day_report_never_counts_a_not_delivered_lecture_as_waiting():
    waiting = resolve(no_transcript_rows(
        selected_at=SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=5)))
    # Same lecture, but pretend attendance were still waiting on the source.
    waiting["stages"][ATTENDANCE] = {"state": "WAITING", "action": None}
    report = DayReconciliation(resolver=None).from_states(
        SCHEDULED_START.date(), [waiting])

    assert report["attendance_waiting_count"] == 0
    assert report["not_delivered_count"] == 1
    (row,) = report["lectures"]
    assert row["not_delivered"] is True
    assert row["not_delivered_reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE
    assert row["stages"][QA_EVALUATION] == NOT_APPLICABLE


# --- a fetched transcript with no measurable speech ----------------------------
#
# Stephen - Portfolio Management, 2026-10-01: a 13-minute meeting, no teaching,
# a transcript that could not even be combined. Left alone it re-selected for
# ever and the console said "in progress".

def combine_failed_rows(*, failure, selected_at):
    row = selection_row(status="COMBINE_FAILED", duration_minutes=None,
                        actual_start=None, actual_end=None, updated_at=selected_at)
    return complete_rows(selection=[row + (failure,)], documents=[], speakers=[],
                         evaluations=[], attempts=[], rendered=[], coverage=[])


AFTER_GRACE = SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=5)


def test_6_an_empty_transcript_after_the_grace_is_not_delivered():
    result = resolve(combine_failed_rows(failure="TRANSCRIPT_UNUSABLE",
                                         selected_at=AFTER_GRACE))
    assert result["stages"][SELECTION]["state"] == NOT_APPLICABLE
    assert result["stages"][SELECTION]["reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE
    for stage in NO_TRANSCRIPT_DEPENDENT_STAGES:
        assert result["stages"][stage]["state"] == NOT_APPLICABLE, stage
    assert result["not_delivered_reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_7_content_not_fetched_yet_is_always_retried():
    result = resolve(combine_failed_rows(failure="CONTENT_NOT_FETCHED",
                                         selected_at=AFTER_GRACE))
    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["stages"][SELECTION]["action"] == SELECT_TRANSCRIPT
    assert result["not_delivered"] is False


def test_8_an_empty_transcript_before_the_grace_is_still_retried():
    result = resolve(combine_failed_rows(
        failure="TRANSCRIPT_UNUSABLE", selected_at=SCHEDULED_END + timedelta(hours=1)))
    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["not_delivered"] is False


# --- a meeting that never had any transcript at all ------------------------------
#
# M1, 2026-10-06: Teams held no transcript for this meeting on any day, so the
# TRANSCRIPT stage itself stayed MISSING and re-fetched every cycle - one stage
# before the selection-level close-out above could ever see it.

def never_transcribed_rows(*, swept_at, now_after_grace=True):
    return complete_rows(
        artifacts=[(0, 0, 0, None)],
        selection=[selection_row(status="NO_CANDIDATES", duration_minutes=None,
                                 actual_start=None, actual_end=None,
                                 updated_at=SCHEDULED_END + timedelta(hours=1))],
        acquisition=[(swept_at,)],
        documents=[], speakers=[], evaluations=[], attempts=[], rendered=[],
        coverage=[])


def test_9_never_transcribed_after_a_clean_sweep_past_the_grace_is_not_delivered():
    result = resolve(never_transcribed_rows(
        swept_at=SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=1)))
    assert result["stages"][TRANSCRIPT]["state"] == NOT_APPLICABLE
    assert result["stages"][TRANSCRIPT]["reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE
    assert result["stages"][SELECTION]["state"] == NOT_APPLICABLE
    for stage in NO_TRANSCRIPT_DEPENDENT_STAGES:
        assert result["stages"][stage]["state"] == NOT_APPLICABLE, stage
    assert result["next_executable_action"] == NOTHING_TO_DO
    assert result["not_delivered_reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE


def test_10_a_sweep_before_the_grace_does_not_close_it():
    result = resolve(never_transcribed_rows(swept_at=SCHEDULED_END + timedelta(hours=2)))
    assert result["stages"][TRANSCRIPT]["state"] == MISSING
    assert result["stages"][TRANSCRIPT]["action"] == ACQUIRE_TRANSCRIPT
    assert result["not_delivered"] is False


def test_11_no_clean_sweep_at_all_does_not_close_it():
    """A sweep with errors or blocked meetings is not counted (the query
    filters them), which reads here as no clean sweep."""
    result = resolve(never_transcribed_rows(swept_at=None))
    assert result["stages"][TRANSCRIPT]["state"] == MISSING
    assert result["not_delivered"] is False


def test_12_a_transcript_that_arrives_later_reopens_the_lecture():
    rows = never_transcribed_rows(
        swept_at=SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=1))
    rows["artifacts"] = [(1, 1, 0, SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(hours=1))]
    result = resolve(rows)
    assert result["stages"][TRANSCRIPT]["state"] == COMPLETE
    assert result["stages"][TRANSCRIPT].get("reason") != NO_TRANSCRIPT_FOR_OCCURRENCE
