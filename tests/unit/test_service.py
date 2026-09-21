from datetime import date, datetime, timezone

from app.graph.meetings import (
    DISCOVERY_MAILBOX_CONTEXT,
    JOIN_URL_OID_MATCHES_CONTEXT,
    ORGANIZER_ID_CONFIRMED,
    RESOLVED,
)
from app.lectures.models import CalendarDiscoveryBatch, CalendarEvent, MeetingResolution
from app.lectures.service import LectureDiscoveryService
from app.common.errors import PlatformError
import pytest


EVENT = CalendarEvent(
    "event-1", "ical-1", " Data &amp; AI ",
    datetime(2026, 9, 4, 7, tzinfo=timezone.utc), datetime(2026, 9, 4, 8, tzinfo=timezone.utc),
    "UTC", "https://teams.microsoft.com/join/1",
    calendar_organizer_address="CohortGroup@example.invalid",
)


class Calendar:
    calendar_user_upn = "owner@example.invalid"
    def __init__(self, event=EVENT): self.event = event
    def discover_calendar_events(self, target_date): return CalendarDiscoveryBatch(1, [self.event])


class Meetings:
    def __init__(self): self.calls = []
    def resolve(self, url, calendar_organizer_address=None):
        self.calls.append((url, calendar_organizer_address))
        return MeetingResolution(
            RESOLVED, meeting_id="meeting-1",
            meeting_organizer_user_id="mailbox-object-id",
            organizer_validation=ORGANIZER_ID_CONFIRMED,
            meeting_lookup_user_id="mailbox-object-id",
            meeting_lookup_context_source=DISCOVERY_MAILBOX_CONTEXT,
            join_url_oid_hint_status=JOIN_URL_OID_MATCHES_CONTEXT,
        )


class Aptem:
    def load_active_groups(self, connection): return ["Data & AI"]


class Registry:
    def __init__(self): self.rows = {}; self.calls = 0
    def upsert(self, connection, lecture):
        assert lecture.group_match_status == "MATCHED", "non-canonical row reached the registry"
        outcome = "updated" if lecture.lecture_id in self.rows else "created"
        self.rows[lecture.lecture_id] = lecture
        self.calls += 1
        return outcome
    def count_day(self, connection, target_date): return len(self.rows)


class Runs:
    def __init__(self): self.starts = self.completes = 0; self.summaries = []
    def start(self, connection, target_date):
        import uuid
        self.starts += 1
        return uuid.uuid4()
    def complete(self, connection, run_id, summary):
        self.completes += 1
        self.summaries.append(summary)


def service(registry, runs, calendar=None, qa=None, meetings=None, aptem=None):
    return LectureDiscoveryService(
        calendar=calendar or Calendar(), meetings=meetings or Meetings(),
        aptem_repository=aptem or Aptem(),
        lecture_repository=registry, run_repository=runs, qa_validation_repository=qa,
    )


def test_dry_run_performs_no_writes():
    registry, runs = Registry(), Runs()
    summary = service(registry, runs).discover_day(object(), object(), date(2026, 9, 4), persist=False)
    assert summary["registry_rows_written"] == 0
    assert registry.calls == runs.starts == runs.completes == 0


def test_registry_rerun_is_idempotent():
    registry, runs = Registry(), Runs()
    discovery = service(registry, runs)
    first = discovery.discover_day(object(), object(), date(2026, 9, 4))
    second = discovery.discover_day(object(), object(), date(2026, 9, 4))
    assert len(registry.rows) == 1
    assert registry.calls == 2
    assert (first["registry_rows_created"], first["registry_rows_updated"]) == (1, 0)
    assert (second["registry_rows_created"], second["registry_rows_updated"]) == (0, 1)


def test_registry_rerun_updates_same_occurrence():
    registry, runs = Registry(), Runs()
    service(registry, runs).discover_day(object(), object(), date(2026, 9, 4))
    changed = Calendar(CalendarEvent(**{**EVENT.__dict__, "calendar_event_id": "new-id", "subject": "Data & AI"}))
    service(registry, runs, changed).discover_day(object(), object(), date(2026, 9, 4))
    assert len(registry.rows) == 1
    assert next(iter(registry.rows.values())).calendar_event_id == "new-id"


def test_qa_comparison_is_evidence_not_a_discovery_source():
    class Qa:
        def load_day(self, connection, target_date):
            return [
                {"meeting_id": "meeting-1", "subject": "Data & AI"},
                {"meeting_id": "legacy-only", "subject": "Legacy only"},
            ]

    registry, runs = Registry(), Runs()
    summary = service(registry, runs, qa=Qa()).discover_day(
        object(), object(), date(2026, 9, 4), persist=False
    )
    assert summary["qa_comparison"]["qa_sessions"] == 2
    assert summary["qa_comparison"]["meeting_ids"]["matched"] == ["meeting-1"]
    assert summary["qa_comparison"]["meeting_ids"]["missing_from_new_discovery"] == ["legacy-only"]
    assert len(registry.rows) == 0


def test_zero_active_groups_stops_before_calendar_and_writes():
    class EmptyAptem:
        def load_active_groups(self, connection): return []

    class NeverCalendar(Calendar):
        def discover_calendar_events(self, target_date):
            raise AssertionError("calendar must not run after Aptem safety stop")

    registry, runs = Registry(), Runs()
    discovery = LectureDiscoveryService(
        calendar=NeverCalendar(), meetings=Meetings(), aptem_repository=EmptyAptem(),
        lecture_repository=registry, run_repository=runs,
    )
    with pytest.raises(PlatformError) as error:
        discovery.discover_day(object(), object(), date(2026, 9, 4), persist=False)
    assert error.value.code == "no_active_aptem_groups"
    assert registry.calls == 0
