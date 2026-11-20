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
    TRANSCRIPT_LISTING_NOT_SETTLED,
    TRANSCRIPT_LISTING_SETTLE,
)
from app.orchestration.state import PipelineStateResolver
from test_pipeline_state import (
    EVALUATION_ID,
    FINGERPRINT,
    LECTURE_ID,
    NOW,
    RENDER_ID,
    SCHEDULED_END,
    SCHEDULED_START,
    StubConnection,
    StubObservations,
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

AFTER_GRACE = SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(minutes=5)
# A listing of the meeting made once the meeting had settled.
SETTLED_LISTING = SCHEDULED_END + TRANSCRIPT_LISTING_SETTLE + timedelta(minutes=1)


def combine_failed_rows(*, failure, selected_at, listed_at=SETTLED_LISTING):
    row = selection_row(status="COMBINE_FAILED", duration_minutes=None,
                        actual_start=None, actual_end=None, updated_at=selected_at)
    return complete_rows(selection=[row + (failure,)], documents=[], speakers=[],
                         evaluations=[], attempts=[], rendered=[], coverage=[],
                         transcript_listing=[(listed_at,)])


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


# --- an unusable transcript seen before the meeting had settled ------------------
#
# Marketing Impact and Planning, 2026-11-17 (94008498): the only listing of the
# meeting ran 3 minutes after the scheduled end, while the class was still live.
# Teams held a 52-second pre-class test and nothing else; the 2-hour transcript
# appeared minutes later on the same call. Nothing asked again, and after the
# grace the lecture was closed as "not delivered" with 11 learners present.

EARLY_LISTING = SCHEDULED_END + timedelta(minutes=3)


def stub_resolve(rows):
    connection = StubConnection(rows)
    resolver = PipelineStateResolver(legacy_observations=StubObservations(
        recording_url="https://example.invalid/recording", excel_synced_at=NOW))
    return resolver.for_lecture(connection, LECTURE_ID), connection


def test_13_an_early_listing_is_redone_before_the_lecture_is_closed():
    result = resolve(combine_failed_rows(failure="TRANSCRIPT_UNUSABLE",
                                         selected_at=AFTER_GRACE,
                                         listed_at=EARLY_LISTING))
    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["stages"][SELECTION]["action"] == ACQUIRE_TRANSCRIPT
    assert result["stages"][SELECTION]["reason"] == TRANSCRIPT_LISTING_NOT_SETTLED
    assert result["next_executable_action"] == ACQUIRE_TRANSCRIPT
    assert result["not_delivered"] is False
    assert result["is_complete"] is False
    for stage in NO_TRANSCRIPT_DEPENDENT_STAGES:
        assert result["stages"][stage].get("reason") != EMPTY_TRANSCRIPT_FOR_OCCURRENCE, stage


def test_14_a_settled_listing_newer_than_the_selection_reselects_for_free():
    """Teams has answered since; the selector has not read that answer yet."""
    result = resolve(combine_failed_rows(
        failure="TRANSCRIPT_UNUSABLE", selected_at=AFTER_GRACE,
        listed_at=AFTER_GRACE + timedelta(hours=1)))
    assert result["stages"][SELECTION]["state"] == MISSING
    assert result["stages"][SELECTION]["action"] == SELECT_TRANSCRIPT
    assert result["not_delivered"] is False


def test_15_listing_and_selection_in_the_same_cycle_close_it_with_no_further_work():
    """The sweep and the re-selection share the cycle's transaction time, so a
    still-unusable answer closes the lecture in that cycle - no loop."""
    result = resolve(combine_failed_rows(
        failure="TRANSCRIPT_UNUSABLE", selected_at=AFTER_GRACE, listed_at=AFTER_GRACE))
    assert result["not_delivered_reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE
    assert result["next_executable_action"] == NOTHING_TO_DO


def test_16_the_settle_boundary_counts_as_settled():
    result = resolve(combine_failed_rows(
        failure="TRANSCRIPT_UNUSABLE", selected_at=AFTER_GRACE,
        listed_at=SCHEDULED_END + TRANSCRIPT_LISTING_SETTLE))
    assert result["not_delivered_reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE


def test_17_no_listing_evidence_at_all_never_closes_it():
    result = resolve(combine_failed_rows(failure="TRANSCRIPT_UNUSABLE",
                                         selected_at=AFTER_GRACE, listed_at=None))
    assert result["stages"][SELECTION]["action"] == ACQUIRE_TRANSCRIPT
    assert result["not_delivered"] is False


def test_18_the_real_transcript_found_by_the_relisting_is_processed_normally():
    """After the sweep the selector picks the 2-hour part: an ordinary,
    delivered lecture, and the listing is not even consulted."""
    result, connection = stub_resolve(complete_rows(
        transcript_listing=[(AFTER_GRACE,)]))
    assert result["not_delivered"] is False
    assert result["stages"][SELECTION]["state"] == COMPLETE
    assert "transcript_listing" not in connection.seen


def test_19_before_the_grace_nothing_changes_and_no_extra_read_is_made():
    result, connection = stub_resolve(combine_failed_rows(
        failure="TRANSCRIPT_UNUSABLE", selected_at=SCHEDULED_END + timedelta(hours=1),
        listed_at=EARLY_LISTING))
    assert result["stages"][SELECTION]["action"] == SELECT_TRANSCRIPT
    assert "transcript_listing" not in connection.seen


def test_20_no_same_day_transcript_keeps_its_existing_rule():
    """Out of scope on purpose: "no transcript for the day" is still closed by
    the grace alone, exactly as before."""
    rows = no_transcript_rows(selected_at=AFTER_GRACE)
    rows["transcript_listing"] = [(EARLY_LISTING,)]
    result, connection = stub_resolve(rows)
    assert result["not_delivered_reason"] == NO_TRANSCRIPT_FOR_OCCURRENCE
    assert "transcript_listing" not in connection.seen


# --- a meeting that never had any transcript at all ------------------------------
#
# M1, 2026-10-06: Teams held no transcript for this meeting on any day, so the
# TRANSCRIPT stage itself stayed MISSING and re-fetched every cycle - one stage
# before the selection-level close-out above could ever see it.

def never_transcribed_rows(*, swept_at, now_after_grace=True):
    return complete_rows(
        artifacts=[(0, 0, 0, None, 0, 0)],
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
    rows["artifacts"] = [(1, 1, 0, SCHEDULED_END + NO_TRANSCRIPT_GRACE + timedelta(hours=1), 1, 0)]
    result = resolve(rows)
    assert result["stages"][TRANSCRIPT]["state"] == COMPLETE
    assert result["stages"][TRANSCRIPT].get("reason") != NO_TRANSCRIPT_FOR_OCCURRENCE


# --- the same rule inside a real cycle -------------------------------------------
#
# The real resolver and the real orchestrator; only Graph and the selector are
# played by a runner that changes the stub rows the way they would.

from app.orchestration.orchestrator import PipelineOrchestrator  # noqa: E402
from app.orchestration.stages import AUTOMATABLE_ACTIONS  # noqa: E402

CYCLE_AT = AFTER_GRACE + timedelta(hours=20)     # the next evening's cycle


class Preflight:
    def check(self):
        return {"status": "LEGACY_QA_DISABLED", "legacy_qa_node_disabled": True,
                "reason": "test", "writes_permitted": False}


class TeamsWorld:
    """
    ACQUIRE_TRANSCRIPT = a day sweep that (unless Graph fails) lists the meeting,
    stamping the cycle time; SELECT_TRANSCRIPT = re-selection at the cycle time,
    which picks the full transcript when Graph had one to give.
    """

    def __init__(self, rows, *, graph_has_full_transcript, listing_fails=False):
        self.rows = rows
        self.graph_has_full_transcript = graph_has_full_transcript
        self.listing_fails = listing_fails
        self.listed_full = False
        self.calls = []

    def scope_of(self, action):
        return "DAY" if action in (ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT) else "LECTURE"

    def can_run(self, action):
        return action in AUTOMATABLE_ACTIONS

    def refusal_reason(self, action):
        return "REFUSED"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append(action)
        if action == ACQUIRE_TRANSCRIPT and not self.listing_fails:
            self.rows["transcript_listing"] = [(CYCLE_AT,)]
            self.listed_full = self.graph_has_full_transcript
        elif action == SELECT_TRANSCRIPT:
            if self.listed_full:
                self.rows.update(complete_rows(transcript_listing=[(CYCLE_AT,)]))
            else:
                (row,) = self.rows["selection"]
                self.rows["selection"] = [row[:4] + (CYCLE_AT,) + row[5:]]
        return {"graph_calls": int(action == ACQUIRE_TRANSCRIPT), "provider_calls": 0,
                "legacy_rows_written": 0, "summary": {}}


def run_cycle(world):
    resolver = PipelineStateResolver(legacy_observations=StubObservations(
        recording_url="https://example.invalid/recording", excel_synced_at=NOW))
    orchestrator = PipelineOrchestrator(resolver=resolver, runner=world,
                                        preflight=Preflight())
    connection = StubConnection(world.rows)
    orchestrator.run_window(connection, SCHEDULED_START.date())
    return resolver.for_lecture(connection, LECTURE_ID)


def production_shape():
    return combine_failed_rows(failure="TRANSCRIPT_UNUSABLE",
                               selected_at=AFTER_GRACE, listed_at=EARLY_LISTING)


def test_21_cycle_a_delivered_lecture_recovers_with_one_sweep():
    world = TeamsWorld(production_shape(), graph_has_full_transcript=True)
    final = run_cycle(world)
    assert world.calls == [ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT]
    assert final["not_delivered"] is False
    assert final["stages"][SELECTION]["state"] == COMPLETE


def test_22_cycle_a_genuinely_undelivered_lecture_closes_after_one_sweep_and_stays_closed():
    world = TeamsWorld(production_shape(), graph_has_full_transcript=False)
    final = run_cycle(world)
    assert world.calls == [ACQUIRE_TRANSCRIPT, SELECT_TRANSCRIPT]
    assert final["not_delivered_reason"] == EMPTY_TRANSCRIPT_FOR_OCCURRENCE
    assert final["next_executable_action"] == NOTHING_TO_DO

    world.calls.clear()
    run_cycle(world)                    # the next cycle: no Graph, no selection
    assert world.calls == []


def test_23_cycle_a_failed_relisting_is_tried_once_per_cycle_and_never_closes():
    world = TeamsWorld(production_shape(), graph_has_full_transcript=True,
                       listing_fails=True)
    final = run_cycle(world)
    assert world.calls == [ACQUIRE_TRANSCRIPT]
    assert final["not_delivered"] is False
    assert final["next_executable_action"] == ACQUIRE_TRANSCRIPT
