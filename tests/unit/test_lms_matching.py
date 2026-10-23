"""LMS matching v2 (meeting_key_start_v2): meeting keys, every rule, shared modules, coverage."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.repositories.publisher_queue import retry_delay
from app.lms import coverage, meeting_key
from app.lms.matching import (
    AMBIGUOUS, CONFLICT, LI_DUPLICATE, LMS_RECORD_INACTIVE, NO_LMS_SESSION, NO_MEETING_KEY, PRIMARY,
    RESCHEDULED_CANDIDATE, SHARED_MODULE, TEST_EXCLUDED, VERIFIED, MatchConfig, match_lecture,
)
from tests.unit.lms_fakes import (
    START, channel_url, lecture, occurrence, online_meeting_id, private_url, snapshot,
)

CONFIG = MatchConfig.from_text("Final Test;Test Lms")


# -- meeting key -----------------------------------------------------------------

def test_private_and_channel_keys_from_urls_and_meeting_ids():
    assert meeting_key.from_join_url(private_url()) == "19:meeting_SYNTHETICa1@thread.v2/0"
    assert meeting_key.from_join_url(channel_url()) == "19:SYNTHchannel01@thread.tacv2/1789000000001"
    assert meeting_key.from_online_meeting_id(online_meeting_id()) == "19:meeting_SYNTHETICa1@thread.v2/0"
    channel_mid = online_meeting_id("19:SYNTHchannel01@thread.tacv2", 1789000000001)
    assert meeting_key.from_online_meeting_id(channel_mid) == meeting_key.from_join_url(channel_url())


def test_key_ignores_query_and_survives_double_encoding():
    plain = "https://teams.microsoft.com/l/meetup-join/19:meeting_SYNTHETICa1@thread.v2/0?context=other"
    double = private_url().replace("%3a", "%253a").replace("%40", "%2540")
    assert meeting_key.from_join_url(plain) == meeting_key.from_join_url(double) == meeting_key.from_join_url(private_url())


@pytest.mark.parametrize("value", [None, "", "not a url", "https://example.invalid/x", "https://teams.microsoft.com/meet/123?p=abc"])
def test_unparsable_urls_have_no_key(value):
    assert meeting_key.from_join_url(value) is None


@pytest.mark.parametrize("value", [None, "", "%%%", "bm90IGEgbWVldGluZyBpZA"])
def test_unparsable_meeting_ids_have_no_key(value):
    assert meeting_key.from_online_meeting_id(value) is None


def test_channel_meetings_in_one_channel_never_collide():
    assert meeting_key.from_join_url(channel_url(message=1)) != meeting_key.from_join_url(channel_url(message=2))


def test_key_hash_is_short_and_not_the_key():
    digest = meeting_key.key_hash("19:meeting_SYNTHETICa1@thread.v2/0")
    assert len(digest) == 16 and "thread" not in digest
    assert meeting_key.key_hash(None) is None


# -- rule outcomes ------------------------------------------------------------------

def test_exactly_one_active_occurrence_verifies():
    occ = occurrence()
    result = match_lecture(lecture(), snapshot(occ), CONFIG)
    assert result.status == VERIFIED and not result.requires_review
    assert [(l.occurrence_id, l.role) for l in result.links] == [(occ.occurrence_id, PRIMARY)]


def test_cancelled_twin_at_the_same_start_is_ignored():
    keep = occurrence(sid="LIVE-SERIES")
    occurrence_cancelled = occurrence(sid="LIVE-SERIES", status="cancelled")
    result = match_lecture(lecture(), snapshot(keep, occurrence_cancelled), CONFIG)
    assert result.status == VERIFIED and result.primary.occurrence_id == keep.occurrence_id
    assert result.evidence["excluded_cancelled"] == 1


@pytest.mark.parametrize("series_status", ["superseded", "cancelled", "feedback-test"])
def test_inactive_series_sharing_the_meeting_is_ignored(series_status):
    active = occurrence(module="MOD-A")
    old = occurrence(module="MOD-A", series_status=series_status)
    result = match_lecture(lecture(), snapshot(active, old), CONFIG)
    assert result.status == VERIFIED and result.primary.occurrence_id == active.occurrence_id


def test_week_meeting_series_is_active():
    occ = occurrence(series_status="week-meeting", meeting_type="week-additional")
    assert match_lecture(lecture(), snapshot(occ), CONFIG).status == VERIFIED


def test_only_inactive_rows_at_the_start_are_visible_not_dropped():
    occ = occurrence(status="cancelled")
    result = match_lecture(lecture(), snapshot(occ), CONFIG)
    assert result.status == LMS_RECORD_INACTIVE and result.requires_review
    assert result.candidate_ids == (occ.occurrence_id,) and result.links == ()


def test_deleted_module_is_inactive_and_a_deleted_test_module_is_test_excluded():
    assert match_lecture(lecture(), snapshot(occurrence(module_deleted=True)), CONFIG).status == LMS_RECORD_INACTIVE
    deleted_test = occurrence(module_deleted=True, programme="Test Lms")
    assert match_lecture(lecture(), snapshot(deleted_test), CONFIG).status == TEST_EXCLUDED


def test_configured_test_programme_is_excluded_but_kept_as_evidence():
    occ = occurrence(programme="Final Test")
    result = match_lecture(lecture(), snapshot(occ), CONFIG)
    assert result.status == TEST_EXCLUDED and not result.requires_review and result.links == ()
    assert result.candidate_ids == (occ.occurrence_id,)


def test_test_programme_names_are_exact_not_substrings():
    occ = occurrence(programme="Final Test Programme for Real")
    assert match_lecture(lecture(), snapshot(occ), CONFIG).status == VERIFIED


def test_production_candidate_wins_over_a_test_twin():
    prod = occurrence(module="MOD-P")
    test = occurrence(module="MOD-T", programme="Final Test")
    result = match_lecture(lecture(), snapshot(prod, test), CONFIG)
    assert result.status == VERIFIED and result.primary.occurrence_id == prod.occurrence_id
    assert result.evidence["test_candidates"] == [test.occurrence_id]


def test_li_suppressed_duplicate_is_never_matched():
    result = match_lecture(lecture(suppressed=True), snapshot(occurrence()), CONFIG)
    assert result.status == LI_DUPLICATE and result.links == () and not result.requires_review


def test_lecture_without_a_meeting_key():
    result = match_lecture(lecture(url="https://example.invalid/none"), snapshot(occurrence()), CONFIG)
    assert result.status == NO_MEETING_KEY and result.requires_review


def test_channel_meeting_missing_from_lms_and_same_time_strangers_are_not_evidence():
    stranger = occurrence(url=private_url("19:meeting_OTHERb2@thread.v2"))   # same start, other meeting
    result = match_lecture(lecture(url=channel_url()), snapshot(stranger), CONFIG)
    assert result.status == NO_LMS_SESSION and result.candidate_ids == () and result.requires_review


def test_linked_external_channel_meeting_verifies():
    occ = occurrence(url=channel_url(), meeting_type="linked-external",
                     mid=online_meeting_id("19:SYNTHchannel01@thread.tacv2", 1789000000001))
    assert match_lecture(lecture(url=channel_url()), snapshot(occ), CONFIG).status == VERIFIED


def test_rescheduled_within_seven_days_is_a_candidate_never_a_match():
    occ = occurrence(start=START + timedelta(days=2))
    result = match_lecture(lecture(), snapshot(occ), CONFIG)
    assert result.status == RESCHEDULED_CANDIDATE and result.candidate_ids == (occ.occurrence_id,)
    far = occurrence(start=START + timedelta(days=8))
    assert match_lecture(lecture(), snapshot(far), CONFIG).status == NO_LMS_SESSION


def test_one_minute_off_is_not_the_same_start():
    occ = occurrence(start=START + timedelta(minutes=1))
    assert match_lecture(lecture(), snapshot(occ), CONFIG).status == RESCHEDULED_CANDIDATE


def test_two_active_occurrences_without_a_share_are_ambiguous_and_never_first():
    a, b = occurrence(module="MOD-A"), occurrence(module="MOD-B")
    result = match_lecture(lecture(), snapshot(a, b), CONFIG)
    assert result.status == AMBIGUOUS and result.links == ()
    assert set(result.candidate_ids) == {a.occurrence_id, b.occurrence_id}


def test_two_occurrences_in_one_module_are_ambiguous_even_with_shares():
    a, b = occurrence(module="MOD-A"), occurrence(module="MOD-A")
    assert match_lecture(lecture(), snapshot(a, b, shares={"MOD-B": "MOD-A"}), CONFIG).status == AMBIGUOUS


def test_module_teams_share_maps_one_recording_to_several_occurrences():
    source = occurrence(module="MOD-SRC")
    shared_1 = occurrence(module="MOD-S1")
    shared_2 = occurrence(module="MOD-S2")
    shares = {"MOD-S1": "MOD-SRC", "MOD-S2": "MOD-SRC"}
    result = match_lecture(lecture(), snapshot(shared_1, source, shared_2, shares=shares), CONFIG)
    assert result.status == VERIFIED
    assert result.primary.occurrence_id == source.occurrence_id
    assert sorted((l.occurrence_id, l.role, l.shared_source_module_id) for l in result.links if l.role == SHARED_MODULE) \
        == sorted([(shared_1.occurrence_id, SHARED_MODULE, "MOD-SRC"), (shared_2.occurrence_id, SHARED_MODULE, "MOD-SRC")])


@pytest.mark.parametrize("shares", [
    {"MOD-S1": "MOD-ELSEWHERE"},               # shared from a module that is not a candidate
    {"MOD-S1": "MOD-SRC", "MOD-SRC": "MOD-X"},  # the root is itself shared
    {},                                         # no share at all
])
def test_shares_that_do_not_tie_the_candidates_stay_ambiguous(shares):
    source, shared = occurrence(module="MOD-SRC"), occurrence(module="MOD-S1")
    assert match_lecture(lecture(), snapshot(source, shared, shares=shares), CONFIG).status == AMBIGUOUS


def test_moduleless_series_cannot_be_resolved_by_shares():
    a, b = occurrence(module=None), occurrence(module="MOD-SRC")
    assert match_lecture(lecture(), snapshot(a, b, shares={"MOD-X": "MOD-SRC"}), CONFIG).status == AMBIGUOUS


def test_contradicting_online_meeting_id_is_a_conflict():
    lms_mid = online_meeting_id("19:meeting_OTHERb2@thread.v2")
    occ = occurrence(mid=lms_mid)   # join URL key matches the lecture, its meeting id does not
    result = match_lecture(lecture(mid=online_meeting_id()), snapshot(occ), CONFIG)
    assert result.status == CONFLICT and result.requires_review


def test_matching_online_meeting_id_from_another_organizer_is_not_a_conflict():
    occ = occurrence(mid=online_meeting_id(organizer="00000000-0000-4000-8000-000000000002"))
    assert match_lecture(lecture(mid=online_meeting_id()), snapshot(occ), CONFIG).status == VERIFIED


def test_decision_hash_is_deterministic_and_order_free():
    a, b = occurrence(module="MOD-A"), occurrence(module="MOD-B")
    lec = lecture()
    first = match_lecture(lec, snapshot(a, b), CONFIG)
    second = match_lecture(lec, snapshot(b, a), CONFIG)
    assert first.decision_sha256 == second.decision_sha256
    assert first.decision_sha256 != match_lecture(lec, snapshot(a), CONFIG).decision_sha256


def test_evidence_never_carries_urls_or_meeting_ids():
    occ = occurrence(mid=online_meeting_id())
    for snap in (snapshot(occ), snapshot(), snapshot(occ, occurrence(module="MOD-B"))):
        text = repr(match_lecture(lecture(mid=online_meeting_id()), snap, CONFIG).evidence)
        assert "http" not in text and "thread" not in text and online_meeting_id() not in text


def test_config_from_text():
    config = MatchConfig.from_text(" Final Test ;\nTest Lms;; ")
    assert config.test_programmes == frozenset({"Final Test", "Test Lms"})
    assert MatchConfig.from_text(None).test_programmes == frozenset()


# -- LMS-side coverage -----------------------------------------------------------

def test_coverage_states():
    now = START + timedelta(days=3)
    cases = {
        coverage.INACTIVE: occurrence(status="cancelled"),
        coverage.TEST: occurrence(programme="Final Test"),
        coverage.MATCHED: occurrence(),
        coverage.NOT_YET_DUE: occurrence(start=now - timedelta(minutes=30)),
        coverage.LMS_ONLY_PENDING: occurrence(start=now - timedelta(hours=6)),
        coverage.LMS_SESSION_NOT_DISCOVERED: occurrence(start=now - timedelta(days=2),
                                                        series_status="week-meeting", meeting_type="week-additional"),
    }
    owners = {cases[coverage.MATCHED].occurrence_id: "lecture-1"}
    rows = {r.occurrence.occurrence_id: r for r in coverage.classify_snapshot(snapshot(*cases.values()), owners, CONFIG, now)}
    for expected, occ in cases.items():
        assert rows[occ.occurrence_id].coverage_status == expected
    assert rows[cases[coverage.MATCHED].occurrence_id].matched_lecture_id == "lecture-1"
    assert rows[cases[coverage.LMS_SESSION_NOT_DISCOVERED].occurrence_id].evidence["meeting_type"] == "week-additional"


def test_coverage_never_marks_a_test_or_inactive_occurrence_as_matched():
    occ = occurrence(programme="Final Test")
    row = coverage.classify_occurrence(occ, "lecture-1", CONFIG, START)
    assert row.coverage_status == coverage.TEST and row.matched_lecture_id is None


def test_retry_delay_schedule():
    assert [retry_delay(n) for n in (1, 2, 3, 4, 5, 9)] == [
        timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1),
        timedelta(hours=3), timedelta(hours=3)]
