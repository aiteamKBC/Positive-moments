"""
Phase 2B selection parity.

Every rule below is ported from the authoritative legacy export
automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json, node
"Select Transcript Parts V3". The fixtures are deterministic and synthetic so
the tie-breakers and multi-part clustering are exercised, which the real
2026-09-04 data does not do.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.transcripts.selection import (
    MAX_PART_GAP,
    NO_CANDIDATES,
    NO_SAME_DAY_CANDIDATES,
    NO_WINDOW_CANDIDATES,
    SELECTED,
    SELECTION_VERSION,
    WINDOW_AFTER,
    WINDOW_BEFORE,
    CandidateArtifact,
    select_transcript_parts,
)


TARGET = date(2026, 9, 4)
MEETING = "meeting-series-1"
# Cairo is UTC+3 in September, so the Cairo day 2026-09-04 spans 21:00Z..21:00Z.
START = datetime(2026, 9, 4, 8, tzinfo=timezone.utc)
END = datetime(2026, 9, 4, 10, tzinfo=timezone.utc)


def artifact(transcript_id, start, end, *, call_id="call-a", meeting=MEETING):
    return CandidateArtifact(
        artifact_id=f"artifact-{transcript_id}", provider_transcript_id=transcript_id,
        provider_created_at=start, provider_end_at=end,
        provider_call_id=call_id, meeting_id=meeting,
    )


def run(candidates, *, start=START, end=END, target=TARGET, meeting=MEETING):
    return select_transcript_parts(
        candidates, scheduled_start=start, scheduled_end=end,
        target_date=target, meeting_id=meeting,
    )


def ids(result):
    return [item.candidate.provider_transcript_id for item in result.parts]


# --- constants ----------------------------------------------------------------


def test_occurrence_window_constants_match_the_legacy_export():
    assert WINDOW_BEFORE == timedelta(hours=2)
    assert WINDOW_AFTER == timedelta(hours=3)
    assert MAX_PART_GAP == timedelta(minutes=20)
    assert SELECTION_VERSION == "legacy_qa_v8_overlap_cluster_v1"


# --- step 1: Cairo business date and meeting --------------------------------


def test_only_the_target_cairo_business_date_survives():
    same_day = artifact("today", START, END)
    other_day = artifact("last-week", START - timedelta(days=7), END - timedelta(days=7))
    result = run([other_day, same_day])
    assert result.status == SELECTED
    assert ids(result) == ["today"]
    assert result.diagnostics["candidate_count_before_date_filter"] == 2
    assert result.diagnostics["same_day_candidate_count"] == 1


def test_a_recurring_series_artifact_from_another_date_is_rejected():
    """The real failure mode: a series meeting lists every occurrence."""
    series = [
        artifact(f"week-{week}", START - timedelta(days=7 * week), END - timedelta(days=7 * week))
        for week in range(1, 12)
    ]
    result = run([*series, artifact("this-week", START, END)])
    assert ids(result) == ["this-week"]
    assert result.diagnostics["candidate_count_before_date_filter"] == 12
    assert result.diagnostics["same_day_candidate_count"] == 1


def test_cairo_date_is_used_not_the_utc_date():
    """22:30Z on 2026-09-04 is already 2026-09-05 in Cairo (UTC+3)."""
    late = artifact("late", datetime(2026, 9, 4, 22, 30, tzinfo=timezone.utc),
                    datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc))
    result = select_transcript_parts(
        [late], scheduled_start=datetime(2026, 9, 4, 22, tzinfo=timezone.utc),
        scheduled_end=datetime(2026, 9, 4, 23, 59, tzinfo=timezone.utc),
        target_date=TARGET, meeting_id=MEETING)
    assert result.status == NO_SAME_DAY_CANDIDATES


def test_a_different_meeting_id_is_rejected_when_both_sides_carry_one():
    result = run([artifact("other", START, END, meeting="another-meeting")])
    assert result.status == NO_SAME_DAY_CANDIDATES


def test_a_missing_meeting_id_on_either_side_does_not_reject():
    """Legacy guard only fires when BOTH the lecture and artifact carry one."""
    assert run([artifact("blank", START, END, meeting=None)]).status == SELECTED
    assert run([artifact("blank", START, END)], meeting=None).status == SELECTED


def test_no_candidates_at_all():
    assert run([]).status == NO_CANDIDATES


# --- step 2: occurrence window ------------------------------------------------


def test_two_hour_before_boundary_is_inclusive():
    """A part ending exactly 2h before the scheduled start is still a candidate."""
    edge = artifact("edge", START - timedelta(hours=3), START - WINDOW_BEFORE)
    assert run([edge]).status == SELECTED
    just_outside = artifact(
        "outside", START - timedelta(hours=3), START - WINDOW_BEFORE - timedelta(milliseconds=1))
    assert run([just_outside]).status == NO_WINDOW_CANDIDATES


def test_three_hour_after_boundary_is_inclusive():
    edge = artifact("edge", END + WINDOW_AFTER, END + WINDOW_AFTER + timedelta(minutes=30))
    assert run([edge]).status == SELECTED
    just_outside = artifact(
        "outside", END + WINDOW_AFTER + timedelta(milliseconds=1),
        END + WINDOW_AFTER + timedelta(minutes=30))
    assert run([just_outside]).status == NO_WINDOW_CANDIDATES


# --- step 3: primary ranking --------------------------------------------------


def test_primary_is_the_greatest_overlap_not_the_earliest_or_longest():
    earliest_and_longest = artifact(
        "early-long", START - timedelta(hours=1, minutes=59),
        START + timedelta(minutes=30), call_id="call-x")
    best_overlap = artifact("best", START, END, call_id="call-y")
    result = run([earliest_and_longest, best_overlap])
    assert result.primary.candidate.provider_transcript_id == "best"
    assert result.diagnostics["primary_overlap_seconds"] == 7200.0


def test_duration_breaks_an_overlap_tie():
    # Both cover the full scheduled interval, so overlap ties at 7200s.
    shorter = artifact("shorter", START, END, call_id="call-x")
    longer = artifact("longer", START - timedelta(minutes=30), END + timedelta(minutes=30),
                      call_id="call-y")
    result = run([shorter, longer])
    assert result.diagnostics["primary_overlap_seconds"] == 7200.0
    assert result.primary.candidate.provider_transcript_id == "longer"


def test_start_distance_breaks_an_overlap_and_duration_tie():
    # Identical overlap (7200s) and identical duration (3h), different starts.
    near = artifact("near", START - timedelta(minutes=30), END + timedelta(minutes=30),
                    call_id="call-x")
    far = artifact("far", START - timedelta(hours=1), END, call_id="call-y")
    result = run([near, far])
    assert near.provider_end_at - near.provider_created_at == \
        far.provider_end_at - far.provider_created_at
    assert result.primary.candidate.provider_transcript_id == "near"
    assert result.diagnostics["primary_start_distance_seconds"] == 1800.0


def test_a_full_three_way_tie_resolves_by_stable_order_like_the_legacy_sort():
    """
    When overlap, duration AND start distance all tie, the legacy node falls
    back to list order: both of its sorts are stable. That behaviour is ported
    rather than "improved", because adding a fourth tiebreaker would silently
    diverge from legacy on exactly the ambiguous cases parity is meant to pin
    down.

    Determinism in production comes from the input order instead: the
    repository loads candidates ORDER BY provider_created_at,
    provider_transcript_id, so identical database state always ranks the same.
    """
    first = artifact("aaa", START, END, call_id="call-x")
    second = artifact("bbb", START, END, call_id="call-y")

    # Same input order always gives the same answer, however many times it runs.
    assert {run([first, second]).primary.candidate.provider_transcript_id
            for _ in range(20)} == {"aaa"}
    assert {run([second, first]).primary.candidate.provider_transcript_id
            for _ in range(20)} == {"bbb"}


def test_the_repository_ordering_makes_a_perfect_tie_deterministic():
    """The ORDER BY the selection repository uses, applied to a tie."""
    unordered = [artifact("bbb", START, END, call_id="call-y"),
                 artifact("aaa", START, END, call_id="call-x")]
    as_loaded = sorted(
        unordered, key=lambda c: (c.provider_created_at, c.provider_transcript_id))
    assert run(as_loaded).primary.candidate.provider_transcript_id == "aaa"


# --- step 4: part attachment --------------------------------------------------


def test_same_call_id_attaches_even_when_far_apart():
    primary = artifact("primary", START, END, call_id="call-same")
    distant = artifact("distant", END + timedelta(hours=2), END + timedelta(hours=2, minutes=30),
                       call_id="call-same")
    result = run([primary, distant])
    assert ids(result) == ["primary", "distant"]


def test_a_different_call_within_twenty_minutes_attaches():
    primary = artifact("primary", START, START + timedelta(minutes=60), call_id="call-1")
    restarted = artifact("restarted", START + timedelta(minutes=75),
                         START + timedelta(minutes=110), call_id="call-2")
    result = run([primary, restarted])
    assert ids(result) == ["primary", "restarted"]
    assert result.diagnostics["selected_call_ids"] == ["call-1", "call-2"]


def test_exactly_twenty_minutes_attaches_and_beyond_does_not():
    primary = artifact("primary", START, START + timedelta(minutes=60), call_id="call-1")
    at_limit = artifact("at-limit", START + timedelta(minutes=80),
                        START + timedelta(minutes=90), call_id="call-2")
    assert ids(run([primary, at_limit])) == ["primary", "at-limit"]

    beyond = artifact("beyond", START + timedelta(minutes=80, seconds=1),
                      START + timedelta(minutes=90), call_id="call-3")
    assert ids(run([primary, beyond])) == ["primary"]


def test_cluster_expansion_is_iterative_not_single_pass():
    """
    Part C is 35 minutes from the primary, so a single pass would drop it.
    Attaching B extends the cluster and brings C within 20 minutes.
    """
    primary = artifact("A", START, START + timedelta(minutes=30), call_id="call-1")
    middle = artifact("B", START + timedelta(minutes=45), START + timedelta(minutes=70),
                      call_id="call-2")
    tail = artifact("C", START + timedelta(minutes=85), START + timedelta(minutes=110),
                    call_id="call-3")
    # C alone is 55 minutes past the primary's end - far beyond the 20-minute gap.
    assert tail.provider_created_at - primary.provider_end_at == timedelta(minutes=55)
    result = run([primary, middle, tail])
    assert ids(result) == ["A", "B", "C"]


def test_a_part_beyond_the_expanded_cluster_still_stays_out():
    primary = artifact("A", START, START + timedelta(minutes=30), call_id="call-1")
    middle = artifact("B", START + timedelta(minutes=45), START + timedelta(minutes=70),
                      call_id="call-2")
    stranger = artifact("Z", START + timedelta(minutes=120), START + timedelta(minutes=130),
                        call_id="call-9")
    assert ids(run([primary, middle, stranger])) == ["A", "B"]


# --- ordering and identity ----------------------------------------------------


def test_selected_parts_are_ordered_chronologically():
    late = artifact("late", START + timedelta(minutes=70), START + timedelta(minutes=110),
                    call_id="call-2")
    early = artifact("early", START, START + timedelta(minutes=60), call_id="call-1")
    result = run([late, early])
    assert ids(result) == ["early", "late"]
    assert result.diagnostics["selected_part_ids"] == ["early", "late"]


def test_primary_id_and_individual_part_ids_are_preserved_separately():
    primary = artifact("primary", START, END, call_id="call-1")
    extra = artifact("extra", END + timedelta(minutes=5), END + timedelta(minutes=20),
                     call_id="call-2")
    result = run([primary, extra])
    assert result.diagnostics["primary_transcript_id"] == "primary"
    assert result.diagnostics["selected_part_ids"] == ["primary", "extra"]
    assert len(result.diagnostics["selected_part_ids"]) == 2


def test_session_timing_and_difference_minutes():
    primary = artifact("primary", START - timedelta(minutes=2), END + timedelta(minutes=13))
    result = run([primary])
    assert result.diagnostics["actual_start"] == (START - timedelta(minutes=2)).isoformat()
    assert result.diagnostics["actual_end"] == (END + timedelta(minutes=13)).isoformat()
    assert result.diagnostics["start_difference_minutes"] == -2
    assert result.diagnostics["end_difference_minutes"] == 13
    assert result.diagnostics["start_status"] == "Early"
    assert result.diagnostics["end_status"] == "Overrun"


def test_half_minute_rounding_matches_javascript_math_round():
    """JS Math.round is half-up, including for negatives; Python's is not."""
    primary = artifact("primary", START - timedelta(seconds=30), END + timedelta(seconds=30))
    result = run([primary])
    assert result.diagnostics["start_difference_minutes"] == 0     # -0.5 -> 0
    assert result.diagnostics["end_difference_minutes"] == 1       # +0.5 -> 1


# --- invalid metadata safety ---------------------------------------------------


def test_missing_end_time_falls_back_to_the_start():
    bare = CandidateArtifact(
        artifact_id="a", provider_transcript_id="bare", provider_created_at=START,
        provider_end_at=None, provider_call_id="call-1", meeting_id=MEETING)
    result = run([bare])
    assert result.status == SELECTED
    assert result.diagnostics["primary_duration_seconds"] == 0.0


def test_an_end_before_the_start_is_clamped_not_negative():
    reversed_times = artifact("reversed", START, START - timedelta(hours=1))
    result = run([reversed_times])
    assert result.diagnostics["primary_duration_seconds"] == 0.0


def test_a_candidate_without_a_created_time_is_ignored():
    broken = CandidateArtifact(
        artifact_id="a", provider_transcript_id="broken", provider_created_at=None,
        provider_end_at=None, provider_call_id=None, meeting_id=MEETING)
    assert run([broken]).status == NO_SAME_DAY_CANDIDATES


def test_a_non_positive_scheduled_interval_is_rejected():
    with pytest.raises(ValueError, match="scheduled_end must be after"):
        run([artifact("x", START, END)], start=END, end=START)
