"""
selection_input_fingerprint_v1: a selection is stale when the evidence the
selector would read for THIS occurrence has changed - never because Graph was
asked again, and never because a later occurrence of the same Teams series
produced a transcript.

The production shape: "AI in Project Control 2026" is a weekly series on one
Teams meeting id. Phase 2A links every occurrence's transcript to every lecture
of the series, so the 2026-09-04 lecture holds the 09-11 transcript as a
candidate. A re-fetch of the series on 2026-09-22 then marked 09-04
SELECTION=STALE although nothing relevant to 09-04 had changed.
"""
from datetime import timedelta

from app.orchestration.stages import COMPLETE, SELECT_TRANSCRIPT, SELECTION, STALE
from app.transcripts.selection import (
    CandidateArtifact,
    relevant_candidates,
    select_transcript_parts,
    selection_input_fingerprint,
)
from test_pipeline_state import (
    CANDIDATES,
    NOW,
    SCHEDULED_END,
    SCHEDULED_START,
    SESSION_DATE,
    candidate_row,
    complete_rows,
    input_fingerprint,
    resolve,
    selection_row,
)


# Two real production spellings of ONE transcript (Graph re-serialized its ids
# on 2026-09-22); the same pair test_safety_fixes_f01_f02_f03 pins.
REAL_CANONICAL = (
    "ktVizIDGAAAAgvBxlATZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjI=")
REAL_VARIANT = (
    "ktVizIHGAAAAg_BylQTZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjLA")

NEXT_WEEK = candidate_row("next-week", SCHEDULED_START + timedelta(days=7),
                          SCHEDULED_END + timedelta(days=7), sha="7" * 64,
                          call_id="call-next-week")
WEEK_AFTER = candidate_row("week-after", SCHEDULED_START + timedelta(days=14),
                           SCHEDULED_END + timedelta(days=14), sha="8" * 64,
                           call_id="call-week-after")


def selection_state(rows):
    return resolve(rows)["stages"][SELECTION]


def as_candidates(rows):
    return [CandidateArtifact(
        artifact_id=row[0], provider_transcript_id=row[1],
        provider_created_at=row[2], provider_end_at=row[3],
        provider_call_id=row[4], meeting_id=row[5], content_sha256=row[6],
        content_bytes=row[7]) for row in rows]


# --- 6, 7. the recurring series and re-fetches ---------------------------------

def test_6_a_future_recurring_occurrence_does_not_make_an_earlier_one_stale():
    rows = complete_rows(
        selection_candidates=CANDIDATES + [NEXT_WEEK],
        # ...fetched long after this lecture's selection was made.
        artifacts=[(3, 3, 0, NOW + timedelta(days=7))])
    stage = selection_state(rows)
    assert stage["state"] == COMPLETE
    assert stage["relevant_candidate_count"] == 2


def test_6b_two_later_occurrences_still_leave_it_complete():
    rows = complete_rows(selection_candidates=CANDIDATES + [NEXT_WEEK, WEEK_AFTER],
                         artifacts=[(4, 4, 0, NOW + timedelta(days=14))])
    assert selection_state(rows)["state"] == COMPLETE


def test_7_refetching_identical_content_is_not_new_evidence():
    """Same bytes, same ids, a newer content_fetched_at: nothing changed."""
    rows = complete_rows(artifacts=[(2, 2, 0, NOW + timedelta(hours=6))])
    result = resolve(rows)
    assert result["stages"][SELECTION]["state"] == COMPLETE
    assert result["next_action"] != SELECT_TRANSCRIPT


def test_7b_the_fingerprint_ignores_fetch_and_seen_times():
    refetched = [candidate_row(*row[1:4], sha=row[6], call_id=row[4],
                               first_seen=NOW + timedelta(days=3))
                 for row in CANDIDATES]
    assert input_fingerprint(refetched) == input_fingerprint(CANDIDATES)


# --- 8, 9. genuinely new evidence for the same occurrence ----------------------

def test_8_changed_content_of_a_relevant_candidate_makes_the_selection_stale():
    revised = [CANDIDATES[0], candidate_row(
        "transcript-2", CANDIDATES[1][2], CANDIDATES[1][3], sha="9" * 64)]
    stage = selection_state(complete_rows(selection_candidates=revised))
    assert stage["state"] == STALE
    assert stage["action"] == SELECT_TRANSCRIPT
    assert stage["reason"] == "SELECTION_INPUTS_CHANGED"


def test_9_a_new_relevant_candidate_makes_the_selection_stale():
    late_part = candidate_row("transcript-3", SCHEDULED_END - timedelta(minutes=5),
                              SCHEDULED_END + timedelta(minutes=40), sha="3" * 64)
    stage = selection_state(complete_rows(
        selection_candidates=CANDIDATES + [late_part]))
    assert stage["state"] == STALE
    assert stage["reason"] == "SELECTION_INPUTS_CHANGED"
    assert stage["relevant_candidate_count"] == 3


def test_9b_new_timing_for_a_relevant_candidate_is_new_evidence():
    extended = [CANDIDATES[0], candidate_row(
        "transcript-2", CANDIDATES[1][2], CANDIDATES[1][3] + timedelta(minutes=30),
        sha=CANDIDATES[1][6])]
    assert selection_state(complete_rows(
        selection_candidates=extended))["state"] == STALE


# --- 10. canonical identity -----------------------------------------------------

def test_10_a_reserialized_graph_id_is_the_same_selection_input():
    spelled = [candidate_row(REAL_CANONICAL, SCHEDULED_START, SCHEDULED_END,
                             sha="1" * 64)]
    respelled = [candidate_row(REAL_VARIANT, SCHEDULED_START, SCHEDULED_END,
                               sha="1" * 64, artifact_id="another-artifact")]
    assert input_fingerprint(spelled) == input_fingerprint(respelled)


def test_10b_the_resolver_accepts_a_selection_made_under_the_old_spelling():
    first = [candidate_row(REAL_CANONICAL, SCHEDULED_START, SCHEDULED_END, sha="1" * 64)]
    later = [candidate_row(REAL_VARIANT, SCHEDULED_START, SCHEDULED_END, sha="1" * 64)]
    rows = complete_rows(
        selection_candidates=later,
        selection=[selection_row(fingerprint=input_fingerprint(first))])
    assert selection_state(rows)["state"] == COMPLETE


# --- one definition, shared ------------------------------------------------------

def test_the_selector_stamps_exactly_what_the_resolver_recomputes():
    candidates = as_candidates(CANDIDATES + [NEXT_WEEK])
    schedule = {"scheduled_start": SCHEDULED_START, "scheduled_end": SCHEDULED_END,
                "target_date": SESSION_DATE, "meeting_id": "meeting-1"}
    outcome = select_transcript_parts(candidates, **schedule)
    relevance = relevant_candidates(candidates, **schedule)
    assert outcome.diagnostics["selection_input_fingerprint"] == relevance.fingerprint
    assert relevance.fingerprint == selection_input_fingerprint(
        as_candidates(CANDIDATES))
    # The future occurrence is a candidate but was never relevant.
    assert outcome.diagnostics["candidate_count_before_date_filter"] == 3
    assert outcome.diagnostics["occurrence_window_candidate_count"] == 2


def test_the_resolver_no_longer_compares_fetch_timestamps():
    import inspect
    from app.orchestration import state
    source = inspect.getsource(state.PipelineStateResolver._selection)
    assert "newest_content_at" not in source
    assert "relevant_candidates" in inspect.getsource(state)
    assert "load_candidates" in inspect.getsource(
        state.PipelineStateResolver._selection_freshness)


# --- selections persisted before fingerprints existed --------------------------

def test_a_legacy_selection_with_unchanged_evidence_is_complete():
    rows = complete_rows(selection=[selection_row(fingerprint="")],
                         selection_candidates=CANDIDATES + [NEXT_WEEK],
                         artifacts=[(3, 3, 0, NOW + timedelta(days=7))])
    stage = selection_state(rows)
    assert stage["state"] == COMPLETE
    assert stage["freshness_basis"] == "LEGACY_SELECTION_EQUIVALENCE"


def test_a_legacy_selection_whose_part_content_changed_is_stale():
    revised = [CANDIDATES[0], candidate_row(
        "transcript-2", CANDIDATES[1][2], CANDIDATES[1][3], sha="9" * 64)]
    stage = selection_state(complete_rows(
        selection=[selection_row(fingerprint="")], selection_candidates=revised))
    assert stage["state"] == STALE
    assert stage["reason"] == "SELECTED_CONTENT_CHANGED"


def test_a_legacy_selection_that_would_now_attach_another_part_is_stale():
    late_part = candidate_row("transcript-3", SCHEDULED_END + timedelta(minutes=5),
                              SCHEDULED_END + timedelta(minutes=40), sha="3" * 64)
    stage = selection_state(complete_rows(
        selection=[selection_row(fingerprint="")],
        selection_candidates=CANDIDATES + [late_part]))
    assert stage["state"] == STALE
    assert stage["reason"] == "SELECTED_PARTS_CHANGED"
