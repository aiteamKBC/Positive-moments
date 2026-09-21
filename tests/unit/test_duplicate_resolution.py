"""
Phase 4C1 unit tests: the deterministic duplicate rule itself.

These test the RULE, not the database. `classify_group` is pure - rows in, a
decision out - so every awkward shape can be written down directly: two valid
meetings, two events sharing one meeting, an ambiguous sibling, a group that
differs only in module, a group that differs only by a minute.

The tests that matter most here are the negative ones. An automation that
retires a lecture is only safe if the set of shapes it will act on is small and
provably closed, so most of this file is about the shapes it must refuse.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.lectures.duplicate_service import (
    ALREADY_SUPPRESSED,
    NOT_APPLICABLE,
    REFUSED_DOWNSTREAM_FOOTPRINT,
    REFUSED_RACED,
    SUPPRESSED,
    DuplicateResolutionService,
)
from app.lectures.duplicates import (
    CASE_MULTIPLE_VALID_MEETINGS,
    CASE_NONE_RESOLVED,
    CASE_NOT_DUPLICATE,
    CASE_SHARED_MEETING,
    CASE_SUPPRESSED,
    CASE_UNRESOLVED_NOT_SUPPRESSIBLE,
    DUPLICATE_RESOLUTION_VERSION,
    SUPPRESSION_KEY,
    classify_group,
    group_key,
    group_rows,
    is_confidently_resolved,
    is_suppressible,
)


SESSION_DATE = date(2026, 9, 18)
START = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
NOW = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)

WINNER_ID = "ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6"
LOSER_ID = "26e74d25-ea3f-5e3d-ab08-19ab949eb75f"


def row(lecture_id, *, meeting_id=None, mapping="ONLINE_MEETING_NOT_FOUND",
        organizer="NOT_ATTEMPTED", downstream_ready=False, module="Ray-MSP",
        normalized="ray-msp", start=START, end=END, cancelled=False,
        suppression=None, subject="Ray-MSP"):
    return {"lecture_id": lecture_id,
            "calendar_event_id": f"event-{lecture_id[:4]}",
            "i_cal_uid": f"ical-{lecture_id[:4]}", "meeting_id": meeting_id,
            "calendar_mapping_status": mapping,
            "organizer_validation_status": organizer,
            "discovery_status": "READY" if meeting_id else "REVIEW",
            "downstream_ready": downstream_ready, "is_cancelled": cancelled,
            "session_date": SESSION_DATE, "normalized_subject": normalized,
            "module": module, "scheduled_start": start, "scheduled_end": end,
            "subject": subject, "suppression": suppression}


def winner(**overrides):
    defaults = {"meeting_id": "meeting-real", "mapping": "RESOLVED",
                "organizer": "ORGANIZER_ID_CONFIRMED", "downstream_ready": True}
    return row(WINNER_ID, **{**defaults, **overrides})


def loser(**overrides):
    return row(LOSER_ID, **overrides)


# --- 1. the one shape that is automated --------------------------------------

def test_1_one_resolved_and_one_unresolved_sibling_suppresses_the_unresolved_one():
    decision = classify_group([loser(), winner()], now=NOW)
    assert decision["case"] == CASE_SUPPRESSED
    assert decision["winner"]["lecture_id"] == WINNER_ID
    assert [item["lecture_id"] for item in decision["suppress"]] == [LOSER_ID]
    assert decision["requires_manual_review"] is False


def test_1b_the_order_rows_arrive_in_does_not_change_the_answer():
    """A rule whose answer depends on row order is not deterministic."""
    forward = classify_group([loser(), winner()], now=NOW)
    backward = classify_group([winner(), loser()], now=NOW)
    assert forward["case"] == backward["case"] == CASE_SUPPRESSED
    assert forward["winner"]["lecture_id"] == backward["winner"]["lecture_id"]
    assert [item["lecture_id"] for item in forward["suppress"]] == \
        [item["lecture_id"] for item in backward["suppress"]]


def test_2_the_winner_is_never_in_the_suppression_list():
    decision = classify_group([loser(), winner()], now=NOW)
    assert WINNER_ID not in [item["lecture_id"] for item in decision["suppress"]]


# --- 8, 9. the shapes that stay with a human ---------------------------------

def test_8_two_different_valid_meetings_require_manual_review():
    decision = classify_group(
        [winner(), row(LOSER_ID, meeting_id="meeting-other", mapping="RESOLVED",
                       organizer="ORGANIZER_ID_CONFIRMED",
                       downstream_ready=True)], now=NOW)
    assert decision["case"] == CASE_MULTIPLE_VALID_MEETINGS
    assert decision["requires_manual_review"] is True
    assert decision["suppress"] == []


def test_8b_two_events_holding_the_SAME_meeting_also_require_manual_review():
    decision = classify_group(
        [winner(), row(LOSER_ID, meeting_id="meeting-real", mapping="RESOLVED",
                       organizer="ORGANIZER_ID_CONFIRMED",
                       downstream_ready=True)], now=NOW)
    assert decision["case"] == CASE_SHARED_MEETING
    assert decision["suppress"] == []


def test_9_neither_sibling_resolved_means_nothing_is_suppressed():
    decision = classify_group([loser(), row(WINNER_ID)], now=NOW)
    assert decision["case"] == CASE_NONE_RESOLVED
    assert decision["suppress"] == []
    assert decision["requires_manual_review"] is True


def test_9b_an_ambiguous_sibling_refuses_the_whole_group():
    """
    AMBIGUOUS_ONLINE_MEETING is evidence that a real meeting EXISTS and we
    declined to pick between candidates. That is the opposite of the emptiness
    the rule needs, so it is not suppressible - and it disqualifies the group
    rather than being quietly skipped.
    """
    decision = classify_group(
        [winner(), loser(mapping="AMBIGUOUS_ONLINE_MEETING")], now=NOW)
    assert decision["case"] == CASE_UNRESOLVED_NOT_SUPPRESSIBLE
    assert decision["suppress"] == []


def test_a_meeting_without_a_confirmed_organizer_is_not_a_winner():
    decision = classify_group(
        [loser(), winner(organizer="ORGANIZER_ID_UNAVAILABLE")], now=NOW)
    assert decision["case"] == CASE_NONE_RESOLVED


def test_the_pre_migration_002_spelling_of_resolved_still_wins():
    """Historical rows say EXACT_JOIN_URL_MATCH for exactly the same fact."""
    decision = classify_group(
        [loser(), winner(mapping="EXACT_JOIN_URL_MATCH")], now=NOW)
    assert decision["case"] == CASE_SUPPRESSED


@pytest.mark.parametrize("mapping", [
    "ONLINE_MEETING_NOT_FOUND", "NOT_ATTEMPTED", "ORGANIZER_NOT_RESOLVABLE",
    "FORBIDDEN_FOR_ORGANIZER"])
def test_every_final_unresolved_state_is_suppressible(mapping):
    assert is_suppressible(loser(mapping=mapping)) is True


def test_a_row_with_a_meeting_id_is_never_suppressible():
    assert is_suppressible(loser(meeting_id="meeting-x")) is False


def test_an_unknown_mapping_status_is_neither_resolved_nor_suppressible():
    """Fail closed: a status nobody has thought about is nobody's winner."""
    unknown = loser(mapping="SOME_FUTURE_STATUS")
    assert is_confidently_resolved(unknown) is False
    assert is_suppressible(unknown) is False


# --- 10, 11. what is NOT a duplicate group -----------------------------------

def test_10_a_different_module_is_a_different_group():
    assert group_key(winner()) != group_key(loser(module="Other Module"))
    groups = group_rows([winner(), loser(module="Other Module")])
    assert len(groups) == 2
    for members in groups.values():
        assert classify_group(members, now=NOW)["case"] == CASE_NOT_DUPLICATE


def test_11_a_different_start_time_is_a_different_group():
    assert group_key(winner()) != group_key(loser(start=START + timedelta(minutes=1)))


def test_11b_a_different_end_time_is_a_different_group():
    """
    Same start, different duration. Deliberately not grouped: "compatible
    duration" is implemented as equality, because a tolerance window is a
    fuzzy rule wearing a precise costume.
    """
    assert group_key(winner()) != group_key(loser(end=END + timedelta(minutes=30)))


def test_a_different_normalized_subject_is_a_different_group():
    assert group_key(winner()) != group_key(loser(normalized="something else"))


def test_titles_that_merely_look_similar_are_never_grouped():
    """No fuzzy matching anywhere. Normalization is exact equality or nothing."""
    assert group_key(winner(normalized="ray-msp jan 2026")) != \
        group_key(loser(normalized="ray-msp jan 2026 "))


def test_a_lone_occurrence_is_not_a_duplicate_group():
    assert classify_group([winner()], now=NOW)["case"] == CASE_NOT_DUPLICATE


def test_a_cancelled_occurrence_is_not_a_duplicate_of_a_live_one():
    decision = classify_group([winner(), loser(cancelled=True)], now=NOW)
    assert decision["case"] == CASE_NOT_DUPLICATE
    assert decision["suppress"] == []


# --- 12. provenance -----------------------------------------------------------

def test_12_the_suppressed_row_points_at_the_winner_in_both_directions():
    decision = classify_group([loser(), winner()], now=NOW)
    annotation = decision["suppress"][0]["annotation"]
    assert annotation["winner_lecture_id"] == WINNER_ID
    assert annotation["suppressed_lecture_id"] == LOSER_ID
    assert annotation["winner_meeting_id"] == "meeting-real"
    assert annotation["duplicate_resolution_version"] == DUPLICATE_RESOLUTION_VERSION
    assert annotation["duplicate_resolution_reason"] == CASE_SUPPRESSED
    assert annotation["resolved_at"] == NOW.isoformat()


def test_12b_both_calendar_identities_survive_in_the_provenance():
    """
    Nothing is deleted, so the annotation has to carry enough to reverse the
    decision: which event won, which event lost, and both iCalUIDs.
    """
    annotation = classify_group([loser(), winner()],
                                now=NOW)["suppress"][0]["annotation"]
    assert annotation["winner_calendar_event_id"] != \
        annotation["suppressed_calendar_event_id"]
    assert annotation["winner_i_cal_uid"] != annotation["suppressed_i_cal_uid"]
    assert annotation["group_key"]["scheduled_start"] == START.isoformat()


def test_no_module_in_this_package_deletes_anything():
    """
    Structural, and worth stating: "no calendar event is ever deleted" is the
    promise this whole phase rests on.
    """
    import inspect

    import app.db.repositories.lecture_duplicates as repository
    import app.lectures.duplicate_service as service
    import app.lectures.duplicates as rule
    for module in (rule, service, repository):
        source = inspect.getsource(module).upper()
        assert "DELETE FROM" not in source, module.__name__
        assert "DROP " not in source, module.__name__


# --- the service: dry run, guards, idempotency --------------------------------

class StubRepository:
    def __init__(self, group, *, footprint=None, write_succeeds=True):
        self.group = group
        self.footprint = footprint or dict.fromkeys(
            ("transcript_candidates", "canonical_documents", "qa_evaluations",
             "legacy_qa_writes", "perfect_writes"), 0)
        self.write_succeeds = write_succeeds
        self.writes = []

    def load_group_for_lecture(self, connection, lecture_id):
        return self.group

    def load_day(self, connection, session_date):
        return self.group

    def load_all_duplicate_groups(self, connection):
        return self.group

    def downstream_footprint(self, connection, lecture_id):
        return self.footprint

    def suppress(self, connection, lecture_id, annotation):
        self.writes.append((str(lecture_id), annotation))
        return self.write_succeeds


def service_for(group, **kwargs):
    repository = StubRepository(group, **kwargs)
    return DuplicateResolutionService(repository=repository, now=NOW), repository


def test_the_default_is_a_dry_run_that_writes_nothing():
    service, repository = service_for([loser(), winner()])
    outcome = service.resolve_lecture(None, LOSER_ID)
    assert outcome["outcome"] == SUPPRESSED
    assert outcome["would_suppress"] is True
    assert outcome["persisted"] is False
    assert repository.writes == []


def test_executing_writes_exactly_one_annotation():
    service, repository = service_for([loser(), winner()])
    outcome = service.resolve_lecture(None, LOSER_ID, persist=True)
    assert outcome["persisted"] is True
    assert outcome["suppressed_count"] == 1
    assert len(repository.writes) == 1
    assert repository.writes[0][0] == LOSER_ID


def test_the_winner_is_never_written_to():
    service, repository = service_for([loser(), winner()])
    outcome = service.resolve_lecture(None, WINNER_ID, persist=True)
    assert outcome["outcome"] == NOT_APPLICABLE
    assert outcome["role"] == "WINNER"
    assert repository.writes == []


def test_a_group_needing_review_is_never_written_to():
    service, repository = service_for(
        [winner(), row(LOSER_ID, meeting_id="other", mapping="RESOLVED",
                       organizer="ORGANIZER_ID_CONFIRMED")])
    outcome = service.resolve_lecture(None, LOSER_ID, persist=True)
    assert outcome["requires_manual_review"] is True
    assert repository.writes == []


def test_a_candidate_with_downstream_work_is_refused_outright():
    """
    The rule should never select such a row - an occurrence with no meeting
    cannot have a transcript - but "should never" is not a guarantee, and this
    check is what turns it into one.
    """
    service, repository = service_for(
        [loser(), winner()],
        footprint={"transcript_candidates": 1, "canonical_documents": 0,
                   "qa_evaluations": 0, "legacy_qa_writes": 0,
                   "perfect_writes": 0})
    outcome = service.resolve_lecture(None, LOSER_ID, persist=True)
    assert outcome["outcome"] == REFUSED_DOWNSTREAM_FOOTPRINT
    assert outcome["requires_manual_review"] is True
    assert repository.writes == []


def test_a_row_that_gained_a_meeting_between_decision_and_write_is_not_suppressed():
    """The UPDATE carries its own `meeting_id IS NULL` guard; this is it."""
    service, _ = service_for([loser(), winner()], write_succeeds=False)
    outcome = service.resolve_lecture(None, LOSER_ID, persist=True)
    assert outcome["outcome"] == REFUSED_RACED
    assert outcome["persisted"] is False


# --- 13. idempotency -----------------------------------------------------------

def test_13_a_second_run_over_a_settled_group_writes_nothing():
    settled = loser(suppression={"winner_lecture_id": WINNER_ID})
    service, repository = service_for([settled, winner()])
    outcome = service.resolve_lecture(None, LOSER_ID, persist=True)
    assert outcome["outcome"] == ALREADY_SUPPRESSED
    assert outcome["suppressed_count"] == 0
    assert repository.writes == []


def test_13b_a_settled_group_reports_no_outstanding_work():
    settled = loser(suppression={"winner_lecture_id": WINNER_ID})
    service, _ = service_for([settled, winner()])
    plan = service.plan_day(None, SESSION_DATE)
    assert plan["duplicate_group_count"] == 1
    assert plan["auto_suppressible_count"] == 0
    assert plan["already_suppressed_count"] == 1


def test_an_unsettled_group_does_report_outstanding_work():
    service, _ = service_for([loser(), winner()])
    plan = service.plan_day(None, SESSION_DATE)
    assert plan["auto_suppressible_count"] == 1
    assert plan["groups"][0]["pending_suppression"] == [LOSER_ID]


def test_the_classification_still_describes_a_settled_group_honestly():
    """
    Acting on a decision does not change what the calendar looks like. A
    settled group is still a Case A group, and re-deciding it is always safe.
    """
    settled = loser(suppression={"winner_lecture_id": WINNER_ID})
    service, _ = service_for([settled, winner()])
    decision = service.classify_lecture(None, LOSER_ID)
    assert decision["case"] == CASE_SUPPRESSED
    assert decision["already_suppressed"] is True
    assert decision["role"] == "SUPPRESSIBLE_DUPLICATE"


# --- the historical scan --------------------------------------------------------

def test_the_registry_wide_scan_only_reads():
    service, repository = service_for([loser(), winner()])
    plan = service.plan_all(None)
    assert plan["scope"] == "ALL"
    assert plan["duplicate_group_count"] == 1
    assert repository.writes == []


def test_the_annotation_is_stored_under_exactly_one_metadata_key():
    """
    One key, because the discovery upsert has to carry it across by name. A
    second key would be silently dropped by the next nightly rediscovery.
    """
    assert SUPPRESSION_KEY == "duplicate_suppression"
