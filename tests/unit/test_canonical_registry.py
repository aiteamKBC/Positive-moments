"""
Canonical registry scope.

public.lecture_sessions holds KBC LECTURES, not every Teams calendar event. A
calendar occurrence becomes a row only when it is a non-cancelled Teams event
whose normalized subject exactly matches an ACTIVE Aptem group. Everything else
is discovery-run audit evidence.
"""
from datetime import date, datetime, timezone

import pytest

from app.common.errors import PlatformError
from app.graph.meetings import (
    DISCOVERY_MAILBOX_CONTEXT,
    ONLINE_MEETING_NOT_FOUND,
    ORGANIZER_ID_CONFIRMED,
    RESOLVED,
)
from app.db.repositories.lecture_sessions import LectureSessionRepository
from app.lectures.models import CalendarDiscoveryBatch, CalendarEvent, MeetingResolution
from app.lectures.service import LectureDiscoveryService

from tests.unit.test_service import Calendar, Meetings, Registry, Runs, service


def _event(event_id, subject, hour=8, minute=0, organizer="Cohort@example.invalid"):
    return CalendarEvent(
        event_id, f"ical-{event_id}", subject,
        datetime(2026, 9, 4, hour, minute, tzinfo=timezone.utc),
        datetime(2026, 9, 4, hour + 2, minute, tzinfo=timezone.utc),
        "UTC", f"https://teams.microsoft.com/l/meetup-join/{event_id}",
        calendar_organizer_address=organizer,
    )


class ManyEvents(Calendar):
    def __init__(self, events): self.events = events
    def discover_calendar_events(self, target_date):
        return CalendarDiscoveryBatch(len(self.events), self.events)


class Groups:
    def __init__(self, groups): self.groups = groups
    def load_active_groups(self, connection): return self.groups


def test_unmatched_teams_events_are_not_canonical_lectures():
    events = [_event("a", "Ray-Project Management Office (PMO)"),
              _event("b", "Weekly Meeting", hour=15),
              _event("c", "hhj"),
              _event("d", "Test Module")]
    registry, runs, meetings = Registry(), Runs(), Meetings()
    summary = service(
        registry, runs, calendar=ManyEvents(events), meetings=meetings,
        aptem=Groups(["Ray-Project Management Office (PMO)"]),
    ).discover_day(object(), object(), date(2026, 9, 4))

    assert summary["teams_events_found"] == 4
    assert summary["canonical_lecture_candidates"] == 1
    assert summary["non_canonical_calendar_events"] == 3
    # Only the Aptem-matched occurrence was persisted...
    assert [row.subject for row in registry.rows.values()] == ["Ray-Project Management Office (PMO)"]
    # ...and the rest survive only as run diagnostics.
    excluded = {item["subject"] for item in summary["non_canonical_calendar_event_details"]}
    assert excluded == {"Weekly Meeting", "hhj", "Test Module"}
    assert all(item["exclusion_reason"] == "NOT_AN_ACTIVE_APTEM_GROUP"
               for item in summary["non_canonical_calendar_event_details"])


def test_unmatched_event_never_triggers_a_graph_meeting_lookup():
    registry, runs, meetings = Registry(), Runs(), Meetings()
    service(registry, runs, calendar=ManyEvents([_event("x", "Not A Group")]),
            meetings=meetings, aptem=Groups(["Something Else"])).discover_day(
        object(), object(), date(2026, 9, 4))
    assert meetings.calls == []
    assert registry.calls == 0


def test_aptem_matched_event_is_persisted_with_match_provenance():
    registry, runs = Registry(), Runs()
    summary = service(registry, runs, calendar=ManyEvents([_event("a", " Data &amp; AI ")]),
                      aptem=Groups(["Data & AI"])).discover_day(
        object(), object(), date(2026, 9, 4))
    row = next(iter(registry.rows.values()))
    assert row.group_match_status == "MATCHED"
    assert row.module == "Data & AI"
    assert row.normalized_subject == "data & ai"
    assert row.discovery_status == "READY"
    assert row.downstream_ready is True
    assert summary["registry_rows_created"] == 1


def test_repository_refuses_a_non_canonical_row_outright():
    """Defence in depth behind the service filter and the table CHECK."""
    from tests.unit.test_service import EVENT
    from app.common.hashing import lecture_identity
    from app.lectures.models import Lecture

    lecture = Lecture(
        lecture_id=lecture_identity(calendar_user_upn="o@example.invalid",
                                    calendar_event_id="e", i_cal_uid="i"),
        calendar_user_upn="o@example.invalid", calendar_event_id="e", i_cal_uid="i",
        meeting_id=None, join_url=EVENT.join_url, subject="Weekly Meeting",
        normalized_subject="weekly meeting", module=None,
        scheduled_start=EVENT.meeting_start, scheduled_end=EVENT.meeting_end,
        session_date=date(2026, 9, 4), calendar_timezone="UTC",
        calendar_organizer_address=None, meeting_organizer_user_id=None,
        organizer_validation_status=None, meeting_lookup_user_id=None,
        meeting_lookup_context_source=None, join_url_oid_hint_status=None,
        graph_meeting_subject=None, graph_meeting_start=None, graph_meeting_end=None,
        meeting_type=None, calendar_mapping_status="NOT_ATTEMPTED",
        group_match_status="NO_ACTIVE_GROUP_MATCH", discovery_status="REVIEW",
        is_cancelled=False,
    )

    class ExplodingConnection:
        def execute(self, *args, **kwargs):
            raise AssertionError("the repository must refuse before touching the database")

    with pytest.raises(PlatformError):
        LectureSessionRepository().upsert(ExplodingConnection(), lecture)


# --- duplicate occurrences ---------------------------------------------------


class MspMeetings(Meetings):
    """One MSP occurrence resolves; its duplicate-looking twin does not."""

    def resolve(self, url, calendar_organizer_address=None):
        self.calls.append((url, calendar_organizer_address))
        if url.endswith("orphan"):
            return MeetingResolution(
                ONLINE_MEETING_NOT_FOUND, reason="NO_EXACT_JOIN_URL_MATCH_IN_ANY_CONTEXT",
                meeting_lookup_user_id="mailbox-object-id",
                meeting_lookup_context_source=DISCOVERY_MAILBOX_CONTEXT,
            )
        return MeetingResolution(
            RESOLVED, meeting_id="meeting-msp",
            meeting_organizer_user_id="mailbox-object-id",
            organizer_validation=ORGANIZER_ID_CONFIRMED,
            meeting_lookup_user_id="mailbox-object-id",
            meeting_lookup_context_source=DISCOVERY_MAILBOX_CONTEXT,
        )


def _duplicate_msp_events():
    subject = "Ray-Managing Successful Programmes (MSP) Jan 2026"
    return [
        CalendarEvent("series", "ical-series", subject,
                      datetime(2026, 9, 4, 11, tzinfo=timezone.utc),
                      datetime(2026, 9, 4, 13, tzinfo=timezone.utc), "UTC",
                      "https://teams.microsoft.com/l/meetup-join/live",
                      calendar_organizer_address="Ray-MSP@example.invalid"),
        # Same normalized subject, same start, DIFFERENT calendar identity and
        # different meeting coordinates.
        CalendarEvent("exception", "ical-exception", subject + "  ",
                      datetime(2026, 9, 4, 11, tzinfo=timezone.utc),
                      datetime(2026, 9, 4, 13, tzinfo=timezone.utc), "UTC",
                      "https://teams.microsoft.com/l/meetup-join/orphan",
                      calendar_organizer_address="G2-PCP@example.invalid",
                      occurrence_type="exception"),
    ]


def test_duplicate_subject_and_start_remain_two_distinct_occurrences():
    events = _duplicate_msp_events()
    registry, runs = Registry(), Runs()
    summary = service(registry, runs, calendar=ManyEvents(events), meetings=MspMeetings(),
                      aptem=Groups(["Ray-Managing Successful Programmes (MSP) Jan 2026"])).discover_day(
        object(), object(), date(2026, 9, 4))

    assert summary["canonical_lecture_candidates"] == 2
    assert len(registry.rows) == 2, "occurrences must never be merged by subject + start"
    normalized = {row.normalized_subject for row in registry.rows.values()}
    assert len(normalized) == 1  # identical subject after normalization...
    assert len({row.lecture_id for row in registry.rows.values()}) == 2  # ...distinct identities
    assert len({row.calendar_event_id for row in registry.rows.values()}) == 2
    assert len({row.join_url for row in registry.rows.values()}) == 2


def test_unresolved_occurrence_is_retained_but_not_downstream_ready():
    events = _duplicate_msp_events()
    registry, runs = Registry(), Runs()
    summary = service(registry, runs, calendar=ManyEvents(events), meetings=MspMeetings(),
                      aptem=Groups(["Ray-Managing Successful Programmes (MSP) Jan 2026"])).discover_day(
        object(), object(), date(2026, 9, 4))

    by_event = {row.calendar_event_id: row for row in registry.rows.values()}
    live, orphan = by_event["series"], by_event["exception"]

    assert live.downstream_ready is True
    assert live.discovery_status == "READY"

    # Retained as a canonical lecture, but explicitly ineligible downstream.
    assert orphan.downstream_ready is False
    assert orphan.discovery_status == "REVIEW"
    assert orphan.meeting_id is None
    assert orphan.calendar_mapping_status == ONLINE_MEETING_NOT_FOUND

    assert summary["downstream_ready_lectures"] == 1
    assert summary["online_meetings_unresolved"] == 1
    assert [item["subject"] for item in summary["unresolved_lectures"]] == [orphan.subject]


def test_idempotency_holds_after_canonical_filtering():
    events = [*_duplicate_msp_events(), _event("noise", "Weekly Meeting", hour=15)]
    registry, runs = Registry(), Runs()
    discovery = service(registry, runs, calendar=ManyEvents(events), meetings=MspMeetings(),
                        aptem=Groups(["Ray-Managing Successful Programmes (MSP) Jan 2026"]))
    first = discovery.discover_day(object(), object(), date(2026, 9, 4))
    identities = {row.lecture_id for row in registry.rows.values()}
    second = discovery.discover_day(object(), object(), date(2026, 9, 4))

    assert (first["registry_rows_created"], first["registry_rows_updated"]) == (2, 0)
    assert (second["registry_rows_created"], second["registry_rows_updated"]) == (0, 2)
    assert {row.lecture_id for row in registry.rows.values()} == identities
    assert first["canonical_lecture_candidates"] == second["canonical_lecture_candidates"] == 2
    assert len(registry.rows) == 2
